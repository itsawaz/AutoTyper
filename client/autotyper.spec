# PyInstaller spec for the AutoTyper desktop client.
# Build a standalone, windowed (no console) app for the current OS:
#
#   macOS   :  pyinstaller autotyper.spec        -> dist/AutoTyper.app
#   Windows :  pyinstaller autotyper.spec        -> dist\AutoTyper\AutoTyper.exe
#
# Run from the `client/` directory with its venv active and pyinstaller installed:
#   pip install pyinstaller
#
# Notes:
# - pynput and pyautogui pull in platform backends; PyInstaller usually detects
#   them, but we add a couple of hidden imports defensively.
# - The backend URL is read at runtime from the AUTOTYPER_API_BASE env var or the
#   saved config, so the same build works against dev and prod.

import sys

block_cipher = None

hidden = [
    "pynput.keyboard._darwin",
    "pynput.keyboard._win32",
    "pynput.keyboard._xorg",
    "pynput.mouse._darwin",
    "pynput.mouse._win32",
    "pynput.mouse._xorg",
    "qrcode",
    "qrcode.image.pil",
    "PIL",
    "PIL.Image",
    "PIL._imaging",
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
    excludes=[],
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
    argv_emulation=True,    # macOS: forward file-open/args to the app
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
            # AutoTyper simulates keystrokes, so it needs Accessibility permission.
            "NSAppleEventsUsageDescription": "AutoTyper simulates typing for you.",
        },
    )
