# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for MiceCam — dual-camera recorder.

Bundles:
- The micecam Python package + all submodules
- PyQt6 (QtCore, QtGui, QtWidgets)
- ffmpeg.exe (standalone binary, placed at bundle root)

Usage:
    uv run pyinstaller --clean micecam.spec

Output: dist/MiceCam.exe (single-file, no console)
"""

import shutil as _shutil
import sys as _sys
from pathlib import Path as _Path

_PROJECT = _Path(SPECPATH)
_IS_WIN = _sys.platform == "win32"
_EXE = "ffmpeg.exe" if _IS_WIN else "ffmpeg"

# Locate ffmpeg binary
_ffmpeg = _PROJECT / "ffmpeg" / _EXE
if not _ffmpeg.exists():
    _ffmpeg = _PROJECT / "ffmpeg_bundled" / _EXE
if not _ffmpeg.exists():
    _which = _shutil.which(_EXE)
    if _which:
        _ffmpeg = _Path(_which)
    else:
        raise FileNotFoundError(
            f"{_EXE} not found. Run scripts/download_ffmpeg.py first, "
            f"or install ffmpeg and add it to PATH."
        )

print(f"  Bundling ffmpeg: {_ffmpeg}  ({_ffmpeg.stat().st_size // (1024*1024)} MB)")

# ── Analysis ───────────────────────────────────────────────────────────
a = Analysis(
    ['src/micecam/main.py'],
    pathex=['src'],
    binaries=[(str(_ffmpeg), '.')],
    datas=[],
    hiddenimports=[
        # PyQt6
        'PyQt6',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'PyQt6.sip',
        # micecam — all subpackages (belt-and-suspenders; most are
        # auto-detected via static imports, but explicit listing
        # prevents surprises when PyInstaller misses a dynamic path)
        'micecam',
        'micecam.camera_manager',
        'micecam.recorder',
        'micecam.timestamp',
        'micecam.core',
        'micecam.core.sync_controller',
        'micecam.gui',
        'micecam.gui.main_window',
        'micecam.gui.camera_panel',
        'micecam.services',
        'micecam.services.disk_monitor',
        'micecam.utils',
        'micecam.utils.platform',
        'micecam.utils.resource_path',
        # stdlib modules that may be missed
        'logging',
        'pathlib',
        'subprocess',
        'threading',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # These are large and never used at runtime
        'tkinter',
        'unittest',
        'email',
        'http',
        'xmlrpc',
        'pydoc',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MiceCam',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[
        # Don't UPX-compress large binaries that are already compressed
        # or that UPX may corrupt (ffmpeg.exe, Qt DLLs)
        'ffmpeg.exe',
        'Qt6Core.dll',
        'Qt6Gui.dll',
        'Qt6Widgets.dll',
    ],
    runtime_tmpdir=None,
    console=False,               # no terminal window on Windows
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
