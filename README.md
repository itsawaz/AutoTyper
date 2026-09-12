# AutoTyper ⌨️

AutoTyper simulates human-like typing activity and sells that time by the hour.
It has two parts:

- **`client/`** — a standalone desktop app (macOS `.app` / Windows `.exe`) that
  logs in, shows the user's remaining hours, lets them recharge, lets them set a
  launch hotkey, and runs the typing engine. This is what users download.
- **`backend/`** — a tiny FastAPI service deployed free on Vercel. It holds the
  only copies of the Turso database token and the payment secret, verifies
  logins, enforces the hours balance, and confirms payments. Users never see it.

```
 ┌──────────────┐        HTTPS         ┌──────────────────┐        ┌─────────┐
 │  Desktop app │  ───────────────▶    │  FastAPI backend │  ────▶ │  Turso  │
 │  (client/)   │   login / balance    │  (backend/)      │   SQL  │  (data) │
 │              │   session heartbeat  │  holds secrets   │        └─────────┘
 └──────────────┘   buy hours          └──────────────────┘
                                              │  reads bank credit-alert
                                              ▼  emails to confirm UPI payments
                                        Gmail API (read-only)
                                        (mock provider for local dev)
```

## Why the split (please read before changing it)

Anything shipped inside the desktop `.exe` can be extracted by any user. So the
Turso write-token, the payment secret, and the "did they pay / how many hours do
they have" decision **must** live on the backend. The client only ever holds the
logged-in user's own session token. This is why the app talks to a backend
instead of hitting Turso directly.

---

## 1. Backend

### What it does

- `POST /auth/signup`, `POST /auth/login` → returns a JWT.
- `GET  /me/balance` → remaining seconds / hours.
- `POST /session/start` → opens a usage session (402 if no hours left).
- `POST /session/heartbeat` → client reports elapsed active seconds; server
  debits the balance and replies `should_stop` when it hits zero.
- `POST /session/stop` → closes the session.
- `POST /payments/buy` → creates an order; for UPI it reserves a unique amount
  and returns a `upi://` link + pay page URL.
- `GET  /payments/status/{order_id}` → client polls until `paid`.
- `POST /payments/upi/check/{order_id}` → on-demand: reads recent bank alerts and
  confirms this order (used by the client while the user waits).
- `POST /payments/poll-gmail` → scheduled/batch poll of Gmail that credits any
  matching UPI orders (protected by `POLL_SECRET`).
- `GET  /admin/orders`, `GET /admin/events` → audit/verification views
  (protected by `POLL_SECRET`).
- Mock provider only: `/payments/mock/pay` + `/payments/mock/confirm` simulate a
  successful payment so the whole flow works with zero setup.

Hours are enforced **server-side**. The client cannot grant itself time.

### Set up Turso (free)

1. Install the CLI and sign up at https://turso.tech.
2. Create a DB and get its URL + token:
   ```bash
   turso db create autotyper
   turso db show autotyper --url        # -> libsql://autotyper-you.turso.io
   turso db tokens create autotyper     # -> the auth token
   ```

### Configure

```bash
cd backend
cp .env.example .env
# edit .env: paste TURSO_DATABASE_URL and TURSO_AUTH_TOKEN, set a JWT_SECRET,
# keep PAYMENT_PROVIDER=mock for now.
python -c "import secrets; print(secrets.token_urlsafe(48))"   # for JWT_SECRET
```

Pricing is `PRICE_PER_HOUR_PAISE` (default `4900` = ₹49.00/hour).

### Run locally

Use Python 3.11 or 3.12 (3.14 has no wheels for some native deps yet).

```bash
cd backend
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt uvicorn
uvicorn main:app --reload --port 8000
# open http://127.0.0.1:8000  -> {"ok": true, ...}
```

### Deploy free on Vercel

1. Install the CLI: `npm i -g vercel` (or use the Vercel dashboard + Git import).
2. From `backend/`: `vercel` then `vercel --prod`.
3. In the Vercel project settings, add the same environment variables from your
   `.env` (TURSO_*, JWT_SECRET, PAYMENT_PROVIDER, UPI_*, GMAIL_*, POLL_SECRET,
   and pricing).
4. Your API is now at `https://<project>.vercel.app`. The `vercel.json` routes
   all paths to the FastAPI app in `api/index.py`.

> Free tier is comfortable for ~10–15 users. Turso holds the data so a redeploy
> never loses balances.

### Payments: free UPI + Gmail confirmation (no gateway, no KYC)

The payment layer is behind an interface (`backend/payments.py`):

- `mock`: no real money; the buy flow completes via the mock pages (local dev).
- `upi_gmail` (default): real UPI collection confirmed by reading your bank's
  credit-alert emails. No payment gateway and no GST/merchant KYC.

How it works:

1. **Buy** — the backend reserves a **unique amount** for the order: the base
   price plus a random paise tag (e.g. ₹49.00 → ₹49.37). This makes an incoming
   bank alert map to exactly one order. It returns a `upi://pay?...` link.
2. **Pay** — the user pays that exact amount from any UPI app to your VPA. The
   money lands in your bank.
3. **Confirm** — your bank emails a credit alert. The backend reads recent alert
   emails via the Gmail API, parses the amount + UPI reference, matches it to the
   pending order, and credits the hours. Confirmation is **idempotent** (a bank
   reference credits at most one order) and every alert seen is logged to
   `payment_events` for later verification.

Two confirmation triggers:

- **On-demand** (fast): while the user waits, the client calls
  `POST /payments/upi/check/{order_id}` every few seconds; that reads Gmail and
  confirms immediately.
- **Scheduled** (safety net): a Vercel cron hits `POST /payments/poll-gmail`.
  Note the Vercel **Hobby (free) plan runs crons at most once per day** — the
  on-demand check is what makes confirmation fast; the cron just catches anything
  missed. Set `CRON_SECRET` == `POLL_SECRET` in Vercel so the cron authenticates.

#### One-time setup

1. **UPI**: set your receiving VPA and payee name:
   ```
   PAYMENT_PROVIDER=upi_gmail
   UPI_VPA=your-vpa@bank
   UPI_PAYEE_NAME=AutoTyper
   ```
2. **Bank parser**: set `UPI_BANK_PARSER` to your bank (`hdfc`, `paytm`, or
   `generic`) and `UPI_ALERT_SENDER` to the exact sender address of your bank's
   alert emails (e.g. `alerts@hdfcbank.bank.in`). Only emails from that sender
   are trusted, which blocks spoofed alerts.
3. **Gmail (read-only)**:
   - Create a Google Cloud project and **enable the Gmail API**.
   - Create an OAuth client of type **Desktop app** → gives a client id/secret.
   - Run the helper to mint a refresh token (opens a browser, asks for read-only
     Gmail access):
     ```bash
     cd backend
     GMAIL_CLIENT_ID=... GMAIL_CLIENT_SECRET=... python get_gmail_token.py
     ```
   - Put `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` in env.
   - In Gmail, create a **filter** on your bank's alert sender that applies a
     label (default `upi-alerts`), and set `GMAIL_LABEL` to it. The poller only
     reads that label.
4. **Poll secret**: set a strong `POLL_SECRET` (and the same value as
   `CRON_SECRET` in Vercel so the scheduled poll authenticates).

#### Reconciliation / verification

Every credit alert is recorded in `payment_events` with its outcome:

- `matched` — credited an order (includes order id, amount, bank ref).
- `duplicate` — a bank ref already credited; ignored.
- `unmatched` — a payment arrived with no open order for that amount (e.g. the
  user paid the wrong amount) — reconcile these manually.
- `error` — a Gmail read failure.

View them:
```bash
curl "https://<project>.vercel.app/admin/events?secret=$POLL_SECRET"
curl "https://<project>.vercel.app/admin/orders?status=paid&secret=$POLL_SECRET"
```

> Honest limits: this is a DIY confirmation, not a certified gateway — no
> settlement guarantee, no automatic refunds/disputes. Bank email formats can
> change and need a small parser tweak. Fine for a small user base; the mock
> provider remains available for local testing.

---

## 2. Desktop client

### What it does

- Login / signup screen (talks to the backend).
- Main window: current balance, **Start/Stop typing**, **Recharge**, **Change
  hotkey**, and **Log out**.
- The launch hotkey is user-configurable (default `<ctrl>+<shift>+1`) and toggles
  typing globally.
- **Auto-pause on user activity:** when you move the mouse or type, auto-typing
  pauses immediately and resumes after a configurable idle period (default 5s,
  set via the **Idle resume** button). The engine ignores its own synthetic
  keystrokes, so only *real* user input pauses it. Paused time is not counted
  against your hours balance.
- While typing, it reports active time to the backend every ~20s and stops
  automatically when the balance runs out.
- Recharge opens the payment page in the browser and polls until the backend
  confirms, then updates the balance.

Local settings live in `~/.autotyper/config.json` (hotkey, saved token, and the
backend URL). No server secret is ever stored here.

### Point the client at your backend

The backend URL comes from the `AUTOTYPER_API_BASE` environment variable (falls
back to `http://127.0.0.1:8000` for local dev), and is saved into the local
config. For distributed builds, set it before building or edit
`client/config.py`'s `DEFAULT_API_BASE` to your Vercel URL.

### Run from source

```bash
cd client
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
AUTOTYPER_API_BASE=https://<project>.vercel.app python app.py
```

> **macOS:** the app simulates keystrokes, so grant Accessibility permission in
> *System Settings → Privacy & Security → Accessibility* on first run.

### Build the standalone executables

Build on each target OS (PyInstaller does not cross-compile).

**macOS** → `dist/AutoTyper.app`
```bash
cd client
./build_macos.sh
```

**Windows** → `dist\AutoTyper\AutoTyper.exe`
```bat
cd client
build_windows.bat
```

Both scripts install `pyinstaller`, clean previous builds, and run the shared
`autotyper.spec` (windowed app, no console).

---

## Project layout

```
AutoTyper/
├── backend/
│   ├── api/index.py        # Vercel serverless entrypoint (exposes FastAPI app)
│   ├── main.py             # API routes (auth, balance, sessions, payments)
│   ├── auth.py             # bcrypt password hashing + JWT
│   ├── db.py               # Turso/libSQL access + schema
│   ├── payments.py         # MockProvider + UpiGmailProvider behind get_provider()
│   ├── gmail_client.py     # Gmail reader + bank credit-alert parsers
│   ├── get_gmail_token.py  # one-time helper to mint a Gmail refresh token
│   ├── config.py           # env-driven settings
│   ├── vercel.json         # routes all paths to the app
│   ├── requirements.txt
│   └── .env.example
├── client/
│   ├── app.py              # PySide6 GUI (login, balance, recharge, hotkey)
│   ├── typer_engine.py     # human-like typing engine (from the original script)
│   ├── api_client.py       # HTTP client for the backend
│   ├── hotkey.py           # global hotkey listener
│   ├── config.py           # local settings store (~/.autotyper)
│   ├── autotyper.spec      # PyInstaller build spec (mac + windows)
│   ├── build_macos.sh
│   ├── build_windows.bat
│   └── requirements.txt
└── auto_typer.py           # original standalone script (kept for reference)
```

## Notes & limits

- Use **Python 3.11 / 3.12** everywhere; 3.14 breaks native wheels.
- The mock payment provider is for testing only — use `upi_gmail` for real
  payments.
- Session time is enforced by the backend; the client is not trusted with it.

## License

MIT
