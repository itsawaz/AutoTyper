import time
import random
import threading
import sqlite3
import os
import sys
from datetime import datetime
import pyautogui
from pynput import keyboard

# ──────────────────────────────────────────────
# Break configurations (in seconds)
# Default: 15-20 min of typing before a 2-5 min break
# ──────────────────────────────────────────────
MIN_TYPING_BEFORE_BREAK = 15 * 60
MAX_TYPING_BEFORE_BREAK = 20 * 60

MIN_BREAK_DURATION = 2 * 60
MAX_BREAK_DURATION = 5 * 60

# ──────────────────────────────────────────────
# Daily limit settings
# ──────────────────────────────────────────────
HARD_LIMIT_SECS  = 6 * 3600   # 6 hours base daily limit
EXTENSION_SECS   = 1 * 3600   # each user override adds 1 hour

# Effective limit for this process run (may be raised by user confirmation)
daily_limit_secs = HARD_LIMIT_SECS

# ──────────────────────────────────────────────
# Global state
# ──────────────────────────────────────────────
is_running = False
active_typing_time = 0.0          # seconds of active typing in current ON window
current_session_id = None         # row-id of the active session in DB

# Controller to simulate key presses
controller = keyboard.Controller()

# ──────────────────────────────────────────────
# Database helpers
# ──────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autotyper_sessions.db")

def init_db():
    """Create the SQLite DB and sessions table if they don't exist."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time      TEXT NOT NULL,   -- ISO-8601 local datetime when turned ON
            end_time        TEXT,            -- ISO-8601 local datetime when turned OFF (NULL if still running)
            duration_secs   REAL,            -- total active seconds for this session (set on OFF)
            day             TEXT NOT NULL    -- YYYY-MM-DD calendar day of start_time (12am-12am window)
        )
    """)
    con.commit()
    con.close()
    print(f"[DB] Database ready at: {DB_PATH}")


def _now_str() -> str:
    """Return current local datetime as an ISO-8601 string."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today_str() -> str:
    """Return today's date as YYYY-MM-DD (local time, 12am-12am boundary)."""
    return datetime.now().strftime("%Y-%m-%d")


def db_start_session() -> int:
    """Insert a new session row and return its id."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO sessions (start_time, day) VALUES (?, ?)",
        (_now_str(), _today_str())
    )
    row_id = cur.lastrowid
    con.commit()
    con.close()
    return row_id


def db_end_session(session_id: int, duration_secs: float):
    """Update the session row with end_time and duration."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "UPDATE sessions SET end_time = ?, duration_secs = ? WHERE id = ?",
        (_now_str(), duration_secs, session_id)
    )
    con.commit()
    con.close()


def db_daily_total(day: str) -> float:
    """Return total active seconds for the given YYYY-MM-DD day."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(duration_secs), 0) FROM sessions WHERE day = ? AND duration_secs IS NOT NULL",
        (day,)
    )
    total = cur.fetchone()[0]
    con.close()
    return total


def db_print_daily_summary(day: str):
    """Print a summary of all sessions for the given day."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT start_time, end_time, duration_secs FROM sessions WHERE day = ? ORDER BY start_time",
        (day,)
    )
    rows = cur.fetchall()
    con.close()

    print(f"\n[DB] ── Sessions for {day} ──────────────────────────")
    if not rows:
        print("  (no sessions recorded yet)")
    else:
        for i, (start, end, dur) in enumerate(rows, 1):
            end_str  = end  if end  else "still running"
            dur_str  = _fmt_duration(dur) if dur is not None else "—"
            print(f"  #{i:02d}  {start}  →  {end_str}  ({dur_str})")

    total = db_daily_total(day)
    print(f"  Total active time today: {_fmt_duration(total)}")
    print(f"[DB] ────────────────────────────────────────────────\n")


def _fmt_duration(secs) -> str:
    """Format seconds into a human-readable h m s string."""
    if secs is None:
        return "—"
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    elif m:
        return f"{m}m {s}s"
    else:
        return f"{s}s"


# ──────────────────────────────────────────────
# Daily-limit helpers
# ──────────────────────────────────────────────
def get_current_daily_total() -> float:
    """Completed DB sessions today + the active session so far."""
    return db_daily_total(_today_str()) + active_typing_time


def check_daily_limit_on_start() -> bool:
    """
    Called once at startup.
    Returns True  → OK to proceed.
    Returns False → user declined; caller should exit.
    Raises daily_limit_secs if user grants an extension.
    """
    global daily_limit_secs
    today_total = db_daily_total(_today_str())

    if today_total < HARD_LIMIT_SECS:
        remaining = HARD_LIMIT_SECS - today_total
        print(f"[Limit] Today's usage: {_fmt_duration(today_total)} / {_fmt_duration(HARD_LIMIT_SECS)}  "
              f"— {_fmt_duration(remaining)} remaining.")
        return True

    # Already at or past the base limit
    overrun = today_total - HARD_LIMIT_SECS
    print()
    print("  ╔══════════════════════════════════════════════════════╗")
    print("  ║  ⚠️   DAILY LIMIT REACHED                            ║")
    print(f"  ║  You have already used {_fmt_duration(today_total)} today.           ║")
    print(f"  ║  Base limit  : {_fmt_duration(HARD_LIMIT_SECS)}                             ║")
    print(f"  ║  Overrun     : {_fmt_duration(overrun)}                              ║")
    print("  ║                                                      ║")
    print("  ║  Continuing may strain your eyes and hands.          ║")
    print("  ║  The script will auto-close after +1 hour if you do. ║")
    print("  ╚══════════════════════════════════════════════════════╝")
    print()

    try:
        resp = input("  Continue for 1 more hour? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        resp = "n"

    if resp == "y":
        daily_limit_secs = today_total + EXTENSION_SECS
        print(f"[Limit] Extended. Script will auto-close when total reaches "
              f"{_fmt_duration(daily_limit_secs)} (in ~{_fmt_duration(EXTENSION_SECS)}).")
        return True
    else:
        print("[Limit] Exiting. Take a break! 🌿")
        return False


def _force_stop_and_exit(reason: str):
    """Save current session, print summary, then hard-exit the process."""
    global is_running, current_session_id, active_typing_time

    is_running = False   # stop the typing worker immediately

    if current_session_id is not None:
        db_end_session(current_session_id, active_typing_time)
        print(f"[DB] Session #{current_session_id} saved — "
              f"active time: {_fmt_duration(active_typing_time)}")
        current_session_id = None

    db_print_daily_summary(_today_str())
    print(f"[Limit] {reason}")
    print("[Limit] Closing script... Goodbye! 👋")
    time.sleep(0.5)   # let stdout flush
    os._exit(0)


def limit_watcher():
    """Background thread: monitors total daily usage and auto-closes when limit hit."""
    while True:
        time.sleep(2)   # check every 2 seconds
        if is_running:
            total = get_current_daily_total()
            if total >= daily_limit_secs:
                print(f"\n[Limit] 🚨 Daily limit of {_fmt_duration(daily_limit_secs)} reached! "
                      f"Auto-stopping...")
                _force_stop_and_exit(
                    f"Total usage {_fmt_duration(total)} hit the daily cap of "
                    f"{_fmt_duration(daily_limit_secs)}.")
                return   # unreachable but clean


def live_counter():
    """
    Background thread: every second, overwrites a single console line with
    a live status showing session time, daily total, progress bar, and
    time remaining before the daily limit.
    """
    _LINE_WIDTH = 80
    last_active = False

    while True:
        time.sleep(1)

        if is_running:
            session_secs = active_typing_time
            # Read only completed sessions from DB (cheap indexed query)
            completed    = db_daily_total(_today_str())
            daily_total  = completed + session_secs
            remaining    = max(0.0, daily_limit_secs - daily_total)
            pct          = min(100, int(daily_total / max(daily_limit_secs, 1) * 100))

            # 20-char ASCII progress bar
            filled = int(pct / 5)
            bar    = "\u2588" * filled + "\u2591" * (20 - filled)

            # Colour the bar red when above 90%, yellow above 75%
            if pct >= 90:
                bar_coloured = f"\033[91m[{bar}]\033[0m"
            elif pct >= 75:
                bar_coloured = f"\033[93m[{bar}]\033[0m"
            else:
                bar_coloured = f"\033[92m[{bar}]\033[0m"

            line = (
                f"  \u23f1  Session {_fmt_duration(session_secs)}"
                f"  \u2502  Today {_fmt_duration(daily_total)}/{_fmt_duration(daily_limit_secs)}"
                f"  \u2502  {bar_coloured} {pct}%"
                f"  \u2502  -{_fmt_duration(remaining)}"
            )

            # Pad to overwrite any leftover chars from previous longer lines
            sys.stdout.write("\r" + line + " " * max(0, _LINE_WIDTH - len(line)))
            sys.stdout.flush()
            last_active = True

        elif last_active:
            # Typer was just stopped — clear the counter line
            sys.stdout.write("\r" + " " * _LINE_WIDTH + "\r")
            sys.stdout.flush()
            last_active = False


# ──────────────────────────────────────────────
# RPGLE / CL command list
# ──────────────────────────────────────────────
rpgle_cl_commands = [
    "CRTBNDRPG PGM(MYPGM) SRCFILE(QRPGLESRC)",
    "DSPPGM PGM(MYPGM)",
    "WRKSPLF",
    "CALL PGM(MYPGM)",
    "CHGJOB LOG(4 00 *SECLVL)",
    "DSPJOB",
    "WRKACTJOB",
    "STRSEU",
    "CRTPF FILE(MYFILE) SRCFILE(QDDSSRC)",
    "SNDPGMMSG MSG('Hello World')",
    "WRKOBJ OBJ(MYPGM) OBJTYPE(*PGM)",
    "DSPSYSVAL SYSVAL(QDATE)",
    "WRKUSRJOB USER(*ALL)",
    "STRSQL",
    "WRKSYSVAL",
    "ADDLIBLE LIB(MYLIB)",
    "RMVLIBLE LIB(MYLIB)",
    "CHGLIBL LIBL(QTEMP QGPL MYLIB)",
    "CPYF FROMFILE(FILEA) TOFILE(FILEB) MBROPT(*ADD)",
    "CLRPFM FILE(MYFILE)",
    "DSPDBR FILE(MYFILE)",
    "DSPFD FILE(MYFILE)",
    "DSPFFD FILE(MYFILE)",
    "CRTRPGMOD MODULE(MYMOD) SRCFILE(QRPGLESRC)",
    "CRTPGM PGM(MYPGM) MODULE(MYMOD)",
    "STRDBG PGM(MYPGM) UPDPROD(*YES)",
    "ENDDBG",
    "WRKMSG",
    "SNDMSG MSG('System update completed') TOUSR(*SYSOPR)",
    "DSPJOBLOG",
    "DSPMSG MSGQ(*WRKUSR)",
    "SIGNOFF"
]


# ──────────────────────────────────────────────
# Typing helpers
# ──────────────────────────────────────────────
def human_type_char(char):
    """Types a single character with a simulated human-like dwell time."""
    dwell_time = random.uniform(0.05, 0.12)
    shift_chars = {
        'A': 'a', 'B': 'b', 'C': 'c', 'D': 'd', 'E': 'e', 'F': 'f',
        'G': 'g', 'H': 'h', 'I': 'i', 'J': 'j', 'K': 'k', 'L': 'l',
        'M': 'm', 'N': 'n', 'O': 'o', 'P': 'p', 'Q': 'q', 'R': 'r',
        'S': 's', 'T': 't', 'U': 'u', 'V': 'v', 'W': 'w', 'X': 'x',
        'Y': 'y', 'Z': 'z',
        '(': '9', ')': '0', '*': '8', '_': '-', ':': ';', '"': "'"
    }

    if char in shift_chars:
        key = shift_chars[char]
        pyautogui.keyDown('shift')
        pyautogui.keyDown(key)
        time.sleep(dwell_time)
        pyautogui.keyUp(key)
        pyautogui.keyUp('shift')
    else:
        key = 'space' if char == ' ' else char
        pyautogui.keyDown(key)
        time.sleep(dwell_time)
        pyautogui.keyUp(key)


def human_press_backspace():
    """Presses backspace with a simulated human-like dwell time."""
    dwell_time = random.uniform(0.05, 0.12)
    pyautogui.keyDown('backspace')
    time.sleep(dwell_time)
    pyautogui.keyUp('backspace')


# ──────────────────────────────────────────────
# Typing worker thread
# ──────────────────────────────────────────────
def typing_worker():
    """Worker thread that handles typing/backspacing with automatic breaks."""
    global is_running, active_typing_time

    cycle_start = None        # tracks start of each command cycle (for partial-cycle accounting)
    shuffled_pool = list(rpgle_cl_commands)
    random.shuffle(shuffled_pool)
    index = 0

    next_break_threshold = random.uniform(MIN_TYPING_BEFORE_BREAK, MAX_TYPING_BEFORE_BREAK)
    print(f"[Timer] Next automatic break after {next_break_threshold / 60:.2f} min of active typing.")

    while True:
        if is_running:
            # ── Automatic break check ──────────────────────────
            if active_typing_time >= next_break_threshold:
                break_duration = random.uniform(MIN_BREAK_DURATION, MAX_BREAK_DURATION)
                print(f"\n[Break] Taking an automatic break for {break_duration / 60:.2f} minutes...")

                break_start = time.time()
                while time.time() - break_start < break_duration:
                    if not is_running:
                        break
                    time.sleep(1.0)

                active_typing_time = 0.0
                next_break_threshold = random.uniform(MIN_TYPING_BEFORE_BREAK, MAX_TYPING_BEFORE_BREAK)

                if not is_running:
                    print("[Break] Break cancelled because Auto-Typer was paused.")
                    continue
                else:
                    print(f"[Break] Done! Resuming. Next break in {next_break_threshold / 60:.2f} min.")

            # ── Type one command ───────────────────────────────
            cycle_start = time.time()
            partial_accounted = False   # ensure we don't double-count

            if index >= len(shuffled_pool):
                random.shuffle(shuffled_pool)
                index = 0

            cmd = shuffled_pool[index]
            index += 1

            for char in cmd:
                if not is_running:
                    break

                if char != ' ' and random.random() < 0.04:
                    typo = random.choice("abcdefghijklmnopqrstuvwxyz")
                    human_type_char(typo)
                    time.sleep(random.uniform(0.15, 0.35))
                    human_press_backspace()
                    time.sleep(random.uniform(0.1, 0.2))

                human_type_char(char)

                if char in (' ', '(', ')'):
                    time.sleep(random.uniform(0.2, 0.45))
                else:
                    time.sleep(random.uniform(0.04, 0.22))

            if is_running:
                time.sleep(random.uniform(1.0, 2.5))

            if is_running:
                for _ in range(len(cmd)):
                    if not is_running:
                        break
                    human_press_backspace()
                    time.sleep(random.uniform(0.03, 0.08))

            if is_running:
                time.sleep(random.uniform(2.0, 5.0))

            cycle_duration = time.time() - cycle_start
            active_typing_time += cycle_duration
            partial_accounted = True
            cycle_start = None

        else:
            # If we were mid-cycle when toggled off, count the partial time now
            if cycle_start is not None:
                active_typing_time += time.time() - cycle_start
                cycle_start = None
            time.sleep(0.1)


# ──────────────────────────────────────────────
# Toggle handler
# ──────────────────────────────────────────────
def toggle_typing():
    """Toggles the running state and records session start/end in the DB."""
    global is_running, active_typing_time, current_session_id

    is_running = not is_running

    if is_running:
        # ── Turning ON ────────────────────────────────────────
        active_typing_time = 0.0
        current_session_id = db_start_session()
        print(f"\n[AutoTyper] ▶ STARTED  at {_now_str()}  (session #{current_session_id})")
        print("[Timer] Active typing session timer reset.")

    else:
        # ── Turning OFF ───────────────────────────────────────
        print(f"\n[AutoTyper] ■ STOPPED  at {_now_str()}")
        if current_session_id is not None:
            db_end_session(current_session_id, active_typing_time)
            print(f"[DB] Session #{current_session_id} saved — active time: {_fmt_duration(active_typing_time)}")
            current_session_id = None

        # Print today's full summary
        db_print_daily_summary(_today_str())


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────
def main():
    init_db()
    print("Starting Auto-Typer script...")
    print("Press Control + Shift + 1 to toggle ON/OFF.\n")

    # Show today's history from any previous runs
    db_print_daily_summary(_today_str())

    # ── Daily-limit gate ──────────────────────────────────
    if not check_daily_limit_on_start():
        sys.exit(0)

    # ── Start background threads ──────────────────────────
    worker_thread = threading.Thread(target=typing_worker, daemon=True)
    worker_thread.start()

    watcher_thread = threading.Thread(target=limit_watcher, daemon=True)
    watcher_thread.start()

    counter_thread = threading.Thread(target=live_counter, daemon=True)
    counter_thread.start()

    hotkey_combination = '<ctrl>+<shift>+1'
    with keyboard.GlobalHotKeys({hotkey_combination: toggle_typing}) as listener:
        listener.join()


if __name__ == '__main__':
    main()
