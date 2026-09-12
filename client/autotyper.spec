# PyInstaller spec for the AutoTyper desktop client (Tkinter GUI).
# Build a standalone, windowed (no console) app for the current OS:
#
#   macOS   :  pyinstaller autotyper.spec        -> dist/AutoTyper.app
#   Windows :  pyinstaller autotyper.spec        -> dist\AutoTyper\AutoTyper.exe
#
# Run from the `client/` directory with its venv active and pyinstaller installed:
#   pip install pyinstaller
#
# Notes:
# - The GUI is Tkinter (bundled with Python), so there are no Qt plugins to ship.
#   PyInstaller includes the Tcl/Tk runtime automatically.
# - pynput/pyautogui pull in platform backends; we list them defensively.
# - The backend URL is read at runtime from AUTOTYPER_API_BASE or the saved
#   config, so one build works against dev and prod.

import sys

block_cipher = None

hidden = [
    # pynput / pyautogui platform backends
    "pynput.keyboard._darwin",
    "pynput.keyboard._win32",
    "pynput.keyboard._xorg",
    "pynput.mouse._darwin",
    "pynput.mouse._win32",
    "pynput.mouse._xorg",
    # QR rendering
    "qrcode",
    "qrcode.image.pil",
    "PIL",
    "PIL.Image",
    "PIL.ImageTk",
    "PIL._imaging",
    # Tkinter pieces PyInstaller sometimes misses
    "tkinter",
    "tkinter.ttk",
    "tkinter.font",
    "tkinter.messagebox",
    "tkinter.simpledialog",
]

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PySide6", "shiboken6", "PyQt5", "PyQt6"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AutoTyper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # windowed app, no terminal
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="AutoTyper",
)

# On macOS, wrap the collected app into a .app bundle.
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="AutoTyper.app",
        icon=None,
        bundle_identifier="com.autotyper.client",
        info_plist={
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": "1.1.0",
            # AutoTyper simulates keystrokes and watches for user activity.
            "NSAppleEventsUsageDescription": "AutoTyper simulates typing for you.",
        },
    )
