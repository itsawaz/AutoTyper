"""AutoTyper backend API.

Runs on Vercel Python serverless (or any ASGI host). Holds the only copies of
the Turso token and Juspay secret. The desktop client talks to this over HTTPS
and never sees those secrets.

Responsibilities:
- auth: signup / login (JWT)
- hours: report balance, start a session, heartbeat to debit balance, stop
- payments: create an order, gateway webhook credits hours, poll order status

Hours are enforced HERE, server-side. The client cannot grant itself time.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr, Field

import db
from auth import (
    create_access_token,
    current_user,
    hash_password,
    verify_password,
)
from config import get_settings
from payments import get_provider

app = FastAPI(title="AutoTyper API", version="1.0.0")

# The desktop client isn't a browser so it doesn't need CORS, but the payment
# pages open in a browser and a future web checkout might too. Permissive here
# is safe because every sensitive route requires a bearer token anyway.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── lifecycle ──────────────────────────────────────────────────
# On serverless, startup hooks don't fire reliably per cold-started instance,
# so we also lazily ensure the schema exists on the first request that needs it.
_db_ready = False


def _ensure_db() -> None:
    global _db_ready
    if _db_ready:
        return
    db.init_db()
    _db_ready = True


@app.on_event("startup")
def _startup():
    try:
        _ensure_db()
    except Exception:
        # Don't crash the app if the DB is briefly unreachable on cold start;
        # requests will call _ensure_db() again and surface the real error.
        pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── models ─────────────────────────────────────────────────────
class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class BalanceOut(BaseModel):
    balance_secs: float
    balance_hours: float


class SessionOut(BaseModel):
    session_id: str
    balance_secs: float


class HeartbeatIn(BaseModel):
    session_id: str
    elapsed_secs: float = Field(ge=0, le=120)  # cap per-heartbeat to limit abuse


class HeartbeatOut(BaseModel):
    balance_secs: float
    should_stop: bool


class BuyIn(BaseModel):
    hours: float = Field(gt=0, le=100)


class BuyOut(BaseModel):
    order_id: str
    pay_url: str
    amount_paise: int
    currency: str


class OrderStatusOut(BaseModel):
    order_id: str
    status: str


# ── health ─────────────────────────────────────────────────────
@app.get("/")
def health():
    return {"ok": True, "service": "autotyper-api"}


# ── auth ───────────────────────────────────────────────────────
@app.post("/auth/signup", response_model=TokenOut)
def signup(body: Credentials):
    _ensure_db()
    existing = db.query_one("SELECT id FROM users WHERE email = ?", [body.email.lower()])
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")
    user_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO users (id, email, password_hash, balance_secs, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [user_id, body.email.lower(), hash_password(body.password), 0.0, _now()],
    )
    return TokenOut(access_token=create_access_token(user_id))


@app.post("/auth/login", response_model=TokenOut)
def login(body: Credentials):
    _ensure_db()
    user = db.query_one("SELECT * FROM users WHERE email = ?", [body.email.lower()])
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return TokenOut(access_token=create_access_token(user["id"]))


# ── balance ────────────────────────────────────────────────────
@app.get("/me/balance", response_model=BalanceOut)
def get_balance(user: dict = Depends(current_user)):
    secs = float(user["balance_secs"])
    return BalanceOut(balance_secs=secs, balance_hours=round(secs / 3600, 4))


# ── sessions / hours consumption ───────────────────────────────
@app.post("/session/start", response_model=SessionOut)
def start_session(user: dict = Depends(current_user)):
    if float(user["balance_secs"]) <= 0:
        raise HTTPException(status_code=402, detail="No hours remaining. Please recharge.")
    session_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO sessions (id, user_id, start_time, status) VALUES (?, ?, ?, 'active')",
        [session_id, user["id"], _now()],
    )
    return SessionOut(session_id=session_id, balance_secs=float(user["balance_secs"]))


@app.post("/session/heartbeat", response_model=HeartbeatOut)
def heartbeat(body: HeartbeatIn, user: dict = Depends(current_user)):
    """Debit `elapsed_secs` from the balance. Called every ~15-30s by the client
    while typing is active. Server is the source of truth for remaining time."""
    session = db.query_one(
        "SELECT * FROM sessions WHERE id = ? AND user_id = ?",
        [body.session_id, user["id"]],
    )
    if not session or session["status"] != "active":
        raise HTTPException(status_code=404, detail="No active session")

    new_balance = max(0.0, float(user["balance_secs"]) - body.elapsed_secs)
    prev_dur = float(session["duration_secs"] or 0.0)
    db.execute("UPDATE users SET balance_secs = ? WHERE id = ?", [new_balance, user["id"]])
    db.execute(
        "UPDATE sessions SET duration_secs = ? WHERE id = ?",
        [prev_dur + body.elapsed_secs, body.session_id],
    )
    return HeartbeatOut(balance_secs=new_balance, should_stop=new_balance <= 0)


@app.post("/session/stop")
def stop_session(body: HeartbeatIn, user: dict = Depends(current_user)):
    """Final debit + close the session."""
    session = db.query_one(
        "SELECT * FROM sessions WHERE id = ? AND user_id = ?",
        [body.session_id, user["id"]],
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    new_balance = max(0.0, float(user["balance_secs"]) - body.elapsed_secs)
    prev_dur = float(session["duration_secs"] or 0.0)
    db.execute("UPDATE users SET balance_secs = ? WHERE id = ?", [new_balance, user["id"]])
    db.execute(
        "UPDATE sessions SET duration_secs = ?, end_time = ?, status = 'closed' WHERE id = ?",
        [prev_dur + body.elapsed_secs, _now(), body.session_id],
    )
    return {"balance_secs": new_balance}


# ── payments ───────────────────────────────────────────────────
def _absolutize(request: Request, url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    base = str(request.base_url).rstrip("/")
    return base + url


@app.post("/payments/buy", response_model=BuyOut)
def buy_hours(body: BuyIn, request: Request, user: dict = Depends(current_user)):
    settings = get_settings()
    provider = get_provider()
    amount_paise = int(round(body.hours * settings.price_per_hour_paise))
    order_id = str(uuid.uuid4())

    created = provider.create_order(
        order_id=order_id,
        amount_paise=amount_paise,
        currency=settings.currency,
        customer_id=user["id"],
    )
    db.execute(
        "INSERT INTO orders (id, user_id, hours, amount_paise, currency, status, "
        "provider, provider_ref, created_at) VALUES (?, ?, ?, ?, ?, 'created', ?, ?, ?)",
        [order_id, user["id"], body.hours, amount_paise, settings.currency,
         provider.name, created.provider_ref, _now()],
    )
    return BuyOut(
        order_id=order_id,
        pay_url=_absolutize(request, created.pay_url),
        amount_paise=amount_paise,
        currency=settings.currency,
    )


def _credit_order(order_id: str) -> None:
    """Idempotently mark an order paid and add its hours to the user balance."""
    order = db.query_one("SELECT * FROM orders WHERE id = ?", [order_id])
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order["status"] == "paid":
        return  # already credited; idempotent
    add_secs = float(order["hours"]) * 3600.0
    user = db.query_one("SELECT * FROM users WHERE id = ?", [order["user_id"]])
    new_balance = float(user["balance_secs"]) + add_secs
    db.execute("UPDATE users SET balance_secs = ? WHERE id = ?", [new_balance, user["id"]])
    db.execute(
        "UPDATE orders SET status = 'paid', paid_at = ? WHERE id = ?",
        [_now(), order_id],
    )


@app.get("/payments/status/{order_id}", response_model=OrderStatusOut)
def order_status(order_id: str, user: dict = Depends(current_user)):
    order = db.query_one(
        "SELECT * FROM orders WHERE id = ? AND user_id = ?", [order_id, user["id"]]
    )
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return OrderStatusOut(order_id=order_id, status=order["status"])


@app.post("/payments/webhook/juspay")
async def juspay_webhook(request: Request):
    """Called by Juspay on payment status change. Verifies the signature/auth
    then credits hours when the order is CHARGED.

    Juspay's payload nests the order under content.order, and echoes our order id
    both as order_id and as udf1 (which we set on create). We look in all the
    likely places so a field-name change doesn't silently drop a payment."""
    _ensure_db()
    provider = get_provider()
    body = await request.body()
    provider.verify_webhook({k.lower(): v for k, v in request.headers.items()}, body)

    import json
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    content = payload.get("content", {}) or {}
    order = content.get("order", content) if isinstance(content, dict) else {}
    if not isinstance(order, dict):
        order = {}

    order_id = (
        order.get("udf1")
        or order.get("order_id")
        or payload.get("order_id")
    )
    status_str = str(
        order.get("status") or payload.get("status") or ""
    ).upper()

    # Juspay success statuses for a completed charge.
    if order_id and status_str in ("CHARGED", "SUCCESS", "PAID", "COMPLETED"):
        _credit_order(order_id)
    return {"received": True}


# ── mock pay page (only meaningful when PAYMENT_PROVIDER=mock) ──
@app.get("/payments/mock/pay", response_class=HTMLResponse)
def mock_pay(order_id: str):
    """A tiny fake checkout page. Clicking 'Pay' credits the order."""
    return f"""
    <html><body style="font-family: sans-serif; text-align:center; padding:40px">
      <h2>AutoTyper — Test Checkout (mock)</h2>
      <p>Order <code>{order_id}</code></p>
      <form method="post" action="/payments/mock/confirm">
        <input type="hidden" name="order_id" value="{order_id}"/>
        <button style="padding:12px 24px; font-size:16px">Pay now</button>
      </form>
      <p style="color:#888">No real money. This simulates a successful UPI payment.</p>
    </body></html>
    """


@app.post("/payments/mock/confirm", response_class=HTMLResponse)
async def mock_confirm(request: Request):
    form = await request.form()
    order_id = form.get("order_id")
    if not order_id:
        raise HTTPException(status_code=400, detail="Missing order_id")
    _credit_order(order_id)
    return HTMLResponse(
        "<html><body style='font-family:sans-serif;text-align:center;padding:40px'>"
        "<h2>✅ Payment successful</h2><p>Your hours have been added. "
        "You can close this window and return to AutoTyper.</p></body></html>"
    )
