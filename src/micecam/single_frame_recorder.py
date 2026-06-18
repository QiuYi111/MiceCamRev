"""
Windows Media Foundation single-frame recorder wrapper.

The Python side owns process lifecycle and metadata.  Frame acquisition stays
inside the C++ helper so Media Foundation COM, QPC timestamping, and writer
thread behavior are isolated from the PyQt process.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

HELPER_EXE = "mf_single_frame_helper.exe"
HELPER_VERSION = "0.1.0"


class SingleFrameRecorder:
    """Manage a Windows-only Media Foundation single-frame helper process."""

    def __init__(
        self,
        camera_id: str,
        camera_name: str = "",
        output_dir: Path = Path("./output"),
        camera_device_number: int | None = None,
        pixel_format: str = "auto",
        ring_buffer_capacity: int = 256,
    ) -> None:
        self.camera_id = camera_id
        self.camera_name = camera_name
        self.output_dir = Path(output_dir)
        self.camera_device_number = camera_device_number
        self.pixel_format = pixel_format
        self.ring_buffer_capacity = ring_buffer_capacity

        self._process: Optional[subprocess.Popen] = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._ready_event = threading.Event()
        self._is_recording = False

        self._session_dir: Path | None = None
        self._frame_log_path: Path | None = None
        self._metadata_path: Path | None = None
        self._helper_path: Path | None = None
        self._helper_exit_code: int | None = None

        self._requested_resolution: tuple[int, int] | None = None
        self._requested_fps: int | None = None
        self._wall_start: float | None = None
        self._steady_start: int | None = None
        self._start_time: float = 0.0
        self._stop_time: float | None = None

        self.is_ready = False
        self.frame_count = 0
        self.drop_count = 0
        self.qpc_frequency: int | None = None
        self.capture_format: str | None = None
        self.stop_reason: str | None = None
        self.warnings: list[str] = []
        self._failure_reason: str | None = None

    def start(
        self,
        resolution: tuple[int, int] = (1920, 1080),
        fps: int = 30,
        codec: str = "h264",
        wall_start: float | None = None,
        steady_start: int | None = None,
        wait_for_ready: bool = True,
    ) -> Path:
        """Start the Media Foundation helper and return the session directory."""
        del codec  # single_frame_mode stores native frames, not encoded video.
        if self._is_recording:
            raise RuntimeError("Already recording")
        if sys.platform != "win32":
            raise RuntimeError("single_frame_mode is Windows-only")

        helper_path = self._resolve_helper_path()
        if helper_path is None:
            raise RuntimeError(
                f"single_frame_mode helper not found: {HELPER_EXE}. "
                "Build/package mf_single_frame_helper.exe and place it next to "
                "MiceCam.exe or under helpers/ during development."
            )

        self._prepare_paths()
        self._helper_path = helper_path
        self._requested_resolution = resolution
        self._requested_fps = fps
        self._wall_start = wall_start if wall_start is not None else time.time()
        self._steady_start = steady_start if steady_start is not None else time.monotonic_ns()
        self._failure_reason = None
        self.stop_reason = None
        self.warnings = []
        self.frame_count = 0
        self.drop_count = 0
        self.is_ready = False
        self._ready_event.clear()

        cmd = self._build_helper_command(
            helper_path=helper_path,
            resolution=resolution,
            fps=fps,
            wall_start=self._wall_start,
            steady_start=self._steady_start,
        )
        logger.info("single_frame_mode helper command: %s", subprocess.list2cmdline(cmd))

        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        self._is_recording = True
        self._start_time = time.monotonic()

        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

        if wait_for_ready and not self._ready_event.wait(timeout=5.0):
            if self._process.poll() is not None:
                self._helper_exit_code = self._process.returncode
                self._is_recording = False
                self._mark_failed(f"helper exited during startup with code {self._helper_exit_code}")
            else:
                self._mark_failed("helper did not report ready within 5 seconds")
                self._terminate_helper()
            self._join_reader_threads()
            self._write_metadata()
            raise RuntimeError(f"single_frame_mode failed to start: {self._failure_reason}")

        return self._session_dir or Path()

    def wait_until_ready(self, timeout_seconds: float = 2.0) -> None:
        """Verify the helper has reported ready."""
        if self._process is None:
            raise RuntimeError("Recording process has not been started")
        if not self._ready_event.wait(timeout=timeout_seconds):
            if self._process.poll() is not None:
                self._helper_exit_code = self._process.returncode
                self._is_recording = False
                self._mark_failed(f"helper exited during startup with code {self._helper_exit_code}")
            else:
                self._mark_failed("helper did not report ready")
                self._terminate_helper()
                self._join_reader_threads()
            raise RuntimeError(f"single_frame_mode failed to start: {self._failure_reason}")
        # Ready event fired — but check if an error slipped in before/after
        if self._failure_reason:
            self._is_recording = False
            self._terminate_helper()
            self._join_reader_threads()
            raise RuntimeError(f"single_frame_mode failed to start: {self._failure_reason}")

    def stop(self) -> tuple[Path, Path]:
        """Stop the helper, flush metadata, and return (session_dir, frame_log)."""
        if not self._is_recording or self._process is None:
            raise RuntimeError("Not recording")

        try:
            if self._process.stdin:
                self._process.stdin.write("q\n")
                self._process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._mark_failed("helper did not exit after stop request")
            self._terminate_helper()

        self._helper_exit_code = self._process.returncode
        self._is_recording = False
        self._stop_time = time.monotonic()
        if self._stdout_thread:
            self._stdout_thread.join(timeout=2)
        if self._stderr_thread:
            self._stderr_thread.join(timeout=2)
        if self._helper_exit_code not in (0, None):
            message = f"helper exited with code {self._helper_exit_code}"
            if self._failure_reason is None:
                self._mark_failed(message)
            elif message not in self.warnings:
                self.warnings.append(message)
                self.warnings = self.warnings[-20:]

        self._write_metadata()
        if self._failure_reason:
            # Distinguish startup failure (0 frames, never really recorded)
            # from mid-recording failure (had frames, then crashed).
            if self.frame_count == 0:
                logger.warning("single_frame_mode stopped with startup failure: %s",
                               self._failure_reason)
            else:
                raise RuntimeError(self._failure_reason)
        return (self._session_dir or Path(), self._frame_log_path or Path())

    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def output_path(self) -> Path | None:
        """The single-frame session directory."""
        return self._session_dir

    @property
    def srt_path(self) -> Path | None:
        """Compatibility path used by existing UI; points to frame_log.csv."""
        return self._frame_log_path

    @property
    def metadata_path(self) -> Path | None:
        return self._metadata_path

    @property
    def ffmpeg_log_path(self) -> Path | None:
        return None

    @property
    def last_error(self) -> str | None:
        return self._failure_reason

    def _prepare_paths(self) -> None:
        date_str = time.strftime("%Y-%m-%d")
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        safe_name = self.camera_name.replace(" ", "_").replace('"', "")[:30]
        stem = f"{safe_name}_{timestamp}" if safe_name else f"cam_{timestamp}"
        session_dir = self.output_dir / safe_name / date_str / f"{stem}_single_frames"
        session_dir.mkdir(parents=True, exist_ok=True)
        self._session_dir = session_dir
        self._frame_log_path = session_dir / "frame_log.csv"
        self._metadata_path = session_dir / "metadata.json"

    def _resolve_helper_path(self) -> Path | None:
        candidates: list[Path] = []
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            candidates.append(Path(sys._MEIPASS) / HELPER_EXE)  # type: ignore[attr-defined]
        root = Path(__file__).resolve().parents[2]
        candidates.extend([
            root / "helpers" / HELPER_EXE,
            root / "build" / HELPER_EXE,
            root / "build" / "Release" / HELPER_EXE,
            root / "helpers" / "build" / "Release" / HELPER_EXE,
        ])
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _build_helper_command(
        self,
        helper_path: Path,
        resolution: tuple[int, int],
        fps: int,
        wall_start: float,
        steady_start: int,
    ) -> list[str]:
        if self._session_dir is None:
            raise RuntimeError("session directory has not been initialized")
        w, h = resolution
        # The helper uses --camera-name (if non-empty) for MF device
        # enumeration, falling back to --camera-id.  camera_name may
        # carry a "#1"/"#2" display suffix that doesn't match the
        # driver's friendly name.  Use camera_id exclusively so the
        # helper's strip_dshow_prefix logic handles device matching;
        # device_number disambiguates same-name cameras.
        cmd = [
            str(helper_path),
            "--camera-id", self.camera_id,
            "--width", str(w),
            "--height", str(h),
            "--fps", str(fps),
            "--pixel-format", self.pixel_format,
            "--output-dir", str(self._session_dir),
            "--ring-buffer-capacity", str(self.ring_buffer_capacity),
            "--shared-wall-start", repr(wall_start),
            "--shared-steady-start-ns", str(steady_start),
        ]
        if self.camera_device_number is not None:
            cmd.extend(["--device-number", str(self.camera_device_number)])
        return cmd

    def _read_stdout(self) -> None:
        if self._process is None or self._process.stdout is None:
            return
        for line in self._process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("single_frame_mode helper emitted non-JSON stdout: %s", line)
                continue
            self._handle_helper_event(event)

    def _read_stderr(self) -> None:
        if self._process is None or self._process.stderr is None:
            return
        for line in self._process.stderr:
            line = line.strip()
            if line:
                logger.warning("single_frame_mode helper stderr: %s", line)

    def _handle_helper_event(self, event: dict) -> None:
        kind = str(event.get("event", ""))
        if kind == "ready":
            self.is_ready = True
            if "qpc_frequency" in event:
                self.qpc_frequency = int(event["qpc_frequency"])
            if "format" in event:
                self.capture_format = str(event["format"])
            self._ready_event.set()
        elif kind == "frame_count":
            self.frame_count = int(event.get("frame_count", self.frame_count))
        elif kind == "dropped":
            self.drop_count = int(event.get("drop_count", self.drop_count))
        elif kind == "warning":
            message = str(event.get("message", "helper warning"))
            self.warnings.append(message)
            self.warnings = self.warnings[-20:]
        elif kind == "error":
            self._mark_failed(str(event.get("message", "helper error")))
            # Do NOT set _ready_event on error — the helper failed to start.
            # wait_until_ready() will time out and raise a clear startup error.
        elif kind == "stopped":
            self.stop_reason = str(event.get("stop_reason", "helper_stopped"))
            self.frame_count = int(event.get("frame_count", self.frame_count))
            self.drop_count = int(event.get("drop_count", self.drop_count))
            self._ready_event.set()

    def _terminate_helper(self) -> None:
        if self._process is None or self._process.poll() is not None:
            return
        try:
            self._process.terminate()
            self._process.wait(timeout=3)
        except Exception as exc:
            logger.warning("Could not terminate single_frame_mode helper: %s", exc)
            try:
                self._process.kill()
                self._process.wait(timeout=3)
            except Exception as kill_exc:
                logger.warning("Could not kill single_frame_mode helper: %s", kill_exc)
                pass
        self._helper_exit_code = self._process.returncode
        self._is_recording = False

    def _join_reader_threads(self) -> None:
        if self._stdout_thread:
            self._stdout_thread.join(timeout=2)
        if self._stderr_thread:
            self._stderr_thread.join(timeout=2)

    def _mark_failed(self, reason: str) -> None:
        if self._failure_reason is None:
            self._failure_reason = reason

    def _write_metadata(self) -> None:
        if self._metadata_path is None:
            return
        duration = None
        if self._start_time:
            end = self._stop_time if self._stop_time is not None else time.monotonic()
            duration = max(0.0, end - self._start_time)
        metadata = {
            "format_version": 1,
            "backend": "single_frame_mode",
            "helper": {
                "version": HELPER_VERSION,
                "path": str(self._helper_path) if self._helper_path else None,
                "exit_code": self._helper_exit_code,
            },
            "camera": {
                "id": self.camera_id,
                "name": self.camera_name,
                "device_number": self.camera_device_number,
            },
            "capture": {
                "resolution": self._requested_resolution,
                "fps": self._requested_fps,
                "pixel_format": self.pixel_format,
                "reported_format": self.capture_format,
                "ring_buffer_capacity": self.ring_buffer_capacity,
                "qpc_frequency": self.qpc_frequency,
            },
            "shared_time_base": {
                "wall_start": self._wall_start,
                "steady_start_ns": self._steady_start,
            },
            "files": {
                "session_dir": str(self._session_dir) if self._session_dir else None,
                "frame_log": str(self._frame_log_path) if self._frame_log_path else None,
                "frames_bin": str((self._session_dir / "frames.bin")) if self._session_dir else None,
                "frames_index": str((self._session_dir / "frames.idx.csv")) if self._session_dir else None,
            },
            "timestamp_semantics": {
                "alignment_timestamp": "arrival_qpc_ns",
                "diagnostic_timestamp": "mf_pts_100ns",
                "note": (
                    "arrival_qpc_ns is recorded immediately after IMFSourceReader "
                    "returns a sample. It is an arrival timestamp, not a sensor "
                    "exposure timestamp."
                ),
            },
            "diagnostics": {
                "status": "failed" if self._failure_reason else "ok",
                "failure_reason": self._failure_reason,
                "frame_count": self.frame_count,
                "drop_count": self.drop_count,
                "warnings": self.warnings,
                "stop_reason": self.stop_reason,
                "duration_seconds": duration,
            },
        }
        try:
            self._metadata_path.parent.mkdir(parents=True, exist_ok=True)
            self._metadata_path.write_text(
                json.dumps(metadata, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            logger.exception("Could not write single_frame_mode metadata: %s", self._metadata_path)
