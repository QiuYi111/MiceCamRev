#!/usr/bin/env python3
"""
Download a pre-compiled ffmpeg.exe for Windows packaging.

Fetches from BtbN/FFmpeg-Builds GitHub releases (GPL-shared build).
Run this on a machine with internet access before building the Windows exe.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from urllib.request import urlretrieve

# BtbN's ffmpeg-master-latest-win64-gpl.zip — statically linked, no DLL deps.
# "gpl" (not "gpl-shared"): all codecs linked into ffmpeg.exe, ~80 MB but
# fully self-contained — no external DLLs needed at runtime.
FFMPEG_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl.zip"
)

# Where to place the extracted ffmpeg.exe (project root /ffmpeg/)
PROJECT_ROOT = Path(__file__).parent.parent
BUNDLE_DIR = PROJECT_ROOT / "ffmpeg"


def download_with_progress(url: str, dest: Path) -> None:
    """Download a file with a simple progress indicator."""
    print(f"Downloading {url}")
    print(f"  → {dest}")

    def _report(block_num: int, block_size: int, total_size: int) -> None:
        downloaded = block_num * block_size
        if total_size > 0:
            pct = min(100, int(downloaded * 100 / total_size))
            mb_dl = downloaded / (1024 * 1024)
            mb_tot = total_size / (1024 * 1024)
            print(f"\r  {pct:3d}%  {mb_dl:.1f} / {mb_tot:.1f} MB", end="")
        else:
            print(f"\r  {downloaded / (1024*1024):.1f} MB downloaded", end="")

    urlretrieve(url, dest, reporthook=_report)
    print()  # newline after progress


def extract_ffmpeg(zip_path: Path) -> Path | None:
    """Extract ffmpeg.exe from the downloaded zip. Returns its path or None."""
    print(f"Extracting ffmpeg.exe from {zip_path.name}...")
    BUNDLE_DIR.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        # Find the bin/ffmpeg.exe inside the zip
        for name in zf.namelist():
            if name.endswith("/bin/ffmpeg.exe"):
                print(f"  Found: {name}")
                data = zf.read(name)
                dest = BUNDLE_DIR / "ffmpeg.exe"
                dest.write_bytes(data)
                dest.chmod(0o755)
                print(f"  Extracted → {dest} ({len(data):,} bytes)")
                return dest

    print("ERROR: ffmpeg.exe not found in the zip archive.")
    print("Contents (top-level):")
    with zipfile.ZipFile(zip_path) as zf:
        for name in sorted(zf.namelist())[:20]:
            print(f"  {name}")
    return None


def main() -> int:
    if sys.platform == "win32":
        print("Note: Running on Windows — downloading ffmpeg.exe")
    else:
        print(
            "Note: Running on non-Windows OS. "
            "The downloaded ffmpeg.exe is for Windows packaging only."
        )
        print("  On macOS/Linux, local ffmpeg is used for development.")

    zip_path = BUNDLE_DIR / "ffmpeg-win64.zip"

    try:
        download_with_progress(FFMPEG_URL, zip_path)
    except Exception as exc:
        print(f"ERROR: Download failed: {exc}")
        return 1

    result = extract_ffmpeg(zip_path)

    # Cleanup zip
    zip_path.unlink(missing_ok=True)

    if result:
        print("\nDone! ffmpeg.exe is ready for PyInstaller packaging.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
