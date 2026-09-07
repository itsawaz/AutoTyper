# AutoTyper 🤖⌨️

A Python script that simulates human-like typing activity on your terminal by cycling through RPGLE/CL commands with realistic speed, typos, corrections, and automatic breaks.

## Features

- **Human-like typing** — random dwell times, occasional typos that self-correct
- **Auto-breaks** — automatically pauses every 15–20 minutes for 2–5 minutes
- **Hotkey toggle** — `Ctrl + Shift + 1` to turn ON/OFF instantly
- **SQLite session tracking** — logs every ON→OFF session (start time, end time, active duration)
- **Daily usage limit** — auto-closes after **6 hours** of active time per day
  - If you've hit the limit and relaunch, it warns you and offers a **+1 hour extension**
  - Each subsequent launch past the limit grants another 1-hour increment
- **Live console counter** — a status bar that updates every second while active:

  ```
  ⏱  Session 4m 32s  │  Today 41m 12s/6h 0m 0s  │  [████░░░░░░░░░░░░░░░░] 11%  │  -5h 18m 48s
  ```

## Requirements

- Python 3.8+
- macOS (uses `pyautogui` and `pynput`)

## Installation

```bash
# Clone the repo
git clone <repo-url>
cd AutoTyper

# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install pyautogui pynput
```

## Usage

```bash
source venv/bin/activate
Y
```

Then press **Ctrl + Shift + 1** to start/stop typing.

> **Note:** On first run you may need to grant Accessibility permissions to Terminal in  
> *System Settings → Privacy & Security → Accessibility*.

## Database

Session history is stored in `autotyper_sessions.db` (SQLite) in the project directory.  
This file is excluded from git via `.gitignore` — it contains your personal usage data.

Schema:

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER | Auto-incremented session ID |
| `start_time` | TEXT | Local datetime when toggled ON |
| `end_time` | TEXT | Local datetime when toggled OFF |
| `duration_secs` | REAL | Active seconds for the session |
| `day` | TEXT | `YYYY-MM-DD` calendar day (12am–12am) |

## Configuration

Edit the constants at the top of `auto_typer.py`:

| Constant | Default | Description |
|---|---|---|
| `MIN_TYPING_BEFORE_BREAK` | 15 min | Minimum active time before a break |
| `MAX_TYPING_BEFORE_BREAK` | 20 min | Maximum active time before a break |
| `MIN_BREAK_DURATION` | 2 min | Minimum break length |
| `MAX_BREAK_DURATION` | 5 min | Maximum break length |
| `HARD_LIMIT_SECS` | 6 hours | Daily active-time limit |
| `EXTENSION_SECS` | 1 hour | Extra time granted per override |

## License

MIT
