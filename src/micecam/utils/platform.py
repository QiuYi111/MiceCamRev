"""
Canonical OS detection helpers.

Use these everywhere instead of raw ``sys.platform`` or ``platform.system()``
to keep comparisons consistent across the codebase.
"""

from __future__ import annotations

import platform as _platform
import sys
from pathlib import Path


def is_windows() -> bool:
    return sys.platform == "win32"


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform == "linux"


def system_name() -> str:
    """Return 'Darwin', 'Windows', or 'Linux' (matches encoder dict keys)."""
    return _platform.system()


def ffmpeg_device_format() -> str:
    """Return the ffmpeg ``-f`` value for the current platform."""
    if is_macos():
        return "avfoundation"
    elif is_windows():
        return "dshow"
    else:
        return "v4l2"


def ffmpeg_executable_name() -> str:
    """Return 'ffmpeg.exe' on Windows, 'ffmpeg' otherwise."""
    return "ffmpeg.exe" if is_windows() else "ffmpeg"


def default_output_root() -> Path:
    """Platform-appropriate default directory for recordings."""
    if is_windows():
        return Path.home() / "Videos" / "MiceCam"
    elif is_macos():
        return Path.home() / "Movies" / "MiceCam"
    else:
        return Path.home() / "Videos" / "MiceCam"


def default_settings_path() -> Path:
    """Platform-appropriate settings file location."""
    if is_windows():
        base = Path.home() / "AppData" / "Roaming" / "MiceCam"
    elif is_macos():
        base = Path.home() / "Library" / "Application Support" / "MiceCam"
    else:
        base = Path.home() / ".config" / "micecam"
    base.mkdir(parents=True, exist_ok=True)
    return base / "settings.json"
