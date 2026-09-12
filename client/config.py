"""Local client configuration and persisted settings.

Stored in the user's home dir (not the app bundle), so it survives reinstalls
and works from a read-only packaged executable. Holds only non-sensitive local
prefs plus the logged-in user's own session token — never any server secret.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# Backend base URL. Overridable via env so the same build can point at a local
# dev server or the deployed Vercel URL without recompiling.
DEFAULT_API_BASE = os.environ.get(
    "AUTOTYPER_API_BASE", "http://127.0.0.1:8000"
)

APP_DIR = Path.home() / ".autotyper"
CONFIG_PATH = APP_DIR / "config.json"

DEFAULTS = {
    "api_base": DEFAULT_API_BASE,
    "hotkey": "<ctrl>+<shift>+1",  # pynput GlobalHotKeys format
    "token": "",                    # JWT for the logged-in user
    "email": "",
    # Seconds of no user activity before auto-typing resumes after the user
    # moves the mouse or types.
    "idle_resume_secs": 5.0,
}


def _ensure_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


def load() -> dict:
    _ensure_dir()
    if not CONFIG_PATH.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in data.items() if k in DEFAULTS})
    return merged


def save(cfg: dict) -> None:
    _ensure_dir()
    to_write = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    CONFIG_PATH.write_text(json.dumps(to_write, indent=2))


def update(**kwargs) -> dict:
    cfg = load()
    cfg.update(kwargs)
    save(cfg)
    return cfg
