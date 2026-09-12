"""AutoTyper desktop client (PySide6).

Standalone GUI: login/signup, hours balance, recharge (opens the payment page in
the browser and polls for confirmation), configurable global hotkey, and
start/stop of the typing engine. Hours are enforced by the backend — this app
reports active time and stops when the server says the balance is exhausted.
"""
from __future__ import annotations

import logging
import os
import sys
import webbrowser

# Ensure the GUI uses the native platform plugin. Clear any stray QT_QPA_PLATFORM
# (e.g. "offscreen" left in the environment) that would otherwise start the app
# headless / fail to show a window. Must happen before QApplication is created.
if os.environ.get("QT_QPA_PLATFORM"):
    os.environ.pop("QT_QPA_PLATFORM", None)

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

import config
from api_client import ApiClient, ApiError
from hotkey import HotkeyManager
from typer_engine import TyperEngine

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("autotyper")


def _fmt_hms(secs: float) -> str:
    secs = int(max(0, secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _make_qr_pixmap(data: str, box_size: int = 8):
    """Render `data` (a upi:// link) to a scannable QPixmap QR code.

    We use qrcode's own PIL renderer (well-tested, produces a clean quiet zone
    and sharp modules), export it to PNG bytes, and load that into a QPixmap.
    This avoids hand-rolling pixel data into a QImage, which previously produced
    an unscannable code. Returns None if the libraries aren't available.
    """
    try:
        import io
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except Exception:
        log.exception("QR: qrcode import failed")
        return None
    try:
        qr = qrcode.QRCode(
            error_correction=ERROR_CORRECT_M,
            border=4,           # spec-minimum quiet zone
            box_size=box_size,  # pixels per module -> crisp, no rescaling
        )
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        pix = QPixmap()
        ok = pix.loadFromData(buf.getvalue(), "PNG")
        if not ok:
            log.error("QR: QPixmap.loadFromData failed")
            return None
        log.debug("QR rendered: %dx%d px", pix.width(), pix.height())
        return pix
    except Exception:
        log.exception("QR: rendering failed")
        return None


# ── background worker for one-off API calls ────────────────────
class Worker(QObject):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        log.debug("Worker.run start")
        try:
            result = self._fn()
            log.debug("Worker.run success, emitting done")
            self.done.emit(result)
        except ApiError as e:
            log.warning("Worker.run ApiError: %s", e.message)
            self.failed.emit(e.message)
        except Exception as e:  # noqa: BLE001
            log.exception("Worker.run unexpected error")
            self.failed.emit(str(e))


def run_async(parent, fn, on_done=None, on_fail=None):
    """Run `fn` on a QThread; deliver the result on the UI thread.

    Cleanup is driven by QThread.finished (never by calling thread.wait() from a
    slot, which blocks the UI thread and can deadlock inside a modal exec loop).
    Thread/worker refs are held on `parent` until the thread finishes, then
    released, so nothing is garbage-collected mid-run.
    """
    log.debug("run_async: starting thread")
    thread = QThread(parent)
    worker = Worker(fn)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)

    # IMPORTANT: worker.done/failed are emitted on the WORKER thread. If we
    # connect plain Python callables directly, Qt (AutoConnection) runs them on
    # the worker thread — so touching widgets (e.g. dialog.accept()) crashes /
    # deadlocks with "Cannot filter events for objects in a different thread".
    # We marshal every user callback onto the UI thread via QTimer.singleShot(0)
    # bound to `parent`, which lives on the UI thread.
    def _deliver(cb, value):
        QTimer.singleShot(0, parent, lambda: cb(value))

    def _on_done(value):
        log.debug("run_async: done received on %s, marshalling to UI thread",
                  QThread.currentThread())
        thread.quit()
        if on_done:
            _deliver(on_done, value)

    def _on_fail(msg):
        thread.quit()
        if on_fail:
            _deliver(on_fail, msg)

    worker.done.connect(_on_done)
    worker.failed.connect(_on_fail)

    # Keep strong refs so neither is collected while running.
    parent._threads = getattr(parent, "_threads", [])
    entry = (thread, worker)
    parent._threads.append(entry)

    def _released():
        worker.deleteLater()
        try:
            parent._threads.remove(entry)
        except ValueError:
            pass

    thread.finished.connect(_released)
    thread.finished.connect(thread.deleteLater)
    thread.start()


# ── login / signup dialog ──────────────────────────────────────
class LoginDialog(QDialog):
    def __init__(self, api: ApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self.setWindowTitle("AutoTyper — Sign in")
        self.setModal(True)

        self.email = QLineEdit()
        self.email.setPlaceholderText("you@example.com")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("At least 8 characters")

        form = QFormLayout()
        form.addRow("Email", self.email)
        form.addRow("Password", self.password)

        self.login_btn = QPushButton("Log in")
        self.signup_btn = QPushButton("Sign up")
        self.login_btn.clicked.connect(lambda: self._submit(self.api.login))
        self.signup_btn.clicked.connect(lambda: self._submit(self.api.signup))

        btns = QHBoxLayout()
        btns.addWidget(self.login_btn)
        btns.addWidget(self.signup_btn)

        self.status = QLabel("")
        self.status.setStyleSheet("color:#c0392b")

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(btns)
        layout.addWidget(self.status)

    def _set_busy(self, busy: bool):
        self.login_btn.setEnabled(not busy)
        self.signup_btn.setEnabled(not busy)
        self.status.setText("Working…" if busy else "")
        self.status.setStyleSheet("color:#888" if busy else "color:#c0392b")

    def _submit(self, fn):
        email = self.email.text().strip()
        pw = self.password.text()
        log.debug("submit: email=%r pw_len=%d api_base=%s", email, len(pw),
                  self.api.base_url)
        if not email or len(pw) < 8:
            self.status.setText("Enter an email and a password of 8+ characters.")
            return
        self._set_busy(True)
        run_async(
            self,
            lambda: fn(email, pw),
            on_done=lambda token: self._ok(email, token),
            on_fail=self._err,
        )

    def _ok(self, email, token):
        log.debug("signup/login OK, token_len=%d, saving config + accepting",
                  len(token or ""))
        config.update(token=token, email=email)
        self.accept()

    def _err(self, msg):
        log.warning("signup/login failed: %s", msg)
        self._set_busy(False)
        self.status.setText(msg)


# ── recharge dialog ─────────────────────────────────────────────
class RechargeDialog(QDialog):
    credited = Signal()

    def __init__(self, api: ApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self.setWindowTitle("Recharge hours")
        self.setModal(True)
        self._order_id = None
        self._upi_uri = ""

        self.hours = QSpinBox()
        self.hours.setRange(1, 100)
        self.hours.setValue(1)
        self.hours.setSuffix(" hour(s)")

        self.buy_btn = QPushButton("Buy hours")
        self.buy_btn.clicked.connect(self._buy)

        # QR + amount shown after an order is created.
        self.qr_label = QLabel()
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setVisible(False)
        self.amount_label = QLabel()
        self.amount_label.setAlignment(Qt.AlignCenter)
        self.amount_label.setStyleSheet("font-size:16px; font-weight:600")

        self.status = QLabel("Choose how many hours to add, then pay by UPI.")
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignCenter)

        form = QFormLayout()
        form.addRow("Hours", self.hours)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.buy_btn)
        layout.addWidget(self.amount_label)
        layout.addWidget(self.qr_label)
        layout.addWidget(self.status)

        # On-demand confirmation polling while the user pays.
        self._poll = QTimer(self)
        self._poll.setInterval(4000)
        self._poll.timeout.connect(self._check_status)

    def _buy(self):
        self.buy_btn.setEnabled(False)
        self.hours.setEnabled(False)
        self.status.setText("Creating order…")
        run_async(
            self,
            lambda: self.api.buy_hours(float(self.hours.value())),
            on_done=self._order_created,
            on_fail=self._err,
        )

    def _order_created(self, order):
        self._order_id = order["order_id"]
        amount = order["amount_paise"] / 100
        self._upi_uri = order.get("upi_uri", "")

        if self._upi_uri:
            # UPI flow: show a QR of the exact-amount upi:// link to scan with
            # a phone (payment happens on the phone, not this computer).
            self.amount_label.setText(f"Pay exactly ₹{amount:.2f}")
            pix = _make_qr_pixmap(self._upi_uri)
            if pix is not None:
                self.qr_label.setPixmap(pix)
                self.qr_label.setVisible(True)
            self.status.setText(
                "Open any UPI app on your phone (GPay / PhonePe / Paytm), scan "
                "this QR, and pay the exact amount shown — the amount identifies "
                "your payment. Keep this window open; your hours are added "
                "automatically within a minute of paying."
            )
        else:
            # Mock/other flow: open the returned pay page in a browser.
            webbrowser.open(order["pay_url"])
            self.amount_label.setText(f"₹{amount:.2f}")
            self.status.setText(
                f"Opened payment page for ₹{amount:.2f}. Complete it, then wait "
                "here — your hours are added automatically."
            )
        self._poll.start()

    def _check_status(self):
        if not self._order_id:
            return

        def check():
            # For UPI, ask the backend to read Gmail now; falls back to plain
            # status for the mock provider.
            try:
                return self.api.upi_check(self._order_id)
            except ApiError:
                return self.api.order_status(self._order_id)

        run_async(
            self,
            check,
            on_done=self._status_result,
            on_fail=lambda _msg: None,  # keep polling on transient errors
        )

    def _status_result(self, res):
        if res.get("status") == "paid":
            self._poll.stop()
            self.status.setText("✅ Payment confirmed. Hours added.")
            self.credited.emit()
            QTimer.singleShot(1200, self.accept)
        elif res.get("status") == "expired":
            self._poll.stop()
            self.status.setText("This order expired. Close and try again.")

    def _err(self, msg):
        self.buy_btn.setEnabled(True)
        self.hours.setEnabled(True)
        self.status.setText(f"Error: {msg}")


# ── hotkey capture dialog ───────────────────────────────────────
class HotkeyDialog(QDialog):
    def __init__(self, current: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set launch hotkey")
        self.setModal(True)
        self.result_combo = current

        self.field = QLineEdit(current)
        self.field.setPlaceholderText("<ctrl>+<shift>+1")
        hint = QLabel(
            "Use pynput format, e.g. <ctrl>+<shift>+1, <cmd>+<alt>+t.\n"
            "Modifiers: <ctrl> <alt> <shift> <cmd>."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#888")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Launch / toggle hotkey:"))
        layout.addWidget(self.field)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def _accept(self):
        combo = self.field.text().strip()
        # Validate by attempting to construct a listener.
        try:
            from pynput import keyboard
            keyboard.GlobalHotKeys({combo: lambda: None})
        except Exception:
            QMessageBox.warning(self, "Invalid hotkey", "That combo isn't valid pynput syntax.")
            return
        self.result_combo = combo
        self.accept()


# ── main window ─────────────────────────────────────────────────
class MainWindow(QMainWindow):
    _hb_result = Signal(object)   # backend heartbeat response -> UI thread
    _hb_error = Signal(str)

    def __init__(self, api: ApiClient, cfg: dict):
        super().__init__()
        log.debug("MainWindow.__init__ start")
        self.api = api
        self.cfg = cfg
        self.session_id = None
        self.balance_secs = 0.0

        self.setWindowTitle("AutoTyper")
        self.resize(420, 260)

        self.balance_label = QLabel("Balance: —")
        self.balance_label.setStyleSheet("font-size:18px; font-weight:600")
        self.status_label = QLabel("Idle.")
        self.status_label.setStyleSheet("color:#555")
        self.hotkey_label = QLabel()
        self.hotkey_label.setStyleSheet("color:#888")

        self.toggle_btn = QPushButton("Start typing")
        self.toggle_btn.clicked.connect(self.toggle_typing)
        self.recharge_btn = QPushButton("Recharge")
        self.recharge_btn.clicked.connect(self.open_recharge)
        self.hotkey_btn = QPushButton("Change hotkey")
        self.hotkey_btn.clicked.connect(self.change_hotkey)
        self.idle_btn = QPushButton("Idle resume")
        self.idle_btn.clicked.connect(self.change_idle_resume)

        row = QHBoxLayout()
        row.addWidget(self.recharge_btn)
        row.addWidget(self.hotkey_btn)
        row.addWidget(self.idle_btn)

        central = QWidget()
        v = QVBoxLayout(central)
        v.addWidget(self.balance_label)
        v.addWidget(self.status_label)
        v.addWidget(self.toggle_btn)
        v.addLayout(row)
        v.addWidget(self.hotkey_label)
        self.setCentralWidget(central)

        # Menu: logout
        logout = QAction("Log out", self)
        logout.triggered.connect(self.logout)
        self.menuBar().addMenu("Account").addAction(logout)

        # Engine + hotkey
        self.engine = TyperEngine(
            on_active_seconds=self._report_active_seconds,
            on_status=self._engine_status,
            on_stopped=self._engine_stopped,
            idle_resume_secs=float(self.cfg.get("idle_resume_secs", 5.0)),
        )
        self.hotkeys = HotkeyManager()
        self._hotkey_ok = True
        # Defer starting the global hotkey listener until AFTER the window is
        # shown and the event loop is running. On macOS pynput's global listener
        # can hard-crash if started during construction / before the app is
        # fully up; deferring lets the window appear first and isolates the risk.
        self._update_hotkey_label()
        QTimer.singleShot(300, lambda: self._install_hotkey(self.cfg["hotkey"]))

        # Heartbeat responses come from the engine's worker thread; marshal to UI.
        self._hb_result.connect(self._on_hb_result)
        self._hb_error.connect(lambda m: self.status_label.setText(f"Sync issue: {m}"))

        self.refresh_balance()

    # ── balance ────────────────────────────────────────────────
    def refresh_balance(self):
        run_async(
            self,
            self.api.balance,
            on_done=self._set_balance,
            on_fail=lambda m: self.status_label.setText(f"Couldn't load balance: {m}"),
        )

    def _set_balance(self, data):
        self.balance_secs = float(data["balance_secs"])
        self.balance_label.setText(f"Balance: {_fmt_hms(self.balance_secs)}")

    # ── typing toggle ──────────────────────────────────────────
    def toggle_typing(self):
        if self.engine.running:
            self._stop_typing()
        else:
            self._start_typing()

    def _start_typing(self):
        if self.balance_secs <= 0:
            QMessageBox.information(self, "No hours", "You're out of hours. Please recharge.")
            self.open_recharge()
            return
        self.toggle_btn.setEnabled(False)
        self.status_label.setText("Starting session…")
        run_async(
            self,
            self.api.start_session,
            on_done=self._session_started,
            on_fail=self._session_start_failed,
        )

    def _session_started(self, res):
        self.session_id = res["session_id"]
        self.balance_secs = float(res["balance_secs"])
        self._set_balance({"balance_secs": self.balance_secs})
        self.engine.start()
        self.toggle_btn.setText("Stop typing")
        self.toggle_btn.setEnabled(True)

    def _session_start_failed(self, msg):
        self.toggle_btn.setEnabled(True)
        self.status_label.setText(f"Couldn't start: {msg}")
        if "recharge" in msg.lower() or "no hours" in msg.lower():
            self.open_recharge()

    def _stop_typing(self):
        self.engine.stop("Stopping…")

    def _report_active_seconds(self, secs: float) -> bool:
        """Called by the engine thread. Debit on the backend; return True to stop.

        We do the network call synchronously here (we're already off the UI
        thread) and marshal the result back to the UI via signals.
        """
        if not self.session_id:
            return False
        try:
            res = self.api.heartbeat(self.session_id, secs)
        except ApiError as e:
            self._hb_error.emit(e.message)
            raise  # let the engine re-queue the seconds
        self._hb_result.emit(res)
        return bool(res.get("should_stop"))

    def _on_hb_result(self, res):
        self.balance_secs = float(res["balance_secs"])
        self._set_balance({"balance_secs": self.balance_secs})

    def _engine_status(self, text):
        # Marshal to UI thread safely.
        QTimer.singleShot(0, lambda: self.status_label.setText(text))

    def _engine_stopped(self, reason):
        def finish():
            self.toggle_btn.setText("Start typing")
            self.toggle_btn.setEnabled(True)
            self.status_label.setText(reason)
            # Close the session on the backend with any final seconds (0 here;
            # the engine already flushed them via the heartbeat callback).
            if self.session_id:
                sid = self.session_id
                self.session_id = None
                run_async(
                    self,
                    lambda: self.api.stop_session(sid, 0.0),
                    on_done=lambda r: self._set_balance(r) if "balance_secs" in r else None,
                    on_fail=lambda _m: None,
                )
            self.refresh_balance()
        QTimer.singleShot(0, finish)

    # ── recharge ───────────────────────────────────────────────
    def open_recharge(self):
        dlg = RechargeDialog(self.api, self)
        dlg.credited.connect(self.refresh_balance)
        dlg.exec()
        self.refresh_balance()

    # ── hotkey ─────────────────────────────────────────────────
    def _install_hotkey(self, combo: str):
        log.debug("installing global hotkey: %r", combo)
        self._hotkey_ok = True
        try:
            self.hotkeys.start(combo, self._hotkey_fired)
            log.debug("global hotkey listener started")
        except Exception:
            log.exception("hotkey listener failed to start (continuing without it)")
            self._hotkey_ok = False
        self._update_hotkey_label()

    def _update_hotkey_label(self):
        combo = self.cfg.get("hotkey", "")
        idle = float(self.cfg.get("idle_resume_secs", 5.0))
        if getattr(self, "_hotkey_ok", True):
            self.hotkey_label.setText(f"Hotkey: {combo}   ·   Resume after {idle:g}s idle")
        else:
            self.hotkey_label.setText(f"Hotkey '{combo}' invalid — set a new one.")

    def _hotkey_fired(self):
        # Runs on the pynput listener thread; marshal to UI.
        QTimer.singleShot(0, self.toggle_typing)

    def change_hotkey(self):
        dlg = HotkeyDialog(self.cfg["hotkey"], self)
        if dlg.exec() == QDialog.Accepted:
            self.cfg = config.update(hotkey=dlg.result_combo)
            self._install_hotkey(dlg.result_combo)

    def change_idle_resume(self):
        current = float(self.cfg.get("idle_resume_secs", 5.0))
        val, ok = QInputDialog.getDouble(
            self, "Idle resume",
            "Resume auto-typing after this many seconds of no mouse/keyboard "
            "activity:",
            current, 0.5, 120.0, 1,
        )
        if ok:
            self.cfg = config.update(idle_resume_secs=float(val))
            self.engine.set_idle_resume_secs(float(val))
            self._update_hotkey_label()

    # ── logout ─────────────────────────────────────────────────
    def logout(self):
        if self.engine.running:
            self.engine.stop("Logged out.")
        config.update(token="", email="")
        QMessageBox.information(self, "Logged out", "You have been logged out. The app will close.")
        self.close()

    def closeEvent(self, event):
        if self.engine.running:
            self.engine.stop("App closing.")
        self.hotkeys.stop()
        super().closeEvent(event)


# ── entrypoint ──────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("AutoTyper")

    cfg = config.load()
    log.info("AutoTyper starting. api_base=%s  have_token=%s",
             cfg["api_base"], bool(cfg.get("token")))
    api = ApiClient(cfg["api_base"], token=cfg.get("token", ""))

    # If we have a token, verify it by fetching balance; else show login.
    def ensure_logged_in() -> bool:
        if not api.token:
            return _do_login(api)
        try:
            api.balance()
            return True
        except ApiError:
            return _do_login(api)

    if not ensure_logged_in():
        return 0

    cfg = config.load()  # token may have been updated by login
    api.token = cfg["token"]
    log.debug("main: constructing MainWindow")
    win = MainWindow(api, cfg)
    log.debug("main: MainWindow constructed, showing")
    win.show()
    log.debug("main: entering event loop")
    return app.exec()


def _do_login(api: ApiClient) -> bool:
    dlg = LoginDialog(api)
    if dlg.exec() == QDialog.Accepted:
        cfg = config.load()
        api.token = cfg["token"]
        return True
    return False


if __name__ == "__main__":
    sys.exit(main())
