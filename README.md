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
                                              │  webhook / confirm
                                              ▼
                                        Payment provider
                                        (mock  ➜  Juspay)
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
- `POST /payments/buy` → creates an order, returns a payment URL.
- `POST /payments/webhook/juspay` → gateway calls this to confirm payment; hours
  are credited (idempotently).
- `GET  /payments/status/{order_id}` → client polls until `paid`.
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
   `.env` (TURSO_*, JWT_SECRET, PAYMENT_PROVIDER, and the JUSPAY_* + pricing when
   you go live).
4. Your API is now at `https://<project>.vercel.app`. The `vercel.json` routes
   all paths to the FastAPI app in `api/index.py`.

> Free tier is comfortable for ~10–15 users. Turso holds the data so a redeploy
> never loses balances.

### Going live with Juspay

The payment layer is behind an interface (`backend/payments.py`) with two
implementations:

- `mock` (default): no real money; the buy flow completes via the mock pages.
- `juspay`: real order create + webhook confirmation.

To switch: set `PAYMENT_PROVIDER=juspay` and fill in `JUSPAY_API_KEY`,
`JUSPAY_MERCHANT_ID`, `JUSPAY_BASE_URL` (sandbox vs prod), and the webhook
credentials. Point your Juspay webhook at
`https://<project>.vercel.app/payments/webhook/juspay`. Hours are credited when
the order status is `CHARGED`. The `JuspayProvider` is written against Juspay's
Orders API shape; verify field names against your merchant dashboard before
production.

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
│   ├── payments.py         # MockProvider + JuspayProvider behind get_provider()
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
- The mock payment provider is for testing only — switch to `juspay` for real
  money.
- Session time is enforced by the backend; the client is not trusted with it.

## License

MIT
