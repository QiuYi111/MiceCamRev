"""
ffmpeg-based video recorder with hardware encoding support.

Manages ffmpeg subprocess lifecycle, builds encoding pipelines that
prioritise the camera's native/accelerated encoder, and reports progress
via parsing ffmpeg's stderr output.
"""

from __future__ import annotations

import logging
import os
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
        self._is_recording = False

        # Progress tracking
        self.duration_seconds: float = 0.0
        self.frame_count: int = 0

    # ── public API ────────────────────────────────────────────────────

    def start(self, resolution: tuple[int, int] = (1920, 1080),
              fps: int = 30, codec: str = "h264",
              wall_start: float | None = None,
              steady_start: int | None = None) -> Path:
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

        return self._output_path

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

        # Wait with timeout, then force-kill if needed
        try:
            self._process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg didn't exit gracefully, sending SIGTERM")
            self._kill_process()

        self._is_recording = False
        self.duration_seconds = time.monotonic() - self._start_time

        # Finalize SRT timestamps
        if self._ts_writer:
            self._ts_writer.finalize(self.duration_seconds, self.frame_count)
            self._ts_writer = None

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
                "-framerate", str(fps),
                "-video_size", f"{w}x{h}",
                "-i", self.camera_id,
            ]
        elif system == "win32":
            # camera_id is already the full dshow device specifier
            # e.g. 'video=Integrated Camera' (NO shell quotes — subprocess list mode)
            input_args = [
                "-f", "dshow",
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
                "-framerate", str(fps),
                "-video_size", f"{w}x{h}",
                "-i", self.camera_id,
            ]

        # Codec args
        if is_passthrough:
            codec_args = ["-c:v", "copy"]
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
            ffmpeg, "-hide_banner", "-loglevel", "info",
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
        """Parse ffmpeg stderr for progress information."""
        if self._process is None or self._process.stderr is None:
            return
        for line in self._process.stderr:
            line = line.strip()
            # Parse: frame=  123 fps= 30 q=...
            if "frame=" in line:
                # Update frame count for timestamp tracking
                match = __import__('re').search(r'frame=\s*(\d+)', line)
                if match:
                    self.frame_count = int(match.group(1))
                # Log progress periodically
                if self.frame_count % 300 == 0:
                    logger.debug("Progress: %s", line)

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
