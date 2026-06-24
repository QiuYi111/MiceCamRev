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

# Platform-specific ffmpeg binary locations (relative to ROOT).
# Must match the directory structure used by scripts/download_ffmpeg.py
# and the runtime resolution in camera_manager.get_ffmpeg_path().
FFMPEG_BUNDLE: dict[str, str] = {
    "Darwin": "ffmpeg/ffmpeg",
    "Windows": "ffmpeg/ffmpeg.exe",
    "Linux": "ffmpeg/ffmpeg",
}
MF_HELPER = ROOT / "helpers" / "build" / "Release" / "mf_single_frame_helper.exe"
HIDDEN_IMPORTS = [
    "PyQt6",
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    "PyQt6.sip",
    "micecam",
    "micecam.camera_manager",
    "micecam.recorder",
    "micecam.single_frame_recorder",
    "micecam.timestamp",
    "micecam.core",
    "micecam.core.sync_controller",
    "micecam.gui",
    "micecam.gui.main_window",
    "micecam.gui.camera_panel",
    "micecam.services",
    "micecam.services.disk_monitor",
    "micecam.utils",
    "micecam.utils.platform",
    "micecam.utils.resource_path",
    "logging",
    "pathlib",
    "subprocess",
    "threading",
]
EXCLUDES = [
    "tkinter",
    "unittest",
    "email",
    "http",
    "xmlrpc",
    "pydoc",
]
UPX_EXCLUDES = [
    "ffmpeg.exe",
    "Qt6Core.dll",
    "Qt6Gui.dll",
    "Qt6Widgets.dll",
]


def ensure_ffmpeg() -> Path:
    """Verify or download the ffmpeg binary for the current platform."""
    system = platform.system()
    bundled = ROOT / FFMPEG_BUNDLE.get(system, "ffmpeg")

    if bundled.exists():
        print(f"[OK] Using bundled ffmpeg: {bundled}")
        return bundled

    # Fall back to system ffmpeg
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        print(f"[WARN] No bundled ffmpeg, using system: {system_ffmpeg}")
        return Path(system_ffmpeg)

    print("ERROR: ffmpeg not found. Download a pre-compiled ffmpeg and place it at:")
    print(f"  {bundled}")
    print("Download from: https://ffmpeg.org/download.html")
    sys.exit(1)


def ensure_mf_helper() -> Path | None:
    """Return the Windows Media Foundation helper when packaging on Windows."""
    if platform.system() != "Windows":
        return None
    if MF_HELPER.exists():
        print(f"[OK] Using Media Foundation helper: {MF_HELPER}")
        return MF_HELPER
    print("ERROR: mf_single_frame_helper.exe not found.")
    print("Build it first from a Visual Studio Developer PowerShell:")
    print("  powershell -ExecutionPolicy Bypass -File helpers/build_mf_helper.ps1")
    sys.exit(1)


def clean_dist() -> None:
    """Remove previous build artifacts (best-effort, skips locked files)."""
    for d in ["dist", "build"]:
        path = ROOT / d
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            print(f"[OK] Cleaned {d}/")


def build_spec(ffmpeg_path: Path, mf_helper_path: Path | None = None) -> str:
    """Generate PyInstaller .spec file content."""
    # Use posix path to avoid backslash escape issues in the spec file
    # (e.g. \f, \U in Windows paths become Python escape sequences)
    binary_path = ffmpeg_path.as_posix()
    binaries = [(binary_path, ".")]
    if mf_helper_path is not None:
        binaries.append((mf_helper_path.as_posix(), "."))
    return f"""# -*- mode: python ; coding: utf-8 -*-
# Auto-generated spec for MiceCam

a = Analysis(
    ['src/micecam/main.py'],
    pathex=['src'],
    binaries={binaries!r},
    datas=[],
    hiddenimports={HIDDEN_IMPORTS!r},
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes={EXCLUDES!r},
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
    upx_exclude={UPX_EXCLUDES!r},
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
    mf_helper_path = ensure_mf_helper()
    clean_dist()

    # Copy ffmpeg to a temp location relative to the package
    bundled_dir = ROOT / "ffmpeg_bundled"
    bundled_dir.mkdir(exist_ok=True)
    dest = bundled_dir / ffmpeg_path.name
    if ffmpeg_path != dest:
        shutil.copy2(ffmpeg_path, dest)
        print(f"[OK] Copied ffmpeg to {dest}")

    # Write a generated spec and run PyInstaller.  Keep it distinct from the
    # hand-maintained micecam.spec so the two packaging entrypoints are not
    # confused during local debugging.
    helper_dest: Path | None = None
    if mf_helper_path is not None:
        helper_dest = bundled_dir / mf_helper_path.name
        if mf_helper_path != helper_dest:
            shutil.copy2(mf_helper_path, helper_dest)
            print(f"[OK] Copied Media Foundation helper to {helper_dest}")

    spec_content = build_spec(dest, helper_dest)
    spec_path = ROOT / "MiceCam.generated.spec"
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
