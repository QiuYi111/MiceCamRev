"""
SRT timestamp generation with nanosecond-precision steady clock.

Design
------
At recording start we capture two clocks:

* **wall_clock_start** (`time.time()`) — absolute POSIX wall time.
* **steady_clock_start** (`time.monotonic_ns()`) — monotonic nanosecond counter
  that never goes backwards and is immune to NTP adjustment.

Every subsequent event's actual wall time is reconstructed as::

    actual_time = wall_clock_start + (monotonic_ns() - steady_clock_start) / 1e9

This gives us wall-clock anchoring while preserving the stable interval
measurement from the monotonic clock — critical for frame-level timing.

SRT Format
----------
Standard SubRip format::

    1
    00:00:00,000 --> 00:00:00,033
    ts=2026-05-31T14:30:00.123456789  frame=1

    2
    00:00:00,033 --> 00:00:00,067
    ts=2026-05-31T14:30:00.156789012  frame=2

The subtitle text contains the ISO 8601 nanosecond-precision timestamp.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class TimestampWriter:
    """
    Writes SRT-format timestamps synchronized with video recording.

    Usage::

        tw = TimestampWriter(Path("output.srt"))
        tw.start()                        # anchors wall + steady clocks
        tw.write_frame(1)                 # write SRT entry for frame 1
        tw.finalize(duration_s=10.5, total_frames=315)
    """

    def __init__(self, output_path: Path) -> None:
        self._path = Path(output_path)
        self._file = None

        # Clock anchors — set in start()
        self.wall_start: float = 0.0
        self.steady_start: int = 0

        # ISO format with nanoseconds
        self._time_fmt = "%Y-%m-%dT%H:%M:%S"

    # ── public API ────────────────────────────────────────────────────

    def start(self, wall_start: float | None = None,
              steady_start: int | None = None) -> None:
        """
        Capture the dual-clock reference point.

        Call once at the instant recording begins.

        If *wall_start* / *steady_start* are provided (from a SyncController),
        they are used as the shared reference instead of capturing new values.
        This enables soft sync — two cameras share the same time base.
        """
        if wall_start is not None:
            self.wall_start = wall_start
        else:
            self.wall_start = time.time()

        if steady_start is not None:
            self.steady_start = steady_start
        else:
            self.steady_start = time.monotonic_ns()
        self._file = open(self._path, "w", encoding="utf-8")

        # Write header comment
        wall_iso = datetime.fromtimestamp(
            self.wall_start, tz=timezone.utc
        ).isoformat()
        self._file.write(
            f"# MiceCam SRT timestamps\n"
            f"# wall_start (UTC): {wall_iso}\n"
            f"# steady_start_ns:  {self.steady_start}\n\n"
        )
        self._file.flush()

        logger.info("Timestamp writer started: wall=%s steady=%d",
                    wall_iso, self.steady_start)

    def write_frame(self, frame_number: int) -> None:
        """
        Write an SRT entry for a single frame.

        The actual wall time is reconstructed from the steady clock offset.
        """
        if self._file is None:
            logger.warning("Timestamp writer not started")
            return

        elapsed_ns = time.monotonic_ns() - self.steady_start
        actual_wall = self.wall_start + (elapsed_ns / 1e9)

        # SRT timecodes (relative to video start)
        frame_duration_s = 1.0 / 30.0  # placeholder, updated in finalize
        start_s = (frame_number - 1) * frame_duration_s
        end_s = frame_number * frame_duration_s

        start_tc = self._seconds_to_srt_timecode(start_s)
        end_tc = self._seconds_to_srt_timecode(end_s)

        # Full nanosecond-precision timestamp
        whole_sec = int(actual_wall)
        nanos = int((actual_wall - whole_sec) * 1e9)
        ts_str = datetime.fromtimestamp(
            whole_sec, tz=timezone.utc
        ).strftime(self._time_fmt)
        ts_full = f"{ts_str}.{nanos:09d}"

        self._file.write(
            f"{frame_number}\n"
            f"{start_tc} --> {end_tc}\n"
            f"ts={ts_full}  frame={frame_number}\n\n"
        )
        self._file.flush()  # crash-safe: persist each entry immediately

    def finalize(self, duration_seconds: float, total_frames: int) -> None:
        """
        Generate all SRT entries now that recording is complete.

        Reconstructs per-frame wall-clock timestamps using the steady-clock
        offset captured in ``start()``.  Each frame's absolute UTC time is::

            wall_start + frame_index × (duration / total_frames)

        The nanoseconds field is computed from the fractional seconds.
        """
        if self._file is None:
            return

        # Append frame entries to the existing header
        self._file.close()

        if total_frames < 1:
            logger.warning("No frames recorded, SRT file will be empty")
            return

        frame_duration = duration_seconds / total_frames

        # Read back the header lines
        header_lines: list[str] = []
        with open(self._path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#"):
                    header_lines.append(line.rstrip("\n"))
                else:
                    break

        with open(self._path, "w", encoding="utf-8") as f:
            for h in header_lines:
                f.write(h + "\n")
            f.write("\n")

            for frame_idx in range(total_frames):
                frame_num = frame_idx + 1
                elapsed = frame_idx * frame_duration
                start_s = elapsed
                end_s = elapsed + frame_duration

                # Reconstruct absolute wall time with nanosecond precision
                actual_wall = self.wall_start + elapsed
                whole_sec = int(actual_wall)
                nanos = int((actual_wall - whole_sec) * 1e9)
                ts_str = datetime.fromtimestamp(
                    whole_sec, tz=timezone.utc
                ).strftime(self._time_fmt)
                ts_full = f"{ts_str}.{nanos:09d}"

                start_tc = self._seconds_to_srt_timecode(start_s)
                end_tc = self._seconds_to_srt_timecode(end_s)

                f.write(
                    f"{frame_num}\n"
                    f"{start_tc} --> {end_tc}\n"
                    f"ts={ts_full}  frame={frame_num}\n\n"
                )

        logger.info("SRT finalized: %d frames over %.3f s (%.3f ms/frame)",
                    total_frames, duration_seconds, frame_duration * 1000)

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _seconds_to_srt_timecode(seconds: float) -> str:
        """Convert seconds to SRT timecode format: HH:MM:SS,mmm"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        return f"{hours:02d}:{minutes:02d}:{secs:06.3f}".replace(".", ",")
