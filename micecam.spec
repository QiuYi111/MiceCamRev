# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for MiceCam — dual-camera recorder.

Usage:
    uv run pyinstaller micecam.spec

The resulting dist/MiceCam.exe (Windows) or dist/MiceCam (macOS) bundles
ffmpeg alongside the application.
"""

import sys
from pathlib import Path

block_cipher = None

# ── Determine ffmpeg binary to bundle ───────────────────────────────────
_IS_WIN = sys.platform == "win32"
_FFMPEG_NAME = "ffmpeg.exe" if _IS_WIN else "ffmpeg"
_PROJECT_ROOT = Path(__file__).parent

# Look for bundled ffmpeg (put there by scripts/download_ffmpeg.py)
_bundled_ffmpeg = _PROJECT_ROOT / "ffmpeg" / _FFMPEG_NAME
if _bundled_ffmpeg.exists():
    _ffmpeg_src = str(_bundled_ffmpeg)
else:
    # Fall back to system ffmpeg (development convenience)
    import shutil as _shutil
    _ffmpeg_src = _shutil.which(_FFMPEG_NAME)
    if not _ffmpeg_src:
        raise FileNotFoundError(
            f"{_FFMPEG_NAME} not found. "
            "Run scripts/download_ffmpeg.py first, or install ffmpeg."
        )

print(f"Bundling ffmpeg from: {_ffmpeg_src}")

a = Analysis(
    ['src/micecam/__main__.py'],
    pathex=[],
    binaries=[(_ffmpeg_src, '.')],
    datas=[],
    hiddenimports=['PyQt6.sip', 'PyQt6.QtCore', 'PyQt6.QtGui', 'PyQt6.QtWidgets'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'unittest', 'email', 'http', 'xml', 'pydoc',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='MiceCam',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                       # No console window (GUI app)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,                            # Set to .ico path if available
)
