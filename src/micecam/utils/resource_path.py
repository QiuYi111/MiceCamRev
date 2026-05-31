"""
Resolve paths to bundled resources — works both in development and when
frozen (PyInstaller one-file / one-dir).
"""

from __future__ import annotations

import sys
from pathlib import Path


def get_ffmpeg_path() -> str:
    """
    Return the path to ffmpeg.

    Priority:
    1. Bundled ffmpeg next to the executable (PyInstaller)
    2. Bundled ffmpeg in the project root (development)
    3. System ffmpeg on PATH
    """
    from micecam.utils.platform import is_windows

    exe_name = "ffmpeg.exe" if is_windows() else "ffmpeg"

    # 1. PyInstaller bundle — ffmpeg is placed next to the exe
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = Path(sys._MEIPASS) / exe_name
        if bundled.exists():
            return str(bundled)

    # 2. Development — look in project root /ffmpeg/
    project_root = Path(__file__).parent.parent.parent.parent
    bundled = project_root / "ffmpeg" / exe_name
    if bundled.exists():
        return str(bundled)

    # 3. System PATH
    return exe_name if is_windows() else "ffmpeg"


def get_resource_dir() -> Path:
    """Return the directory containing bundled data files."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).parent.parent.parent.parent
