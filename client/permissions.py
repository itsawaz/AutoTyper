"""macOS Accessibility permission helpers.

Simulating keystrokes on macOS requires the running app to be trusted for
Accessibility. Without it the OS silently DISCARDS posted key events — no
exception, no error, nothing typed. So we check up front and tell the user
instead of appearing to work while doing nothing.
"""
from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger("autotyper.perms")

_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security"
    "?Privacy_Accessibility"
)


def is_macos() -> bool:
    return sys.platform == "darwin"


def accessibility_trusted() -> bool | None:
    """True/False on macOS; None when the check isn't applicable/available."""
    if not is_macos():
        return None
    try:
        from ApplicationServices import AXIsProcessTrusted
        return bool(AXIsProcessTrusted())
    except Exception:
        log.warning("could not query Accessibility trust", exc_info=True)
        return None


def request_accessibility() -> bool | None:
    """Ask macOS to show the 'grant Accessibility' prompt for this app."""
    if not is_macos():
        return None
    try:
        from ApplicationServices import (AXIsProcessTrustedWithOptions,
                                         kAXTrustedCheckOptionPrompt)
        return bool(AXIsProcessTrustedWithOptions(
            {kAXTrustedCheckOptionPrompt: True}))
    except Exception:
        log.warning("could not request Accessibility", exc_info=True)
        return None


def open_accessibility_settings() -> None:
    """Open System Settings at Privacy & Security → Accessibility."""
    if not is_macos():
        return
    try:
        subprocess.Popen(["open", _SETTINGS_URL])
    except Exception:
        log.warning("could not open System Settings", exc_info=True)


def host_app_hint() -> str:
    """Which app the user must tick in the Accessibility list.

    When running from source that's the terminal (or IDE) that launched Python,
    not 'AutoTyper' — a common source of confusion.
    """
    if getattr(sys, "frozen", False):
        return "AutoTyper"
    return "your terminal app (the one you ran AutoTyper from)"
