"""
PyInstaller packaging script for MiceCam.

Bundles the Python application + ffmpeg into a standalone executable.
Pre-compiled ffmpeg binaries should be placed in `ffmpeg/<platform>/`
before running this script.

Usage::

    uv run python packager.py

Output goes to `dist/MiceCam/`.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent

# Platform-specific ffmpeg binary locations (relative to ROOT)
FFMPEG_BUNDLE: dict[str, str] = {
    "Darwin": "ffmpeg/macos/ffmpeg",
    "Windows": "ffmpeg/windows/ffmpeg.exe",
    "Linux": "ffmpeg/linux/ffmpeg",
}


def ensure_ffmpeg() -> Path:
    """Verify or download the ffmpeg binary for the current platform."""
    system = platform.system()
    bundled = ROOT / FFMPEG_BUNDLE.get(system, "ffmpeg")

    if bundled.exists():
        print(f"✓ Using bundled ffmpeg: {bundled}")
        return bundled

    # Fall back to system ffmpeg
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        print(f"⚠ No bundled ffmpeg, using system: {system_ffmpeg}")
        return Path(system_ffmpeg)

    print("ERROR: ffmpeg not found. Download a pre-compiled ffmpeg and place it at:")
    print(f"  {bundled}")
    print("Download from: https://ffmpeg.org/download.html")
    sys.exit(1)


def clean_dist() -> None:
    """Remove previous build artifacts."""
    for d in ["dist", "build"]:
        path = ROOT / d
        if path.exists():
            shutil.rmtree(path)
            print(f"✓ Cleaned {d}/")


def build_spec(ffmpeg_path: Path) -> str:
    """Generate PyInstaller .spec file content."""
    return f"""# -*- mode: python ; coding: utf-8 -*-
# Auto-generated spec for MiceCam

a = Analysis(
    ['src/micecam/main.py'],
    pathex=[],
    binaries=[('{ffmpeg_path}', '.')],
    datas=[],
    hiddenimports=[
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
    ],
    hookspath=[],
    hooksconfig={{}},
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
"""


def main() -> None:
    print("=" * 60)
    print("  MiceCam Packager")
    print("=" * 60)

    ffmpeg_path = ensure_ffmpeg()
    clean_dist()

    # Copy ffmpeg to a temp location relative to the package
    bundled_dir = ROOT / "ffmpeg_bundled"
    bundled_dir.mkdir(exist_ok=True)
    dest = bundled_dir / ffmpeg_path.name
    if ffmpeg_path != dest:
        shutil.copy2(ffmpeg_path, dest)
        print(f"✓ Copied ffmpeg to {dest}")

    # Write spec and run PyInstaller
    spec_content = build_spec(dest)
    spec_path = ROOT / "MiceCam.spec"
    spec_path.write_text(spec_content, encoding="utf-8")

    print("Running PyInstaller...")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", str(spec_path)],
        cwd=str(ROOT), check=True,
    )

    print()
    print("=" * 60)
    print("  Build complete!")
    print(f"  Output: {ROOT / 'dist' / 'MiceCam'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
