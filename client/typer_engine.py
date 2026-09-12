"""Human-like typing engine, refactored from the original auto_typer.py.

Differences from the original single-file script:
- No local SQLite and no local daily-limit gate. Time accounting and limits are
  enforced by the backend (the client just reports elapsed active seconds).
- Runs as a controllable object (start/stop) instead of a CLI with a hardcoded
  hotkey, so the GUI owns the lifecycle.
- Emits periodic "active seconds" via a heartbeat callback. The callback returns
  whether the engine must stop (balance exhausted).

The actual keystroke simulation (dwell times, typos, auto-breaks) is preserved.
"""
from __future__ import annotations

import random
import sys
import threading
import time
from typing import Callable

import pyautogui

pyautogui.FAILSAFE = False


# ── user-activity detection (polling, no event taps) ───────────
# We deliberately do NOT use pynput listeners here. On macOS a global event tap
# started from inside a GUI app conflicts with the toolkit's main run loop and
# aborts the whole process (SIGTRAP) — which can't be caught in Python. Polling
# uses only read-only APIs, needs no special permission, and cannot crash.
def _hid_idle_seconds():
    """Seconds since the last real user input, or None if unavailable.

    macOS   : CGEventSourceSecondsSinceLastEventType (HID = physical devices)
    Windows : GetLastInputInfo
    """
    if sys.platform == "darwin":
        try:
            from Quartz import (CGEventSourceSecondsSinceLastEventType,
                                kCGEventSourceStateHIDSystemState,
                                kCGAnyInputEventType)
            return float(CGEventSourceSecondsSinceLastEventType(
                kCGEventSourceStateHIDSystemState, kCGAnyInputEventType))
        except Exception:
            return None
    if sys.platform == "win32":
        try:
            import ctypes

            class _LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint),
                            ("dwTime", ctypes.c_uint)]

            info = _LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                millis = ctypes.windll.kernel32.GetTickCount() - info.dwTime
                return millis / 1000.0
        except Exception:
            return None
    return None

# Break configuration (seconds): 15-20 min typing, then 2-5 min break.
MIN_TYPING_BEFORE_BREAK = 15 * 60
MAX_TYPING_BEFORE_BREAK = 20 * 60
MIN_BREAK_DURATION = 2 * 60
MAX_BREAK_DURATION = 5 * 60

# How often to report active time to the backend, in seconds.
HEARTBEAT_INTERVAL = 20.0

# Auto-pause behaviour: when the real user moves the mouse or types, pause the
# auto-typer and resume after this many seconds of no user activity.
DEFAULT_IDLE_RESUME_SECS = 5.0

# When the engine sends a synthetic keystroke, input observed within this many
# seconds is assumed to be our own and is ignored by the activity monitor (so the
# engine doesn't detect itself and pause forever).
_SYNTHETIC_GUARD_SECS = 0.6

# How often the activity monitor polls for user input.
ACTIVITY_POLL_INTERVAL = 0.2

RPGLE_CL_COMMANDS = [
    "CRTBNDRPG PGM(MYPGM) SRCFILE(QRPGLESRC)",
    "DSPPGM PGM(MYPGM)", "WRKSPLF", "CALL PGM(MYPGM)",
    "CHGJOB LOG(4 00 *SECLVL)", "DSPJOB", "WRKACTJOB", "STRSEU",
    "CRTPF FILE(MYFILE) SRCFILE(QDDSSRC)", "SNDPGMMSG MSG('Hello World')",
    "WRKOBJ OBJ(MYPGM) OBJTYPE(*PGM)", "DSPSYSVAL SYSVAL(QDATE)",
    "WRKUSRJOB USER(*ALL)", "STRSQL", "WRKSYSVAL", "ADDLIBLE LIB(MYLIB)",
    "RMVLIBLE LIB(MYLIB)", "CHGLIBL LIBL(QTEMP QGPL MYLIB)",
    "CPYF FROMFILE(FILEA) TOFILE(FILEB) MBROPT(*ADD)", "CLRPFM FILE(MYFILE)",
    "DSPDBR FILE(MYFILE)", "DSPFD FILE(MYFILE)", "DSPFFD FILE(MYFILE)",
    "CRTRPGMOD MODULE(MYMOD) SRCFILE(QRPGLESRC)", "CRTPGM PGM(MYPGM) MODULE(MYMOD)",
    "STRDBG PGM(MYPGM) UPDPROD(*YES)", "ENDDBG", "WRKMSG",
    "SNDMSG MSG('System update completed') TOUSR(*SYSOPR)", "DSPJOBLOG",
    "DSPMSG MSGQ(*WRKUSR)", "SIGNOFF",
]

_SHIFT_CHARS = {
    **{c: c.lower() for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
    '(': '9', ')': '0', '*': '8', '_': '-', ':': ';', '"': "'",
}


def _type_char(char: str, mark_synthetic: Callable[[], None] | None = None) -> None:
    dwell = random.uniform(0.05, 0.12)
    if mark_synthetic:
        mark_synthetic()
    if char in _SHIFT_CHARS:
        key = _SHIFT_CHARS[char]
        pyautogui.keyDown('shift'); pyautogui.keyDown(key)
        time.sleep(dwell)
        pyautogui.keyUp(key); pyautogui.keyUp('shift')
    else:
        key = 'space' if char == ' ' else char
        pyautogui.keyDown(key)
        time.sleep(dwell)
        pyautogui.keyUp(key)
    if mark_synthetic:
        mark_synthetic()


def _backspace(mark_synthetic: Callable[[], None] | None = None) -> None:
    dwell = random.uniform(0.05, 0.12)
    if mark_synthetic:
        mark_synthetic()
    pyautogui.keyDown('backspace')
    time.sleep(dwell)
    pyautogui.keyUp('backspace')
    if mark_synthetic:
        mark_synthetic()


class TyperEngine:
    """Controllable typing engine.

    Callbacks (all optional):
      on_active_seconds(secs) -> bool
          Called ~every HEARTBEAT_INTERVAL with the active seconds elapsed since
          the previous call. Return True to request an immediate stop (e.g. the
          backend says the balance is exhausted).
      on_status(text)
          Human-readable status line for the GUI.
      on_stopped(reason)
          Called once when the engine fully stops.
    """

    def __init__(
        self,
        on_active_seconds: Callable[[float], bool] | None = None,
        on_status: Callable[[str], None] | None = None,
        on_stopped: Callable[[str], None] | None = None,
        idle_resume_secs: float = DEFAULT_IDLE_RESUME_SECS,
    ):
        self._on_active_seconds = on_active_seconds
        self._on_status = on_status
        self._on_stopped = on_stopped
        self._idle_resume_secs = max(0.5, float(idle_resume_secs))

        self._running = False
        self._on_break = False
        self._worker: threading.Thread | None = None
        self._hb_thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # Active-time accounting for heartbeats.
        self._segment_start = 0.0          # wall-clock start of current active run
        self._unreported_secs = 0.0        # active secs accrued but not yet sent

        # ── user-activity auto-pause state ──────────────────────
        # Timestamp of the last REAL user input (mouse move / user keypress).
        self._last_user_activity = 0.0
        # Window during which keyboard events are treated as our own synthetic
        # input and ignored by the monitor.
        self._synthetic_until = 0.0
        self._user_paused = False
        self._monitor_thread: threading.Thread | None = None

    # ── public API ─────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._running

    def set_idle_resume_secs(self, secs: float) -> None:
        """Update the idle-before-resume threshold (seconds). Takes effect live."""
        self._idle_resume_secs = max(0.5, float(secs))

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._on_break = False
        self._user_paused = False
        self._segment_start = time.time()
        self._unreported_secs = 0.0
        self._last_user_activity = 0.0
        self._synthetic_until = 0.0
        self._start_activity_monitor()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._hb_thread.start()
        self._status("Typing started.")

    def stop(self, reason: str = "Stopped by user.") -> None:
        if not self._running:
            return
        # Flush the final unreported active time before stopping.
        self._accrue_active_time()
        self._running = False
        self._on_break = False
        self._stop_activity_monitor()
        if self._on_active_seconds and self._unreported_secs > 0:
            try:
                self._on_active_seconds(self._unreported_secs)
            except Exception:
                pass
            self._unreported_secs = 0.0
        self._status(reason)
        if self._on_stopped:
            self._on_stopped(reason)

    # ── internal ───────────────────────────────────────────────
    def _status(self, text: str) -> None:
        if self._on_status:
            try:
                self._on_status(text)
            except Exception:
                pass

    def _accrue_active_time(self) -> None:
        """Move wall-clock elapsed (when active, not on break) into unreported."""
        with self._lock:
            if self._running and not self._on_break and self._segment_start > 0:
                now = time.time()
                self._unreported_secs += now - self._segment_start
                self._segment_start = now

    # ── user-activity auto-pause ────────────────────────────────
    def _mark_synthetic(self) -> None:
        """Called immediately before/after the engine emits a keystroke so the
        activity monitor ignores our own synthetic keyboard events."""
        self._synthetic_until = time.time() + _SYNTHETIC_GUARD_SECS

    def _start_activity_monitor(self) -> None:
        """Start the polling watcher thread (no event taps — see module notes)."""
        self._monitor_thread = threading.Thread(
            target=self._activity_poll_loop, daemon=True, name="activity-monitor")
        self._monitor_thread.start()

    def _stop_activity_monitor(self) -> None:
        # The loop exits on its own when self._running goes False.
        self._monitor_thread = None

    def _activity_poll_loop(self) -> None:
        """Detect real user input by polling, every ACTIVITY_POLL_INTERVAL.

        Two independent signals:
          1. Mouse movement — the engine never moves the mouse, so any change in
             the cursor position is unambiguously the user.
          2. System idle time — resets on any input including our own synthetic
             keystrokes, so we only trust it outside the synthetic guard window.
        """
        last_pos = None
        while self._running:
            time.sleep(ACTIVITY_POLL_INTERVAL)
            if not self._running:
                break

            # 1) mouse movement
            try:
                pos = pyautogui.position()
            except Exception:
                pos = None
            if pos is not None and last_pos is not None and pos != last_pos:
                self._last_user_activity = time.time()
            if pos is not None:
                last_pos = pos

            # 2) keyboard / clicks via system idle time, ignoring our own output
            idle = _hid_idle_seconds()
            if (idle is not None
                    and idle < ACTIVITY_POLL_INTERVAL * 2
                    and time.time() >= self._synthetic_until):
                self._last_user_activity = time.time()

    def _user_is_active(self) -> bool:
        if self._last_user_activity <= 0:
            return False
        return (time.time() - self._last_user_activity) < self._idle_resume_secs

    def _wait_out_user_activity(self) -> None:
        """If the user is active, pause typing (and time accrual) until they've
        been idle for `idle_resume_secs`, then resume. Returns immediately if the
        user isn't active or the engine is stopping."""
        if not self._user_is_active() or not self._running:
            return

        # Enter paused state: stop counting time and tell the GUI.
        self._accrue_active_time()
        with self._lock:
            self._segment_start = 0.0
        self._user_paused = True
        self._status("Paused — user activity detected.")

        while self._running and self._user_is_active():
            time.sleep(0.2)

        if not self._running:
            return

        # Resume: restart the active-time clock and clear the guard.
        with self._lock:
            self._segment_start = time.time()
        self._synthetic_until = 0.0
        self._user_paused = False
        self._status("Resumed typing after idle.")

    def _heartbeat_loop(self) -> None:
        while self._running:
            time.sleep(HEARTBEAT_INTERVAL)
            if not self._running:
                break
            self._accrue_active_time()
            with self._lock:
                to_send = self._unreported_secs
                self._unreported_secs = 0.0
            if to_send <= 0 or not self._on_active_seconds:
                continue
            try:
                should_stop = self._on_active_seconds(to_send)
            except Exception:
                # Network hiccup — put the time back so we retry next tick.
                with self._lock:
                    self._unreported_secs += to_send
                should_stop = False
            if should_stop:
                self.stop("Out of hours — stopped automatically.")
                break

    def _run(self) -> None:
        pool = list(RPGLE_CL_COMMANDS)
        random.shuffle(pool)
        index = 0
        typing_since_break = 0.0
        next_break = random.uniform(MIN_TYPING_BEFORE_BREAK, MAX_TYPING_BEFORE_BREAK)

        while self._running:
            # ── automatic break ────────────────────────────────
            if typing_since_break >= next_break:
                self._accrue_active_time()
                self._on_break = True
                with self._lock:
                    self._segment_start = 0.0
                dur = random.uniform(MIN_BREAK_DURATION, MAX_BREAK_DURATION)
                self._status(f"On break for {dur/60:.1f} min…")
                bstart = time.time()
                while self._running and time.time() - bstart < dur:
                    time.sleep(1.0)
                self._on_break = False
                if self._running:
                    with self._lock:
                        self._segment_start = time.time()
                typing_since_break = 0.0
                next_break = random.uniform(MIN_TYPING_BEFORE_BREAK, MAX_TYPING_BEFORE_BREAK)
                if self._running:
                    self._status("Typing…")

            # ── pause if the real user is doing something ───────
            self._wait_out_user_activity()

            if not self._running:
                break

            # ── type one command ───────────────────────────────
            cycle_start = time.time()
            if index >= len(pool):
                random.shuffle(pool)
                index = 0
            cmd = pool[index]
            index += 1

            for ch in cmd:
                if not self._running:
                    break
                # User grabbed control mid-command — pause, then continue typing
                # the remaining characters once they go idle.
                if self._user_is_active():
                    self._wait_out_user_activity()
                    if not self._running:
                        break
                if ch != ' ' and random.random() < 0.04:
                    typo = random.choice("abcdefghijklmnopqrstuvwxyz")
                    _type_char(typo, self._mark_synthetic)
                    time.sleep(random.uniform(0.15, 0.35))
                    _backspace(self._mark_synthetic)
                    time.sleep(random.uniform(0.1, 0.2))
                _type_char(ch, self._mark_synthetic)
                if ch in (' ', '(', ')'):
                    time.sleep(random.uniform(0.2, 0.45))
                else:
                    time.sleep(random.uniform(0.04, 0.22))

            if self._running:
                time.sleep(random.uniform(1.0, 2.5))
            if self._running:
                for _ in range(len(cmd)):
                    if not self._running:
                        break
                    if self._user_is_active():
                        self._wait_out_user_activity()
                        if not self._running:
                            break
                    _backspace(self._mark_synthetic)
                    time.sleep(random.uniform(0.03, 0.08))
            if self._running:
                time.sleep(random.uniform(2.0, 5.0))

            # Only count time actually spent typing toward the auto-break clock
            # (paused time was excluded from active-time accrual too).
            typing_since_break += time.time() - cycle_start
