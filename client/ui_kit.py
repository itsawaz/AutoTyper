"""Tkinter UI toolkit: dark theme, palette, and reusable styled widgets.

Tkinter is used instead of Qt because it ships with Python and needs no native
plugin loading, which makes the app launch reliably on any machine.

Everything here is presentation only — no app logic.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk


class Palette:
    """Dark, modern palette."""
    bg = "#12141a"          # window background
    surface = "#1b1f27"     # cards / panels
    surface_alt = "#232833"  # inputs, hover
    border = "#2e3440"
    text = "#eceff4"
    text_dim = "#9aa4b2"
    text_faint = "#6b7482"
    accent = "#4f8cff"       # primary action
    accent_hover = "#6b9dff"
    accent_press = "#3d76e0"
    success = "#3ddc84"
    warning = "#ffb454"
    danger = "#ff5c5c"
    on_accent = "#ffffff"


P = Palette


def fonts(root: tk.Misc) -> dict:
    """Build the font set, preferring a clean system UI face."""
    families = set(tkfont.families(root))
    for name in ("SF Pro Text", "Helvetica Neue", "Segoe UI", "Inter", "Arial"):
        if name in families:
            base = name
            break
    else:
        base = "TkDefaultFont"
    return {
        "display": tkfont.Font(family=base, size=30, weight="bold"),
        "h1": tkfont.Font(family=base, size=19, weight="bold"),
        "h2": tkfont.Font(family=base, size=14, weight="bold"),
        "body": tkfont.Font(family=base, size=12),
        "body_bold": tkfont.Font(family=base, size=12, weight="bold"),
        "small": tkfont.Font(family=base, size=11),
        "tiny": tkfont.Font(family=base, size=10),
        "mono": tkfont.Font(family="Menlo" if "Menlo" in families else base, size=12),
    }


def apply_theme(root: tk.Misc) -> dict:
    """Configure ttk styles for the dark theme. Returns the font dict."""
    F = fonts(root)
    style = ttk.Style(root)
    # 'clam' is themable on every platform (aqua ignores most colour options).
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    root.configure(bg=P.bg)

    style.configure(".", background=P.bg, foreground=P.text,
                    fieldbackground=P.surface_alt, borderwidth=0,
                    focuscolor=P.accent)

    style.configure("TFrame", background=P.bg)
    style.configure("Card.TFrame", background=P.surface)
    style.configure("Surface.TFrame", background=P.surface_alt)

    style.configure("TLabel", background=P.bg, foreground=P.text, font=F["body"])
    style.configure("Card.TLabel", background=P.surface, foreground=P.text,
                    font=F["body"])
    style.configure("Display.TLabel", background=P.surface, foreground=P.text,
                    font=F["display"])
    style.configure("H1.TLabel", background=P.bg, foreground=P.text, font=F["h1"])
    style.configure("H2.TLabel", background=P.surface, foreground=P.text,
                    font=F["h2"])
    style.configure("Dim.TLabel", background=P.bg, foreground=P.text_dim,
                    font=F["small"])
    style.configure("CardDim.TLabel", background=P.surface,
                    foreground=P.text_dim, font=F["small"])
    style.configure("Faint.TLabel", background=P.surface,
                    foreground=P.text_faint, font=F["tiny"])
    style.configure("Error.TLabel", background=P.bg, foreground=P.danger,
                    font=F["small"])
    style.configure("Success.TLabel", background=P.surface,
                    foreground=P.success, font=F["body_bold"])

    # Primary button
    style.configure("Accent.TButton", background=P.accent, foreground=P.on_accent,
                    font=F["body_bold"], borderwidth=0, focusthickness=0,
                    padding=(18, 11))
    style.map("Accent.TButton",
              background=[("pressed", P.accent_press), ("active", P.accent_hover),
                          ("disabled", P.surface_alt)],
              foreground=[("disabled", P.text_faint)])

    # Secondary / ghost button
    style.configure("Ghost.TButton", background=P.surface_alt, foreground=P.text,
                    font=F["body"], borderwidth=0, padding=(14, 9))
    style.map("Ghost.TButton",
              background=[("pressed", P.border), ("active", P.border),
                          ("disabled", P.surface)],
              foreground=[("disabled", P.text_faint)])

    # Danger-ish stop button
    style.configure("Stop.TButton", background=P.danger, foreground="#ffffff",
                    font=F["body_bold"], borderwidth=0, padding=(18, 11))
    style.map("Stop.TButton",
              background=[("pressed", "#e04a4a"), ("active", "#ff7070")])

    # Link-style button
    style.configure("Link.TButton", background=P.bg, foreground=P.accent,
                    font=F["small"], borderwidth=0, padding=(4, 2))
    style.map("Link.TButton",
              background=[("active", P.bg), ("pressed", P.bg)],
              foreground=[("active", P.accent_hover)])

    # Entries
    style.configure("TEntry", fieldbackground=P.surface_alt, foreground=P.text,
                    insertcolor=P.text, borderwidth=0, padding=(10, 9))
    style.map("TEntry", fieldbackground=[("focus", P.surface_alt)])

    style.configure("TSpinbox", fieldbackground=P.surface_alt, foreground=P.text,
                    arrowcolor=P.text, borderwidth=0, padding=(8, 7))

    # Progressbar (balance meter / indeterminate spinner)
    style.configure("Meter.Horizontal.TProgressbar", background=P.accent,
                    troughcolor=P.surface_alt, borderwidth=0, thickness=6)
    style.configure("Busy.Horizontal.TProgressbar", background=P.accent,
                    troughcolor=P.surface, borderwidth=0, thickness=3)

    style.configure("TSeparator", background=P.border)
    return F


def card(parent: tk.Misc, padding: int = 20, **kw) -> ttk.Frame:
    """A rounded-looking surface panel (Tk has no radius, so we use padding +
    a subtle background contrast)."""
    f = ttk.Frame(parent, style="Card.TFrame", padding=padding, **kw)
    return f


def hairline(parent: tk.Misc) -> ttk.Separator:
    return ttk.Separator(parent, orient="horizontal")


def center_window(win: tk.Misc, width: int, height: int) -> None:
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = int((sw - width) / 2)
    y = int((sh - height) / 3)
    win.geometry(f"{width}x{height}+{x}+{y}")


def fmt_hms(secs: float) -> str:
    secs = int(max(0, secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_hms_long(secs: float) -> str:
    secs = int(max(0, secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s"
