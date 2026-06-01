# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for MiceCam — dual-camera recorder.

Finds ffmpeg dynamically from:
  1. SPECPATH (the directory containing this .spec file)
  2. Fallback to system PATH

Usage:
    uv run pyinstaller micecam.spec
"""

import shutil as _shutil
import sys as _sys
from pathlib import Path as _Path

# SPECPATH is injected by PyInstaller at spec-execution time
_PROJECT = _Path(SPECPATH)
_IS_WIN = _sys.platform == "win32"
_EXE = "ffmpeg.exe" if _IS_WIN else "ffmpeg"

# Look for ffmpeg in priority order
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
            f"or install ffmpeg."
        )

print(f"Bundling ffmpeg from: {_ffmpeg}")

a = Analysis(
    ['src/micecam/main.py'],
    pathex=[],
    binaries=[(str(_ffmpeg), '.')],
    datas=[],
    hiddenimports=[
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'PyQt6.sip',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
