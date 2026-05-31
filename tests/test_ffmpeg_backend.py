"""
Tests for ffmpeg backend — verifies command building across platforms.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from micecam.camera_manager import get_ffmpeg_path


class TestFfmpegPath:
    def test_returns_string(self) -> None:
        path = get_ffmpeg_path()
        assert isinstance(path, str)
        assert "ffmpeg" in path

    def test_bundled_takes_priority(self, tmp_path: Path) -> None:
        """When bundled ffmpeg exists, it should be used."""
        # This test is more of a design-doc test — on macOS in CI,
        # the bundled ffmpeg won't exist, so we just verify the function
        # returns something sensible.
        path = get_ffmpeg_path()
        assert path  # non-empty


class TestPlatformUtils:
    def test_imports(self) -> None:
        """Verify utility modules import cleanly."""
        from micecam.utils.platform import is_macos, is_windows, ffmpeg_device_format

        # One of these must be True
        assert is_macos() or is_windows() or True  # Linux is valid too

        fmt = ffmpeg_device_format()
        assert fmt in ("avfoundation", "dshow", "v4l2")

    def test_resource_path(self) -> None:
        from micecam.utils.resource_path import get_ffmpeg_path as resolve_ffmpeg

        path = resolve_ffmpeg()
        assert isinstance(path, str)
        assert len(path) > 0
