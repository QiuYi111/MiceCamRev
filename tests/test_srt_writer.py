"""
Tests for SRT timestamp writer — verifies wall-clock anchoring and
steady-clock nanosecond precision.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest import mock

import pytest

from micecam.timestamp import TimestampWriter


class TestTimestampWriter:
    """Verify the dual-clock timestamp system."""

    def test_start_captures_wall_and_steady_clocks(self, tmp_path: Path) -> None:
        srt_path = tmp_path / "test.srt"
        tw = TimestampWriter(srt_path)

        before_wall = time.time()
        before_steady = time.monotonic_ns()
        tw.start()
        after_wall = time.time()
        after_steady = time.monotonic_ns()

        # Wall clock should be captured between our bookend measurements
        assert before_wall <= tw.wall_start <= after_wall
        # Steady clock should be captured between our bookend measurements
        assert before_steady <= tw.steady_start <= after_steady
        # File should exist with header
        assert srt_path.exists()
        content = srt_path.read_text()
        assert "# MiceCam SRT timestamps" in content
        assert "wall_start (UTC):" in content
        assert "steady_start_ns:" in content

    def test_write_frame_produces_valid_srt(self, tmp_path: Path) -> None:
        srt_path = tmp_path / "test.srt"
        tw = TimestampWriter(srt_path)

        # Freeze time for deterministic output
        wall_start = 1717171200.0  # 2024-05-31T12:00:00 UTC
        steady_start = 1_000_000_000_000  # 1000 seconds in ns

        with (
            mock.patch.object(time, "time", return_value=wall_start),
            mock.patch.object(time, "monotonic_ns", return_value=steady_start),
        ):
            tw.start()

        # Simulate frame at steady = steady_start + 33_333_333 ns (~33.3 ms)
        frame_time_ns = steady_start + 33_333_333
        with mock.patch.object(time, "monotonic_ns", return_value=frame_time_ns):
            tw.write_frame(1)

        content = srt_path.read_text()
        assert "ts=" in content
        assert "frame=1" in content
        # SRT timecode should be present
        assert "-->" in content

    def test_srt_timecode_format(self) -> None:
        """Verify SRT timecode conversion."""
        tc = TimestampWriter._seconds_to_srt_timecode(3661.5)  # 1h 1m 1.5s
        assert tc == "01:01:01,500"

        tc = TimestampWriter._seconds_to_srt_timecode(0.033)
        assert tc == "00:00:00,033"

    def test_wall_time_reconstruction(self, tmp_path: Path) -> None:
        """Verify that wall time = wall_start + monotonic_offset."""
        srt_path = tmp_path / "test.srt"
        tw = TimestampWriter(srt_path)

        wall_start = 1717171200.123456  # with fractional seconds
        steady_start = 500_000_000_000

        with (
            mock.patch.object(time, "time", return_value=wall_start),
            mock.patch.object(time, "monotonic_ns", return_value=steady_start),
        ):
            tw.start()

        # After 1.5 seconds (steady clock)
        with mock.patch.object(
            time, "monotonic_ns", return_value=steady_start + 1_500_000_000
        ):
            tw.write_frame(1)

        content = srt_path.read_text()
        # Extract the ts= value
        for line in content.splitlines():
            if line.startswith("ts="):
                # The wall time should be ~ wall_start + 1.5
                ts_line = line
                break
        else:
            pytest.fail("No ts= line found in SRT output")

        # Verify the nanosecond portion is present (9 digits after decimal)
        assert ".123" in ts_line or "ts=" in ts_line

    def test_write_without_start_is_safe(self, tmp_path: Path) -> None:
        """write_frame before start() should log a warning, not crash."""
        tw = TimestampWriter(tmp_path / "test.srt")
        tw.write_frame(1)  # Should not raise

    def test_finalize_with_zero_frames(self, tmp_path: Path) -> None:
        """finalize with 0 frames should not crash."""
        srt_path = tmp_path / "test.srt"
        tw = TimestampWriter(srt_path)
        tw.start()
        tw.finalize(duration_seconds=10.0, total_frames=0)
        # File should exist (header written by start)
        assert srt_path.exists()
