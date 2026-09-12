"""AutoTyper desktop client (Tkinter).

Screens:
  AuthScreen      — sign in / create account
  DashboardScreen — balance, start/stop typing, settings
  RechargeWindow  — UPI QR for an exact amount + automatic confirmation

Why Tkinter: it ships with Python and needs no native GUI plugins, so the app
launches reliably everywhere (the previous Qt build failed to load its platform
plugin on some machines).

Threading rule: all network calls go through TaskRunner, which delivers results
on the Tk main thread. Widgets are never touched from a worker thread.
"""
from __future__ import annotations

import logging
import sys
import tkinter as tk
from tkinter import messagebox, ttk

import config
import permissions
import ui_kit as ui
from api_client import ApiClient, ApiError
from async_task import TaskRunner
from hotkey import HotkeyManager
from typer_engine import TyperEngine
from ui_kit import P

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("autotyper")
# httpx/httpcore are very chatty at DEBUG; keep them quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

APP_TITLE = "AutoTyper"


# ────────────────────────────────────────────────────────────────
# QR rendering
# ────────────────────────────────────────────────────────────────
def make_qr_photo(data: str, box_size: int = 7):
    """Render `data` to a Tk PhotoImage QR code.

    Uses qrcode's PIL renderer (clean quiet zone, sharp modules) so the result
    is reliably scannable, then hands it to Tk via PIL.ImageTk.
    """
    try:
        import io
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
        from PIL import Image, ImageTk
    except Exception:
        log.exception("QR: missing qrcode/Pillow")
        return None
    try:
        qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=3,
                           box_size=box_size)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        photo = ImageTk.PhotoImage(Image.open(buf))
        log.info("QR rendered %dx%d", photo.width(), photo.height())
        return photo
    except Exception:
        log.exception("QR: render failed")
        return None


# ────────────────────────────────────────────────────────────────
# Auth screen
# ────────────────────────────────────────────────────────────────
class AuthScreen(ttk.Frame):
    """Sign in / create account."""

    def __init__(self, master, app: "AutoTyperApp"):
        super().__init__(master, style="TFrame", padding=0)
        self.app = app
        self._busy = False

        outer = ttk.Frame(self, style="TFrame", padding=(36, 30))
        outer.pack(fill="both", expand=True)

        # Brand
        ttk.Label(outer, text="AutoTyper", style="H1.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Sign in to use your hours.",
                  style="Dim.TLabel").pack(anchor="w", pady=(2, 20))

        panel = ui.card(outer, padding=22)
        panel.pack(fill="x")

        ttk.Label(panel, text="Email", style="CardDim.TLabel").pack(anchor="w")
        self.email = ttk.Entry(panel, width=32)
        self.email.pack(fill="x", pady=(4, 14))

        ttk.Label(panel, text="Password", style="CardDim.TLabel").pack(anchor="w")
        self.password = ttk.Entry(panel, width=32, show="•")
        self.password.pack(fill="x", pady=(4, 4))
        ttk.Label(panel, text="At least 8 characters",
                  style="Faint.TLabel").pack(anchor="w")

        self.btn_login = ttk.Button(panel, text="Sign in", style="Accent.TButton",
                                    command=self._login)
        self.btn_login.pack(fill="x", pady=(18, 8))
        self.btn_signup = ttk.Button(panel, text="Create account",
                                     style="Ghost.TButton", command=self._signup)
        self.btn_signup.pack(fill="x")

        self.busy = ttk.Progressbar(outer, mode="indeterminate",
                                    style="Busy.Horizontal.TProgressbar")
        self.status = ttk.Label(outer, text="", style="Error.TLabel",
                                wraplength=340, justify="left")
        self.status.pack(anchor="w", pady=(14, 0))

        self.email.focus_set()
        # Enter submits sign-in.
        for w in (self.email, self.password):
            w.bind("<Return>", lambda _e: self._login())

    # ── helpers ──
    def _set_busy(self, busy: bool, msg: str = "") -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.btn_login.configure(state=state)
        self.btn_signup.configure(state=state)
        if busy:
            self.busy.pack(fill="x", pady=(14, 0), before=self.status)
            self.busy.start(12)
        else:
            self.busy.stop()
            self.busy.pack_forget()
        self._msg(msg, error=False)

    def _msg(self, text: str, error: bool = True) -> None:
        self.status.configure(text=text,
                              foreground=P.danger if error else P.text_dim)

    def _validate(self) -> tuple[str, str] | None:
        email = self.email.get().strip()
        pw = self.password.get()
        if "@" not in email or "." not in email:
            self._msg("Enter a valid email address.")
            return None
        if len(pw) < 8:
            self._msg("Password must be at least 8 characters.")
            return None
        return email, pw

    # ── actions ──
    def _login(self) -> None:
        if self._busy:
            return
        creds = self._validate()
        if not creds:
            return
        email, pw = creds
        self._set_busy(True, "Signing in…")
        self.app.tasks.run(
            lambda: self.app.api.login(email, pw),
            on_done=lambda token: self._ok(email, token),
            on_fail=self._fail,
            name="login",
        )

    def _signup(self) -> None:
        if self._busy:
            return
        creds = self._validate()
        if not creds:
            return
        email, pw = creds
        self._set_busy(True, "Creating your account…")
        self.app.tasks.run(
            lambda: self.app.api.signup(email, pw),
            on_done=lambda token: self._ok(email, token),
            on_fail=self._fail,
            name="signup",
        )

    def _ok(self, email: str, token: str) -> None:
        """Runs on the main thread."""
        log.info("authenticated as %s", email)
        config.update(token=token, email=email)
        self.app.api.token = token
        self._set_busy(False)
        self.app.show_dashboard()

    def _fail(self, msg: str) -> None:
        self._set_busy(False)
        self._msg(msg)


# ────────────────────────────────────────────────────────────────
# Recharge window
# ────────────────────────────────────────────────────────────────
class RechargeWindow(tk.Toplevel):
    """Buy hours: reserve an exact amount, show a UPI QR, poll for confirmation."""

    POLL_MS = 4000

    def __init__(self, app: "AutoTyperApp"):
        super().__init__(app.root)
        self.app = app
        self.title("Add hours")
        self.configure(bg=P.bg)
        self.resizable(False, False)
        self.transient(app.root)

        self._order_id: str | None = None
        self._qr_photo = None          # keep a ref or Tk drops the image
        self._poll_job: str | None = None
        self._checking = False

        wrap = ttk.Frame(self, style="TFrame", padding=(28, 24))
        wrap.pack(fill="both", expand=True)

        ttk.Label(wrap, text="Add hours", style="H1.TLabel").pack(anchor="w")
        ttk.Label(wrap, text="Pay by UPI from your phone. Hours are added "
                             "automatically once the payment lands.",
                  style="Dim.TLabel", wraplength=380,
                  justify="left").pack(anchor="w", pady=(2, 18))

        # ── step 1: choose hours ──
        self.choose = ui.card(wrap, padding=20)
        self.choose.pack(fill="x")
        row = ttk.Frame(self.choose, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="Hours", style="Card.TLabel").pack(side="left")
        self.hours = tk.IntVar(value=1)
        self.spin = ttk.Spinbox(row, from_=1, to=100, width=6,
                                textvariable=self.hours, justify="center")
        self.spin.pack(side="right")
        self.price_label = ttk.Label(self.choose, text="", style="Faint.TLabel")
        self.price_label.pack(anchor="w", pady=(10, 0))
        self.hours.trace_add("write", lambda *_: self._update_price())
        self._update_price()

        self.btn_buy = ttk.Button(self.choose, text="Continue to payment",
                                  style="Accent.TButton", command=self._buy)
        self.btn_buy.pack(fill="x", pady=(16, 0))

        # ── step 2: pay (built after the order is created) ──
        self.pay = ui.card(wrap, padding=20)
        self.amount_label = ttk.Label(self.pay, text="", style="Display.TLabel")
        self.vpa_label = ttk.Label(self.pay, text="", style="Faint.TLabel")
        self.qr_holder = ttk.Label(self.pay, background="#ffffff", padding=8)
        self.pay_hint = ttk.Label(
            self.pay,
            text="Scan with any UPI app (GPay, PhonePe, Paytm) and pay this "
                 "exact amount — the amount is how we identify your payment.",
            style="CardDim.TLabel", wraplength=340, justify="center")
        self.wait_bar = ttk.Progressbar(self.pay, mode="indeterminate",
                                        style="Busy.Horizontal.TProgressbar")
        self.result_label = ttk.Label(self.pay, text="", style="Success.TLabel")

        self.status = ttk.Label(wrap, text="", style="Dim.TLabel",
                                wraplength=380, justify="left")
        self.status.pack(anchor="w", pady=(14, 0))

        ui.center_window(self, 440, 330)
        self.protocol("WM_DELETE_WINDOW", self._close)

    # ── helpers ──
    def _update_price(self) -> None:
        try:
            h = max(1, int(self.hours.get() or 1))
        except (tk.TclError, ValueError):
            return
        self.price_label.configure(text=f"₹{h * 49:.2f} at ₹49.00 per hour")

    def _msg(self, text: str, error: bool = False) -> None:
        self.status.configure(text=text,
                              foreground=P.danger if error else P.text_dim)

    # ── buy ──
    def _buy(self) -> None:
        try:
            hours = float(max(1, int(self.hours.get())))
        except (tk.TclError, ValueError):
            self._msg("Enter a valid number of hours.", error=True)
            return
        self.btn_buy.configure(state="disabled")
        self.spin.configure(state="disabled")
        self._msg("Creating your order…")
        self.app.tasks.run(
            lambda: self.app.api.buy_hours(hours),
            on_done=self._order_created,
            on_fail=self._buy_failed,
            name="buy",
        )

    def _buy_failed(self, msg: str) -> None:
        self.btn_buy.configure(state="normal")
        self.spin.configure(state="normal")
        self._msg(msg, error=True)

    def _order_created(self, order: dict) -> None:
        """Main thread. Show the QR and start polling for confirmation."""
        self._order_id = order["order_id"]
        amount = order["amount_paise"] / 100
        upi_uri = order.get("upi_uri", "")
        log.info("order %s created for ₹%.2f", self._order_id, amount)

        self.choose.pack_forget()
        self.pay.pack(fill="both", expand=True)

        self.amount_label.configure(text=f"₹{amount:.2f}")
        self.amount_label.pack(anchor="center")
        # Show the payee UPI ID (parsed out of the upi:// link) so the user can
        # verify who they're paying, or pay manually if scanning fails.
        vpa = ""
        if upi_uri:
            from urllib.parse import parse_qs, urlparse
            vpa = parse_qs(urlparse(upi_uri).query).get("pa", [""])[0]
        if vpa:
            self.vpa_label.configure(text=f"to {vpa}")
            self.vpa_label.pack(anchor="center", pady=(2, 0))
        if upi_uri:
            self._qr_photo = make_qr_photo(upi_uri)
            if self._qr_photo is not None:
                self.qr_holder.configure(image=self._qr_photo)
                self.qr_holder.pack(anchor="center", pady=(14, 12))
            else:
                self._msg("Couldn't draw the QR code. Pay ₹"
                          f"{amount:.2f} to the UPI ID shown in your account.",
                          error=True)
            self.pay_hint.pack(anchor="center")
        else:
            # Non-UPI provider (mock): open the hosted pay page instead.
            import webbrowser
            webbrowser.open(order["pay_url"])
            self.pay_hint.configure(
                text="A payment page was opened in your browser. Complete it and "
                     "keep this window open.")
            self.pay_hint.pack(anchor="center")

        self.wait_bar.pack(fill="x", pady=(16, 6))
        self.wait_bar.start(12)
        self._msg("Waiting for your payment…")
        ui.center_window(self, 440, 620)
        self._schedule_poll()

    # ── confirmation polling ──
    def _schedule_poll(self) -> None:
        self._poll_job = self.after(self.POLL_MS, self._poll)

    def _poll(self) -> None:
        if not self._order_id or self._checking:
            self._schedule_poll()
            return
        self._checking = True
        oid = self._order_id

        def check():
            # Ask the backend to read recent bank alerts now (UPI); fall back to
            # a plain status read for other providers.
            try:
                return self.app.api.upi_check(oid)
            except ApiError:
                return self.app.api.order_status(oid)

        self.app.tasks.run(check, on_done=self._checked,
                           on_fail=self._check_failed, name="upi-check")

    def _checked(self, res: dict) -> None:
        self._checking = False
        status = (res or {}).get("status")
        if status == "paid":
            self._confirmed()
            return
        if status == "expired":
            self.wait_bar.stop()
            self.wait_bar.pack_forget()
            self._msg("This order expired. Close this window and try again.",
                      error=True)
            return
        self._schedule_poll()

    def _check_failed(self, _msg: str) -> None:
        self._checking = False
        self._schedule_poll()   # transient error; keep waiting

    def _confirmed(self) -> None:
        log.info("order %s confirmed", self._order_id)
        if self._poll_job:
            self.after_cancel(self._poll_job)
            self._poll_job = None
        self.wait_bar.stop()
        self.wait_bar.pack_forget()
        self.qr_holder.pack_forget()
        self.pay_hint.pack_forget()
        self.result_label.configure(text="✓  Payment received — hours added")
        self.result_label.pack(anchor="center", pady=(10, 0))
        self._msg("")
        self.app.refresh_balance()
        self.after(1600, self._close)

    def _close(self) -> None:
        if self._poll_job:
            try:
                self.after_cancel(self._poll_job)
            except Exception:
                pass
            self._poll_job = None
        self.app.refresh_balance()
        self.destroy()


# ────────────────────────────────────────────────────────────────
# Dashboard
# ────────────────────────────────────────────────────────────────
class DashboardScreen(ttk.Frame):
    """Balance, start/stop typing, and settings."""

    def __init__(self, master, app: "AutoTyperApp"):
        super().__init__(master, style="TFrame", padding=0)
        self.app = app

        wrap = ttk.Frame(self, style="TFrame", padding=(28, 22))
        wrap.pack(fill="both", expand=True)

        # ── header ──
        head = ttk.Frame(wrap, style="TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="AutoTyper", style="H1.TLabel").pack(side="left")
        self.account_btn = ttk.Button(head, text="Sign out", style="Link.TButton",
                                      command=self.app.logout)
        self.account_btn.pack(side="right")
        self.email_label = ttk.Label(wrap, text="", style="Dim.TLabel")
        self.email_label.pack(anchor="w", pady=(2, 18))

        # ── balance card ──
        bal = ui.card(wrap, padding=22)
        bal.pack(fill="x")
        ttk.Label(bal, text="TIME REMAINING", style="Faint.TLabel").pack(anchor="w")
        self.balance_label = ttk.Label(bal, text="—", style="Display.TLabel")
        self.balance_label.pack(anchor="w", pady=(2, 10))
        self.meter = ttk.Progressbar(bal, mode="determinate", maximum=100,
                                     style="Meter.Horizontal.TProgressbar")
        self.meter.pack(fill="x")
        srow = ttk.Frame(bal, style="Card.TFrame")
        srow.pack(fill="x", pady=(14, 0))
        self.session_label = ttk.Label(srow, text="Idle", style="CardDim.TLabel")
        self.session_label.pack(side="left")
        self.add_btn = ttk.Button(srow, text="Add hours", style="Ghost.TButton",
                                  command=self.app.open_recharge)
        self.add_btn.pack(side="right")

        # ── primary action ──
        self.toggle_btn = ttk.Button(wrap, text="Start typing",
                                     style="Accent.TButton",
                                     command=self.app.toggle_typing)
        self.toggle_btn.pack(fill="x", pady=(18, 8))
        self.status_label = ttk.Label(wrap, text="Ready.", style="Dim.TLabel",
                                      wraplength=380, justify="left")
        self.status_label.pack(anchor="w")

        # ── settings ──
        ui.hairline(wrap).pack(fill="x", pady=16)
        cfgrow = ttk.Frame(wrap, style="TFrame")
        cfgrow.pack(fill="x")
        self.idle_btn = ttk.Button(cfgrow, text="Pause delay",
                                   style="Ghost.TButton",
                                   command=self.app.change_idle_resume)
        self.idle_btn.pack(side="left")
        self.hotkey_btn = ttk.Button(cfgrow, text="Enable hotkey",
                                     style="Ghost.TButton",
                                     command=self.app.toggle_hotkey)
        self.hotkey_btn.pack(side="left", padx=(8, 0))
        self.settings_label = ttk.Label(wrap, text="", style="Dim.TLabel")
        self.settings_label.pack(anchor="w", pady=(10, 0))


# ────────────────────────────────────────────────────────────────
# Application
# ────────────────────────────────────────────────────────────────
class AutoTyperApp:
    def __init__(self) -> None:
        self.cfg = config.load()
        self.api = ApiClient(self.cfg["api_base"], token=self.cfg.get("token", ""))

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.minsize(420, 360)
        self.F = ui.apply_theme(self.root)
        ui.center_window(self.root, 440, 560)

        self.tasks = TaskRunner(self.root)
        self.container = ttk.Frame(self.root, style="TFrame")
        self.container.pack(fill="both", expand=True)

        self.screen: ttk.Frame | None = None
        self.dashboard: DashboardScreen | None = None

        self.balance_secs = 0.0
        self.peak_secs = 1.0          # for the meter scale
        self.session_id: str | None = None
        self._recharge: RechargeWindow | None = None
        self._countdown_job: str | None = None
        self._countdown_left = 0

        # Typing engine + hotkey
        self.engine = TyperEngine(
            on_active_seconds=self._report_active_seconds,
            on_status=self._engine_status,
            on_stopped=self._engine_stopped,
            idle_resume_secs=float(self.cfg.get("idle_resume_secs", 5.0)),
        )
        self.hotkeys = HotkeyManager()

        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        log.info("AutoTyper starting. api_base=%s", self.cfg["api_base"])
        log.info("platform=%s frozen=%s accessibility_trusted=%s",
                 sys.platform, getattr(sys, "frozen", False),
                 permissions.accessibility_trusted())

    # ── screen switching ──
    def _swap(self, screen: ttk.Frame) -> None:
        if self.screen is not None:
            self.screen.destroy()
        self.screen = screen
        screen.pack(fill="both", expand=True)

    def show_auth(self) -> None:
        self.dashboard = None
        ui.center_window(self.root, 440, 470)
        self._swap(AuthScreen(self.container, self))

    def show_dashboard(self) -> None:
        self.cfg = config.load()
        self.dashboard = DashboardScreen(self.container, self)
        ui.center_window(self.root, 440, 560)
        self._swap(self.dashboard)
        self.dashboard.email_label.configure(text=self.cfg.get("email", ""))
        self._update_settings_label()
        self.refresh_balance()
        # Start the hotkey only if enabled AND the platform supports it safely.
        if self.cfg.get("hotkey_enabled") and sys.platform != "darwin":
            self.root.after(400, lambda: self._install_hotkey(self.cfg["hotkey"]))

    # ── balance ──
    def refresh_balance(self) -> None:
        self.tasks.run(self.api.balance, on_done=self._balance_loaded,
                       on_fail=self._balance_failed, name="balance")

    def _balance_loaded(self, data: dict) -> None:
        self.balance_secs = float(data["balance_secs"])
        self.peak_secs = max(self.peak_secs, self.balance_secs, 1.0)
        if not self.dashboard:
            return
        self.dashboard.balance_label.configure(
            text=ui.fmt_hms_long(self.balance_secs))
        pct = 0 if self.peak_secs <= 0 else min(
            100, self.balance_secs / self.peak_secs * 100)
        self.dashboard.meter.configure(value=pct)
        if self.balance_secs <= 0:
            self.dashboard.status_label.configure(
                text="You're out of hours. Add hours to start typing.",
                foreground=P.warning)

    def _balance_failed(self, msg: str) -> None:
        if self.dashboard:
            self.dashboard.status_label.configure(
                text=f"Couldn't load balance: {msg}", foreground=P.danger)

    # ── typing ──
    def toggle_typing(self) -> None:
        if self.engine.running:
            self.engine.stop("Stopped.")
            return
        if self._countdown_job is not None:
            self._cancel_countdown()
            return
        if self.balance_secs <= 0:
            messagebox.showinfo(
                APP_TITLE, "You have no hours left. Add hours to continue.",
                parent=self.root)
            self.open_recharge()
            return

        # macOS silently discards simulated keystrokes unless the app is trusted
        # for Accessibility, so check before pretending to type.
        trusted = permissions.accessibility_trusted()
        log.info("accessibility trusted = %s", trusted)
        if trusted is False:
            who = permissions.host_app_hint()
            if messagebox.askyesno(
                "Accessibility permission needed",
                "macOS won't let AutoTyper send keystrokes until you allow it.\n\n"
                f"Turn ON: {who}\n"
                "in System Settings → Privacy & Security → Accessibility, then "
                "come back and press Start typing again.\n\n"
                "Open those settings now?",
                parent=self.root,
            ):
                permissions.request_accessibility()
                permissions.open_accessibility_settings()
            return
        self.dashboard.toggle_btn.configure(state="disabled")
        self.dashboard.status_label.configure(text="Starting session…",
                                              foreground=P.text_dim)
        self.tasks.run(self.api.start_session, on_done=self._session_started,
                       on_fail=self._session_failed, name="session-start")

    def _session_started(self, res: dict) -> None:
        """Session is open. Give the user a few seconds to focus the window they
        want typed into, then minimise ourselves and start the engine.

        Without this, keystrokes land in the AutoTyper window (it has focus after
        you click Start) instead of the app you actually want typed into.
        """
        self.session_id = res["session_id"]
        self.balance_secs = float(res["balance_secs"])
        self._balance_loaded({"balance_secs": self.balance_secs})
        d = self.dashboard
        d.toggle_btn.configure(text="Cancel", style="Stop.TButton", state="normal")
        d.session_label.configure(text="Get ready", foreground=P.warning)
        self._countdown_left = int(self.cfg.get("start_delay_secs", 5))
        self._tick_countdown()

    def _tick_countdown(self) -> None:
        if self._countdown_job is not None:
            self._countdown_job = None
        if not self.dashboard or self.session_id is None:
            return  # cancelled
        if self._countdown_left <= 0:
            self._begin_typing()
            return
        self.dashboard.status_label.configure(
            text=f"Click the window you want typed into — starting in "
                 f"{self._countdown_left}s. AutoTyper will minimise itself.",
            foreground=P.warning)
        self._countdown_left -= 1
        self._countdown_job = self.root.after(1000, self._tick_countdown)

    def _begin_typing(self) -> None:
        """Minimise out of the way, then start emitting keystrokes."""
        self._countdown_job = None
        try:
            self.root.iconify()   # so our window can't receive the keystrokes
        except Exception:
            pass
        self.engine.start()
        d = self.dashboard
        d.toggle_btn.configure(text="Stop typing", style="Stop.TButton",
                               state="normal")
        d.session_label.configure(text="● Typing", foreground=P.success)
        d.status_label.configure(
            text="Typing. It pauses automatically when you use the mouse or "
                 "keyboard, and resumes once you stop.", foreground=P.text_dim)

    def _cancel_countdown(self) -> None:
        """User pressed Cancel during the get-ready countdown."""
        if self._countdown_job is not None:
            try:
                self.root.after_cancel(self._countdown_job)
            except Exception:
                pass
            self._countdown_job = None
        self._countdown_left = 0
        sid, self.session_id = self.session_id, None
        if self.dashboard:
            d = self.dashboard
            d.toggle_btn.configure(text="Start typing", style="Accent.TButton",
                                   state="normal")
            d.session_label.configure(text="Idle", foreground=P.text_dim)
            d.status_label.configure(text="Cancelled.", foreground=P.text_dim)
        if sid:
            self.tasks.run(lambda: self.api.stop_session(sid, 0.0),
                           on_done=lambda _r: self.refresh_balance(),
                           on_fail=lambda _m: None, name="session-cancel")

    def _session_failed(self, msg: str) -> None:
        d = self.dashboard
        d.toggle_btn.configure(state="normal")
        d.status_label.configure(text=msg, foreground=P.danger)
        if "hour" in msg.lower() or "recharge" in msg.lower():
            self.open_recharge()

    def _report_active_seconds(self, secs: float) -> bool:
        """Called from the engine thread. Debits on the backend; returns True to
        stop. Network I/O here is fine (not the UI thread); UI updates are posted
        back to the main thread."""
        if not self.session_id:
            return False
        res = self.api.heartbeat(self.session_id, secs)
        self.balance_secs = float(res["balance_secs"])
        self.tasks.post(lambda: self._balance_loaded(
            {"balance_secs": self.balance_secs}))
        return bool(res.get("should_stop"))

    def _engine_status(self, text: str) -> None:
        self.tasks.post(lambda: self._set_engine_status(text))

    def _set_engine_status(self, text: str) -> None:
        if not self.dashboard:
            return
        self.dashboard.status_label.configure(text=text, foreground=P.text_dim)
        low = text.lower()
        if "paused" in low:
            self.dashboard.session_label.configure(text="❙❙ Paused",
                                                   foreground=P.warning)
        elif "resumed" in low or "typing" in low:
            self.dashboard.session_label.configure(text="● Typing",
                                                   foreground=P.success)

    def _engine_stopped(self, reason: str) -> None:
        self.tasks.post(lambda: self._on_engine_stopped(reason))

    def _on_engine_stopped(self, reason: str) -> None:
        sid, self.session_id = self.session_id, None
        # Bring the window back so the user can see the result / restart.
        try:
            self.root.deiconify()
            self.root.lift()
        except Exception:
            pass
        if self.dashboard:
            d = self.dashboard
            d.toggle_btn.configure(text="Start typing", style="Accent.TButton",
                                   state="normal")
            d.session_label.configure(text="Idle", foreground=P.text_dim)
            d.status_label.configure(text=reason, foreground=P.text_dim)
        if sid:
            self.tasks.run(lambda: self.api.stop_session(sid, 0.0),
                           on_done=lambda _r: self.refresh_balance(),
                           on_fail=lambda _m: self.refresh_balance(),
                           name="session-stop")

    # ── recharge ──
    def open_recharge(self) -> None:
        if self._recharge is not None and self._recharge.winfo_exists():
            self._recharge.lift()
            return
        self._recharge = RechargeWindow(self)

    # ── settings ──
    def _update_settings_label(self) -> None:
        if not self.dashboard:
            return
        idle = float(self.cfg.get("idle_resume_secs", 5.0))
        on = bool(self.cfg.get("hotkey_enabled")) and sys.platform != "darwin"
        combo = self.cfg.get("hotkey", "")
        if sys.platform == "darwin":
            self.dashboard.hotkey_btn.configure(text="About hotkey")
            hk = "Hotkey n/a on macOS"
        else:
            self.dashboard.hotkey_btn.configure(
                text="Disable hotkey" if on else "Enable hotkey")
            hk = f"Hotkey {combo}" if on else "Hotkey off"
        delay = int(self.cfg.get("start_delay_secs", 5))
        self.dashboard.settings_label.configure(
            text=f"{delay}s to switch windows  ·  resumes {idle:g}s after you "
                 f"stop  ·  {hk}")

    def change_idle_resume(self) -> None:
        from tkinter import simpledialog
        cur = float(self.cfg.get("idle_resume_secs", 5.0))
        val = simpledialog.askfloat(
            "Pause delay",
            "Resume typing after this many seconds of no mouse or keyboard "
            "activity:", initialvalue=cur, minvalue=0.5, maxvalue=120.0,
            parent=self.root)
        if val is None:
            return
        self.cfg = config.update(idle_resume_secs=float(val))
        self.engine.set_idle_resume_secs(float(val))
        self._update_settings_label()

    def toggle_hotkey(self) -> None:
        # macOS: a global event tap (pynput) started inside a GUI app fights the
        # toolkit's main run loop and aborts the process at the native level, so
        # we don't offer it here rather than ship a button that can kill the app.
        if sys.platform == "darwin":
            messagebox.showinfo(
                "Global hotkey unavailable on macOS",
                "System-wide hotkeys aren't supported in this build on macOS — "
                "the OS keyboard hook conflicts with the app's window system and "
                "would close the app.\n\n"
                "Use the Start/Stop button instead. Typing still pauses "
                "automatically whenever you use the mouse or keyboard.",
                parent=self.root)
            return

        if self.cfg.get("hotkey_enabled"):
            self.hotkeys.stop()
            self.cfg = config.update(hotkey_enabled=False)
            self._update_settings_label()
            return
        self.cfg = config.update(hotkey_enabled=True)
        self._install_hotkey(self.cfg["hotkey"])
        self._update_settings_label()

    def _install_hotkey(self, combo: str) -> None:
        log.info("starting global hotkey %r", combo)
        try:
            self.hotkeys.start(combo, lambda: self.tasks.post(self.toggle_typing))
        except Exception:
            log.exception("hotkey failed to start")
            if self.dashboard:
                self.dashboard.settings_label.configure(
                    text="Hotkey couldn't start — grant Accessibility permission.",
                    foreground=P.warning)

    # ── lifecycle ──
    def logout(self) -> None:
        if self.engine.running:
            self.engine.stop("Signed out.")
        self.hotkeys.stop()
        config.update(token="", email="")
        self.api.token = ""
        self.cfg = config.load()
        self.show_auth()

    def quit(self) -> None:
        log.info("shutting down")
        if self.engine.running:
            self.engine.stop("Closing.")
        self.hotkeys.stop()
        self.tasks.stop()
        self.root.destroy()

    def start(self) -> int:
        # Decide the first screen: verify a saved token, else show auth.
        if self.api.token:
            def verify():
                self.api.balance()
                return True
            self.tasks.run(verify,
                           on_done=lambda _ok: self.show_dashboard(),
                           on_fail=lambda _m: self.show_auth(),
                           name="verify-token")
            # Placeholder while verifying.
            splash = ttk.Frame(self.container, style="TFrame", padding=40)
            ttk.Label(splash, text="AutoTyper", style="H1.TLabel").pack(pady=(60, 6))
            ttk.Label(splash, text="Signing you in…", style="Dim.TLabel").pack()
            self._swap(splash)
        else:
            self.show_auth()
        self.root.mainloop()
        return 0


def main() -> int:
    return AutoTyperApp().start()


if __name__ == "__main__":
    sys.exit(main())
