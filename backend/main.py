"""AutoTyper backend API.

Runs on Vercel Python serverless (or any ASGI host). Holds the only copies of
the Turso token and Gmail credentials. The desktop client talks to this over
HTTPS and never sees those secrets.

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
    # Present only for the UPI provider so the client can render a QR / intent.
    upi_uri: str = ""


class OrderStatusOut(BaseModel):
    order_id: str
    status: str


# ── health / public pages ──────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>AutoTyper</title></head>
    <body style="font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 20px;line-height:1.6;color:#222">
      <h1>AutoTyper</h1>
      <p>AutoTyper is a desktop application that simulates human-like typing
         activity. Usage is sold by the hour; users log in, top up their balance
         by UPI, and the app runs while they have time remaining.</p>
      <p>This site hosts the AutoTyper backend service (accounts, balance, and
         payment confirmation) for the desktop app. It is not a consumer website.</p>
      <ul>
        <li><a href="/privacy">Privacy Policy</a></li>
      </ul>
      <p style="color:#888">Contact: helloneerajkumarsingh@gmail.com</p>
    </body></html>
    """


@app.get("/healthz")
def health():
    return {"ok": True, "service": "autotyper-api"}


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return """
    <!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>AutoTyper — Privacy Policy</title></head>
    <body style="font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 20px;line-height:1.6;color:#222">
      <h1>Privacy Policy</h1>
      <p><em>Last updated: September 2026</em></p>

      <h2>Who we are</h2>
      <p>AutoTyper ("we", "the app") is a desktop application and its supporting
         backend service. Contact: helloneerajkumarsingh@gmail.com.</p>

      <h2>Information we collect</h2>
      <ul>
        <li><strong>Account data:</strong> the email address and password
            (stored only as a secure hash) you use to sign in.</li>
        <li><strong>Usage data:</strong> the amount of active time you consume,
            used to deduct from your paid balance.</li>
        <li><strong>Payment records:</strong> order amounts and the bank
            reference number of a completed UPI payment, used to confirm and
            credit your purchase.</li>
      </ul>

      <h2>How we use Google / Gmail data</h2>
      <p>To confirm UPI payments automatically, the service reads the account
         owner's own bank payment-alert emails using read-only Gmail API access
         (<code>gmail.readonly</code>). We only read messages from the
         configured bank sender to extract the payment amount and reference
         number. We do not read, store, or share the contents of any other
         email. Google user data is used solely to confirm payments and is not
         transferred to third parties, used for advertising, or used for any
         other purpose. Our use of information received from Google APIs adheres
         to the Google API Services User Data Policy, including the Limited Use
         requirements.</p>

      <h2>Data sharing</h2>
      <p>We do not sell or share your personal data with third parties. Data is
         stored in our database provider (Turso) solely to operate the service.</p>

      <h2>Data retention</h2>
      <p>Account, usage, and payment records are retained for as long as your
         account is active or as needed for reconciliation and legal
         obligations. You can request deletion by emailing us.</p>

      <h2>Your choices</h2>
      <p>You may request access to or deletion of your data at any time by
         contacting helloneerajkumarsingh@gmail.com. You can revoke the app's
         Gmail access at any time at
         <a href="https://myaccount.google.com/permissions">Google Account
         permissions</a>.</p>

      <h2>Contact</h2>
      <p>Questions about this policy: helloneerajkumarsingh@gmail.com</p>
    </body></html>
    """


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
    _ensure_db()
    settings = get_settings()
    provider = get_provider()
    base_amount = int(round(body.hours * settings.price_per_hour_paise))
    order_id = str(uuid.uuid4())

    expires_at = None
    if provider.name == "upi_gmail":
        # Reserve a unique amount so the bank credit-alert maps to one order.
        amount_paise = _reserve_unique_upi_amount(base_amount)
        expires_at = _future(settings.upi_order_ttl_minutes)
    else:
        amount_paise = base_amount

    created = provider.create_order(
        order_id=order_id,
        amount_paise=amount_paise,
        currency=settings.currency,
        customer_id=user["id"],
    )
    db.execute(
        "INSERT INTO orders (id, user_id, hours, amount_paise, currency, status, "
        "provider, provider_ref, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, 'created', ?, ?, ?, ?)",
        [order_id, user["id"], body.hours, amount_paise, settings.currency,
         provider.name, created.provider_ref, _now(), expires_at],
    )
    return BuyOut(
        order_id=order_id,
        pay_url=_absolutize(request, created.pay_url),
        amount_paise=amount_paise,
        currency=settings.currency,
        upi_uri=created.upi_uri,
    )


def _future(minutes: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _reserve_unique_upi_amount(base_amount: int) -> int:
    """Pick base_amount + a paise tag not currently used by another pending,
    unexpired UPI order. Prevents two open orders sharing an amount so an
    incoming alert is unambiguous."""
    import random
    settings = get_settings()
    now = _now()
    # Amounts currently in use by open (unexpired, unpaid) UPI orders.
    taken_rows = db.query_all(
        "SELECT amount_paise FROM orders WHERE provider = 'upi_gmail' "
        "AND status = 'created' AND (expires_at IS NULL OR expires_at > ?)",
        [now],
    )
    taken = {int(r["amount_paise"]) for r in taken_rows}
    lo, hi = settings.upi_tag_min_paise, settings.upi_tag_max_paise
    candidates = [base_amount + t for t in range(lo, hi + 1)]
    random.shuffle(candidates)
    for amt in candidates:
        if amt not in taken:
            return amt
    # All tags in use (many concurrent orders) — fall back to base amount.
    return base_amount


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
    order = db.query_one("SELECT * FROM orders WHERE id = ?", [order_id])
    _credit_order(order_id)
    if order:
        _log_event("mock", "matched", int(order["amount_paise"]), "",
                   order_id=order_id, user_id=order["user_id"],
                   detail=f"mock credited {order['hours']}h")
    return HTMLResponse(
        "<html><body style='font-family:sans-serif;text-align:center;padding:40px'>"
        "<h2>✅ Payment successful</h2><p>Your hours have been added. "
        "You can close this window and return to AutoTyper.</p></body></html>"
    )


# ── UPI + Gmail confirmation ────────────────────────────────────
@app.get("/payments/upi/pay", response_class=HTMLResponse)
def upi_pay(order_id: str):
    """Browser pay page for a UPI order: shows the exact amount + upi:// link.
    The desktop app renders its own QR; this page is a fallback for opening the
    link on a phone / clicking through to a UPI app."""
    _ensure_db()
    order = db.query_one("SELECT * FROM orders WHERE id = ?", [order_id])
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    settings = get_settings()
    from payments import build_upi_uri
    amount_paise = int(order["amount_paise"])
    uri = build_upi_uri(settings.upi_vpa, settings.upi_payee_name, amount_paise,
                        f"AutoTyper {order_id[:8]}")
    amount_rs = f"{amount_paise / 100:.2f}"
    return f"""
    <html><body style="font-family:sans-serif;text-align:center;padding:40px">
      <h2>Pay ₹{amount_rs} to add hours</h2>
      <p>Pay this <b>exact</b> amount by UPI so we can confirm your payment
         automatically. Do not round it off.</p>
      <p>UPI ID: <code>{settings.upi_vpa}</code></p>
      <p><a href="{uri}" style="display:inline-block;padding:12px 24px;
         background:#0b5;color:#fff;border-radius:8px;text-decoration:none">
         Open in a UPI app</a></p>
      <p style="color:#888">After paying, return to AutoTyper — your balance
         updates automatically within a minute.</p>
    </body></html>
    """


def _expire_stale_upi_orders() -> None:
    db.execute(
        "UPDATE orders SET status = 'expired' WHERE provider = 'upi_gmail' "
        "AND status = 'created' AND expires_at IS NOT NULL AND expires_at <= ?",
        [_now()],
    )


def _log_event(source: str, outcome: str, amount_paise=None, bank_ref="",
               order_id=None, user_id=None, subject="", snippet="", detail="") -> None:
    """Record every payment signal we see for later verification / auditing."""
    try:
        db.execute(
            "INSERT INTO payment_events (id, created_at, source, outcome, "
            "amount_paise, bank_ref, order_id, user_id, subject, snippet, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [str(uuid.uuid4()), _now(), source, outcome, amount_paise,
             bank_ref or None, order_id, user_id, subject[:500], snippet[:500],
             detail[:500]],
        )
    except Exception:
        # Auditing must never break the payment path.
        pass


def _match_alert_to_order(amount_paise: int, ref: str, source: str = "gmail_poll",
                          subject: str = "", snippet: str = "") -> bool:
    """Find one open UPI order with this exact amount and credit it. Logs the
    outcome (matched / duplicate / unmatched) to payment_events either way.
    Returns True if an order was credited."""
    # Dedup: if this bank ref already credited an order, do nothing.
    if ref:
        already = db.query_one("SELECT id FROM orders WHERE matched_ref = ?", [ref])
        if already:
            _log_event(source, "duplicate", amount_paise, ref,
                       order_id=already["id"], subject=subject, snippet=snippet,
                       detail="bank_ref already credited")
            return False
    order = db.query_one(
        "SELECT * FROM orders WHERE provider = 'upi_gmail' AND status = 'created' "
        "AND amount_paise = ? AND (expires_at IS NULL OR expires_at > ?) "
        "ORDER BY created_at ASC LIMIT 1",
        [amount_paise, _now()],
    )
    if not order:
        _log_event(source, "unmatched", amount_paise, ref, subject=subject,
                   snippet=snippet, detail="no open order for this amount")
        return False
    db.execute(
        "UPDATE orders SET matched_ref = ? WHERE id = ?", [ref or None, order["id"]]
    )
    _credit_order(order["id"])
    _log_event(source, "matched", amount_paise, ref, order_id=order["id"],
               user_id=order["user_id"], subject=subject, snippet=snippet,
               detail=f"credited {order['hours']}h")
    return True


@app.api_route("/payments/poll-gmail", methods=["GET", "POST"])
def poll_gmail(request: Request):
    """Read recent HDFC credit-alert emails and credit any matching UPI orders.

    Protected by POLL_SECRET. Accepts the secret via:
      - ?secret=... query param, or
      - X-Poll-Secret header, or
      - Authorization: Bearer <CRON_SECRET> (Vercel Cron injects this when a
        CRON_SECRET env var is set; set CRON_SECRET == POLL_SECRET).
    Runs on a schedule (Vercel cron) and can also be triggered manually."""
    _ensure_db()
    settings = get_settings()
    auth = request.headers.get("authorization", "")
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    supplied = (
        request.query_params.get("secret")
        or request.headers.get("x-poll-secret", "")
        or bearer
    )
    if not settings.poll_secret or supplied != settings.poll_secret:
        raise HTTPException(status_code=401, detail="Bad poll secret")

    _expire_stale_upi_orders()
    import gmail_client
    try:
        alerts = gmail_client.fetch_recent_alerts()
    except Exception as e:  # noqa: BLE001
        _log_event("gmail_poll", "error", detail=str(e))
        raise HTTPException(status_code=502, detail="Gmail read failed")
    credited = 0
    for a in alerts:
        if _match_alert_to_order(a.amount_paise, a.ref, source="gmail_poll",
                                 subject=a.subject, snippet=a.snippet):
            credited += 1
    return {"checked": len(alerts), "credited": credited}


@app.post("/payments/upi/check/{order_id}")
def upi_check(order_id: str, user: dict = Depends(current_user)):
    """On-demand check the client calls while waiting: polls Gmail and reports
    this order's status. Authenticated to the order owner, so no poll secret
    needed. Rate is naturally limited by the client's poll interval."""
    _ensure_db()
    order = db.query_one(
        "SELECT * FROM orders WHERE id = ? AND user_id = ?", [order_id, user["id"]]
    )
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order["status"] == "created":
        _expire_stale_upi_orders()
        try:
            import gmail_client
            for a in gmail_client.fetch_recent_alerts():
                _match_alert_to_order(a.amount_paise, a.ref, source="gmail_check",
                                      subject=a.subject, snippet=a.snippet)
        except Exception as e:  # noqa: BLE001
            # Gmail hiccup — log and let the client retry on its next poll.
            _log_event("gmail_check", "error", detail=str(e))
        order = db.query_one("SELECT * FROM orders WHERE id = ?", [order_id])
    return {"order_id": order_id, "status": order["status"]}


# ── admin / verification (protected by POLL_SECRET) ─────────────
def _require_admin(request: Request) -> None:
    settings = get_settings()
    auth = request.headers.get("authorization", "")
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    supplied = (
        request.query_params.get("secret")
        or request.headers.get("x-poll-secret", "")
        or bearer
    )
    if not settings.poll_secret or supplied != settings.poll_secret:
        raise HTTPException(status_code=401, detail="Admin auth required")


@app.get("/admin/orders")
def admin_orders(request: Request, status: str | None = None, limit: int = 100):
    """List recent orders (optionally filtered by status) for verification."""
    _require_admin(request)
    _ensure_db()
    limit = max(1, min(limit, 500))
    if status:
        rows = db.query_all(
            "SELECT id, user_id, hours, amount_paise, currency, status, provider, "
            "provider_ref, matched_ref, created_at, paid_at, expires_at "
            "FROM orders WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            [status, limit],
        )
    else:
        rows = db.query_all(
            "SELECT id, user_id, hours, amount_paise, currency, status, provider, "
            "provider_ref, matched_ref, created_at, paid_at, expires_at "
            "FROM orders ORDER BY created_at DESC LIMIT ?",
            [limit],
        )
    return {"count": len(rows), "orders": rows}


@app.get("/admin/events")
def admin_events(request: Request, outcome: str | None = None, limit: int = 100):
    """List recent payment_events (matched/unmatched/duplicate/error) for audit.

    Use outcome=unmatched to find payments that came in without a matching order
    (e.g. a user paid the wrong amount) so you can reconcile manually."""
    _require_admin(request)
    _ensure_db()
    limit = max(1, min(limit, 500))
    if outcome:
        rows = db.query_all(
            "SELECT * FROM payment_events WHERE outcome = ? "
            "ORDER BY created_at DESC LIMIT ?",
            [outcome, limit],
        )
    else:
        rows = db.query_all(
            "SELECT * FROM payment_events ORDER BY created_at DESC LIMIT ?",
            [limit],
        )
    return {"count": len(rows), "events": rows}
