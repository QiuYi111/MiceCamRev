"""
ffmpeg-based video recorder with hardware encoding support.

Manages ffmpeg subprocess lifecycle, builds encoding pipelines that
prioritise the camera's native/accelerated encoder, and reports progress
via parsing ffmpeg's stderr output.
"""

from __future__ import annotations

import logging
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from micecam.camera_manager import get_ffmpeg_path, get_preferred_encoder
from micecam.timestamp import TimestampWriter

logger = logging.getLogger(__name__)


class Recorder:
    """
    Manages a single ffmpeg recording session.

    Usage::

        rec = Recorder(camera_id="0", output_dir=Path("./recordings"))
        rec.start(resolution=(1920, 1080), fps=30, codec="h264")
        # ... recording runs in background ...
        rec.stop()

    The SRT timestamp file is written alongside the MP4, keyed on the
    frame number and elapsed steady-clock nanoseconds since recording began.
    """

    # Compressed codecs that can be stream-copied to MP4 without re-encoding.
    _PASSTHROUGH_CODECS = frozenset({"mjpeg", "h264", "hevc"})

    def __init__(self, camera_id: str, camera_name: str = "",
                 output_dir: Path = Path("./output"),
                 native_codec: str = "") -> None:
        self.camera_id = camera_id          # platform-specific device id
        self.camera_name = camera_name       # human-readable label
        self.native_codec = native_codec     # camera's native output (e.g. "mjpeg")
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self._process: Optional[subprocess.Popen] = None
        self._ts_writer: Optional[TimestampWriter] = None
        self._output_path: Optional[Path] = None
        self._srt_path: Optional[Path] = None
        self._metadata_path: Optional[Path] = None
        self._is_recording = False

        # Progress tracking
        self.duration_seconds: float = 0.0
        self.frame_count: int = 0
        self._frame_wall_times: list[float] = []
        self._requested_resolution: tuple[int, int] | None = None
        self._requested_fps: int | None = None
        self._requested_codec: str = ""

    # ── public API ────────────────────────────────────────────────────

    def start(self, resolution: tuple[int, int] = (1920, 1080),
              fps: int = 30, codec: str = "h264",
              wall_start: float | None = None,
              steady_start: int | None = None,
              wait_for_ready: bool = True) -> Path:
        """
        Launch ffmpeg recording in a subprocess.

        If *wall_start* / *steady_start* are provided (from a SyncController),
        the SRT timestamps use a shared time base for cross-camera soft sync.

        Returns the path to the output MP4 file.
        """
        if self._is_recording:
            raise RuntimeError("Already recording")

        system = sys.platform
        date_str = time.strftime("%Y-%m-%d")
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        safe_name = self.camera_name.replace(" ", "_").replace('"', '')[:30]
        stem = f"{safe_name}_{timestamp}" if safe_name else f"cam_{timestamp}"

        # Per-camera, date-based output: output/{camera_name}/{YYYY-MM-DD}/
        cam_dir = self.output_dir / safe_name / date_str
        cam_dir.mkdir(parents=True, exist_ok=True)

        self._output_path = cam_dir / f"{stem}.mp4"
        self._srt_path = cam_dir / f"{stem}.srt"
        self._metadata_path = cam_dir / f"{stem}.json"
        self._requested_resolution = resolution
        self._requested_fps = fps
        self._requested_codec = codec
        self._frame_wall_times = []

        # Start timestamp writer — uses shared clock refs if provided (soft sync)
        ts_writer = TimestampWriter(self._srt_path)
        ts_writer.start(wall_start=wall_start, steady_start=steady_start)
        self._ts_writer = ts_writer

        use_passthrough = self.native_codec in self._PASSTHROUGH_CODECS
        if use_passthrough:
            logger.info("Using native %s passthrough — no re-encode",
                        self.native_codec)
        else:
            encoder = get_preferred_encoder(codec)
            logger.info("Using encoder: %s for codec %s", encoder, codec)

        cmd = self._build_command(
            resolution=resolution, fps=fps,
            encoder="copy" if use_passthrough else encoder,
            output_path=self._output_path,
        )
        logger.debug("ffmpeg command: %s", " ".join(cmd))

        # Launch ffmpeg — send stdin as PIPE so we can write 'q' to stop gracefully
        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            # Put ffmpeg in its own process group so we can signal it
            preexec_fn=os.setsid if system != "win32" else None,
            creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                           if system == "win32" else 0),
        )
        self._is_recording = True
        self._start_time = time.monotonic()

        # Start reading stderr in a background thread for progress
        import threading
        self._reader_thread = threading.Thread(
            target=self._read_stderr, daemon=True,
        )
        self._reader_thread.start()

        # Liveness check: if ffmpeg exits within the first 2 seconds, it
        # almost certainly failed to open the camera or encode.  Poll with
        # short sleeps so we don't delay the happy path.
        for _ in (range(10) if wait_for_ready else range(0)):
            time.sleep(0.2)
            if self._process.poll() is not None:
                rc = self._process.returncode
                self._is_recording = False
                # Drain remaining stderr so _read_stderr logs the errors
                self._reader_thread.join(timeout=2)
                # Clean up empty output file if ffmpeg created one
                if self._output_path and self._output_path.exists():
                    try:
                        self._output_path.unlink()
                    except OSError:
                        pass
                if self._srt_path and self._srt_path.exists():
                    try:
                        self._srt_path.unlink()
                    except OSError:
                        pass
                raise RuntimeError(
                    f"ffmpeg exited with code {rc} — camera may be busy, "
                    f"or the resolution/FPS combination is unsupported. "
                    f"Check the log for details."
                )

        return self._output_path

    def wait_until_ready(self, timeout_seconds: float = 2.0) -> None:
        """
        Verify ffmpeg stays alive through its startup window.

        Synchronized multi-camera start calls this only after all recorder
        processes have been launched, so one camera's startup check does not
        delay the next camera.
        """
        if self._process is None:
            raise RuntimeError("Recording process has not been started")

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            time.sleep(0.05)
            if self._process.poll() is not None:
                rc = self._process.returncode
                self._is_recording = False
                if hasattr(self, "_reader_thread"):
                    self._reader_thread.join(timeout=2)
                for path in (self._output_path, self._srt_path, self._metadata_path):
                    if path and path.exists():
                        try:
                            path.unlink()
                        except OSError:
                            pass
                raise RuntimeError(
                    f"ffmpeg exited with code {rc} - camera may be busy, "
                    f"or the resolution/FPS combination is unsupported. "
                    f"Check the log for details."
                )

    def stop(self) -> tuple[Path, Path]:
        """
        Stop recording gracefully.

        Returns (mp4_path, srt_path).
        """
        if not self._is_recording or self._process is None:
            raise RuntimeError("Not recording")

        logger.info("Stopping recording for camera %s", self.camera_name)

        # Send 'q' to ffmpeg's stdin for graceful shutdown
        try:
            if self._process.stdin:
                self._process.stdin.write("q\n")
                self._process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        stop_request_time = time.monotonic()

        # Wait with timeout, then force-kill if needed
        try:
            self._process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg didn't exit gracefully, sending SIGTERM")
            self._kill_process()

        self._is_recording = False
        if hasattr(self, "_reader_thread"):
            self._reader_thread.join(timeout=2)

        wall_duration = stop_request_time - self._start_time
        progress_frames = self.frame_count
        video_duration, video_frames = self._probe_output_video()
        if self._frame_wall_times:
            self.duration_seconds = (
                self._frame_wall_times[-1] - self._frame_wall_times[0]
            )
            self.frame_count = len(self._frame_wall_times)
        else:
            self.duration_seconds = wall_duration
            if video_frames is not None:
                self.frame_count = video_frames

        logger.info(
            "Recording timing [%s]: video=%.3fs/%d frames, wall=%.3fs",
            self.camera_name,
            video_duration if video_duration is not None else -1.0,
            video_frames if video_frames is not None else -1,
            wall_duration,
        )

        # Finalize SRT timestamps
        if self._ts_writer:
            if self._frame_wall_times:
                self._ts_writer.finalize_absolute_times(self._frame_wall_times)
            else:
                self._ts_writer.finalize(self.duration_seconds, self.frame_count)
            self._ts_writer = None
        self._write_metadata(
            wall_duration, progress_frames, video_duration, video_frames,
        )

        # Verify output
        if self._output_path and self._output_path.exists():
            size_mb = self._output_path.stat().st_size / (1024 * 1024)
            logger.info("Recording saved: %s (%.1f MB, %.1f s)",
                        self._output_path, size_mb, self.duration_seconds)
        else:
            logger.error("Output file not found: %s", self._output_path)

        return (self._output_path or Path(), self._srt_path or Path())

    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def output_path(self) -> Path | None:
        """The MP4 output path, or None if recording hasn't started."""
        return self._output_path

    @property
    def srt_path(self) -> Path | None:
        """The SRT timestamp file path, or None if recording hasn't started."""
        return self._srt_path

    @property
    def metadata_path(self) -> Path | None:
        """The JSON metadata path, or None if recording hasn't started."""
        return self._metadata_path

    # ── internals ─────────────────────────────────────────────────────

    def _build_command(self, resolution: tuple[int, int], fps: int,
                       encoder: str, output_path: Path) -> list[str]:
        """Assemble the ffmpeg command line.

        When *encoder* is ``"copy"`` the camera's native compressed stream
        (e.g. MJPEG) is stream-copied to the MP4 container without decoding
        or re-encoding, preserving the camera's true framerate.
        """
        w, h = resolution
        system = sys.platform
        ffmpeg = get_ffmpeg_path()
        is_passthrough = encoder == "copy"

        # Platform-specific input
        if system == "darwin":
            input_args = [
                "-f", "avfoundation",
                "-use_wallclock_as_timestamps", "1",
                "-framerate", str(fps),
                "-video_size", f"{w}x{h}",
                "-i", self.camera_id,
            ]
        elif system == "win32":
            # camera_id is already the full dshow device specifier
            # e.g. 'video=Integrated Camera' (NO shell quotes — subprocess list mode)
            input_args = [
                "-f", "dshow",
                "-use_wallclock_as_timestamps", "1",
                # Large real-time buffer prevents frame drops at high FPS.
                # Default is tiny (~30 MB); 2000 MB is safe for 120fps streams.
                "-rtbufsize", "2000M",
                # Thread queue size for the input demuxer — larger values
                # smooth out jitter at high framerates.
                "-thread_queue_size", "1024",
            ]
            # When copying, request the camera's native codec explicitly
            # (must come after -f dshow, before -framerate)
            if is_passthrough and self.native_codec:
                input_args.extend(["-vcodec", self.native_codec])
            input_args.extend([
                "-framerate", str(fps),
                "-video_size", f"{w}x{h}",
                "-i", self.camera_id,
            ])
        else:
            input_args = [
                "-f", "v4l2",
                "-use_wallclock_as_timestamps", "1",
                "-framerate", str(fps),
                "-video_size", f"{w}x{h}",
                "-i", self.camera_id,
            ]

        # Codec args
        if is_passthrough:
            # -vsync 0 (passthrough): pass frames through unchanged — never drop
            # or duplicate.  Essential for preserving the camera's true framerate.
            codec_args = ["-c:v", "copy", "-vsync", "0"]
        elif "videotoolbox" in encoder:
            codec_args = [
                "-c:v", encoder,
                "-allow_sw", "1",        # allow software fallback
                "-pix_fmt", "nv12",       # VideoToolbox requires NV12
                "-b:v", "5M",
            ]
        elif "nvenc" in encoder or "amf" in encoder:
            codec_args = ["-c:v", encoder, "-b:v", "5M"]
        elif "vaapi" in encoder:
            codec_args = ["-c:v", encoder, "-b:v", "5M"]
        else:
            # Software encoding with reasonable defaults
            codec_args = [
                "-c:v", encoder,
                "-preset", "medium",
                "-crf", "23",
            ]

        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "info", "-debug_ts",
            *input_args,
            *codec_args,
            # No audio (video-only from camera)
            "-an",
            # Overwrite output
            "-y",
        ]
        # Keyframe interval only for re-encoding; copy preserves original GOP
        if not is_passthrough:
            cmd.append("-g")
            cmd.append(str(fps * 2))
        cmd.append(str(output_path))
        return cmd

    def _read_stderr(self) -> None:
        """Parse ffmpeg stderr for progress and error information.

        All stderr lines are logged so that startup failures (camera busy,
        unsupported resolution/FPS combo, etc.) are visible rather than
        silently swallowed.
        """
        if self._process is None or self._process.stderr is None:
            return
        error_lines: list[str] = []
        for line in self._process.stderr:
            line = line.strip()
            if not line:
                continue
            # Detect error-level messages
            lower = line.lower()
            if any(kw in lower for kw in (
                "error", "invalid", "cannot", "failed", "denied",
                "no such", "unable", "i/o error", "permission",
            )):
                error_lines.append(line)
                logger.error("ffmpeg [%s]: %s", self.camera_name, line)
            elif "demuxer ->" in line and "pkt_pts_time:" in line:
                self._record_frame_timestamp(line)
            elif "warning" in lower:
                logger.warning("ffmpeg [%s]: %s", self.camera_name, line)
            elif "frame=" in line:
                # Parse frame count for progress tracking
                match = __import__('re').search(r'frame=\s*(\d+)', line)
                if match:
                    self.frame_count = int(match.group(1))
                # Log progress periodically
                if self.frame_count % 300 == 0:
                    logger.debug("Progress [%s]: %s", self.camera_name, line)
            else:
                logger.debug("ffmpeg [%s]: %s", self.camera_name, line)

        # If stderr closed with errors and zero frames, the process likely
        # failed at startup — flag it so callers can detect the failure.
        if error_lines and self.frame_count == 0:
            logger.error(
                "ffmpeg [%s] exited with errors and 0 frames — recording failed. "
                "Errors: %s",
                self.camera_name, "; ".join(error_lines[-5:]),
            )

    def _kill_process(self) -> None:
        """Force-kill the ffmpeg process."""
        if self._process is None:
            return
        try:
            if sys.platform == "win32":
                self._process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
            self._process.wait(timeout=5)
        except Exception:
            try:
                self._process.kill()
                self._process.wait(timeout=3)
            except Exception:
                pass

    def _record_frame_timestamp(self, line: str) -> None:
        """Capture per-frame wall-clock timestamps emitted by ffmpeg debug_ts."""
        match = re.search(r"\bpkt_pts_time:([+-]?\d+(?:\.\d+)?)", line)
        if not match:
            return
        try:
            pts_time = float(match.group(1))
        except ValueError:
            return
        # With -use_wallclock_as_timestamps this should be POSIX epoch time.
        # Ignore normalized/container-relative values; they are not absolute
        # experimental timestamps.
        if pts_time < 946684800.0:  # 2000-01-01 UTC
            return
        if self._frame_wall_times and pts_time <= self._frame_wall_times[-1]:
            return
        self._frame_wall_times.append(pts_time)

    def _probe_output_video(self) -> tuple[float | None, int | None]:
        """Return final MP4 duration and video packet count when available."""
        if self._output_path is None or not self._output_path.exists():
            return None, None

        cmd = [
            get_ffmpeg_path(),
            "-hide_banner",
            "-i", str(self._output_path),
            "-map", "0:v:0",
            "-c", "copy",
            "-f", "null",
            "-",
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except Exception:
            logger.exception("Could not probe output video: %s", self._output_path)
            return None, None

        output = (proc.stderr or "") + (proc.stdout or "")
        duration = self._parse_ffmpeg_duration(output)
        frames = self._parse_ffmpeg_frame_count(output)
        if duration is None or frames is None:
            logger.warning(
                "Incomplete video probe for %s: duration=%s frames=%s",
                self._output_path,
                duration,
                frames,
            )
        return duration, frames

    @staticmethod
    def _parse_ffmpeg_duration(output: str) -> float | None:
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
        if not match:
            return None
        hours = int(match.group(1))
        minutes = int(match.group(2))
        seconds = float(match.group(3))
        return hours * 3600 + minutes * 60 + seconds

    @staticmethod
    def _parse_ffmpeg_frame_count(output: str) -> int | None:
        matches = re.findall(r"\bframe=\s*(\d+)", output)
        if not matches:
            return None
        return int(matches[-1])

    def _write_metadata(
        self,
        wall_duration: float,
        progress_frames: int,
        video_duration: float | None,
        video_frames: int | None,
    ) -> None:
        """Write a sidecar record that keeps experimental timing auditable."""
        if self._metadata_path is None:
            return

        frame_count = self.frame_count
        real_fps = frame_count / wall_duration if wall_duration > 0 else None
        container_fps = (
            video_frames / video_duration
            if video_duration and video_frames
            else None
        )
        duration_delta = (
            wall_duration - video_duration
            if video_duration is not None
            else None
        )
        warnings: list[str] = []
        if video_frames is not None and video_frames != progress_frames:
            warnings.append(
                f"ffmpeg progress frames ({progress_frames}) != "
                f"container frames ({video_frames})"
            )
        if duration_delta is not None and abs(duration_delta) > 0.5:
            warnings.append(
                "container duration differs from monotonic recording duration; "
                "SRT uses monotonic experimental time"
            )

        metadata = {
            "format_version": 1,
            "camera": {
                "id": self.camera_id,
                "name": self.camera_name,
                "native_codec": self.native_codec,
            },
            "requested": {
                "resolution": self._requested_resolution,
                "fps": self._requested_fps,
                "codec": self._requested_codec,
            },
            "files": {
                "video": str(self._output_path) if self._output_path else None,
                "srt": str(self._srt_path) if self._srt_path else None,
            },
            "experimental_timing": {
                "source": (
                    "ffmpeg_demuxer_wallclock_pts"
                    if self._frame_wall_times
                    else "monotonic_clock"
                ),
                "duration_seconds": wall_duration,
                "frame_count": frame_count,
                "ffmpeg_progress_frame_count": progress_frames,
                "mean_fps": real_fps,
                "frame_timestamps": (
                    "per_frame"
                    if self._frame_wall_times
                    else "uniform_estimate_over_monotonic_duration"
                ),
                "note": (
                    "SRT timestamps use ffmpeg demuxer packet wallclock PTS "
                    "when available; otherwise they fall back to recorder "
                    "monotonic-clock duration. They are not overwritten by "
                    "MP4 container timing."
                ),
            },
            "container_timing": {
                "source": "mp4_probe",
                "duration_seconds": video_duration,
                "frame_count": video_frames,
                "mean_fps": container_fps,
            },
            "diagnostics": {
                "duration_delta_seconds": duration_delta,
                "warnings": warnings,
            },
        }
        try:
            self._metadata_path.write_text(
                json.dumps(metadata, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            logger.exception("Could not write metadata: %s", self._metadata_path)
