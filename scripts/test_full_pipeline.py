"""
Full-pipeline test harness for MiceCam dual-camera recording system.

Covers:
  1. Record capability: 720p@60fps — actual fps, format, memory/disk pressure
  2. Timestamp quality: dual-camera ffmpeg vs single-frame comparison
  3. Local packaging: PyInstaller build

Usage::

    uv run python scripts/test_full_pipeline.py
    uv run python scripts/test_full_pipeline.py --test 1         # record capability only
    uv run python scripts/test_full_pipeline.py --test 2         # timestamp quality only
    uv run python scripts/test_full_pipeline.py --test 3         # packaging only
    uv run python scripts/test_full_pipeline.py --duration 20    # 20s recording
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("test_full_pipeline")

# ── Project paths ────────────────────────────────────────────────────────
PROJECT = Path(__file__).resolve().parents[1]
OUTPUT_BASE = PROJECT / "test_output"
FFMPEG = PROJECT / "ffmpeg" / "ffmpeg.exe"

# ── Camera info (pre-computed) ───────────────────────────────────────────
CAM0_ID = "video=LRCP  V1080P-60fps"
CAM0_NAME = "LRCP_V1080P-60fps_#1"
CAM0_DEV = 0
CAM1_ID = "video=LRCP  V1080P-60fps"
CAM1_NAME = "LRCP_V1080P-60fps_#2"
CAM1_DEV = 1

# ═══════════════════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════════════════

def get_memory_usage() -> dict:
    """Return current process memory in MB."""
    try:
        import psutil
        proc = psutil.Process()
        mem = proc.memory_info()
        return {
            "rss_mb": mem.rss / (1024 * 1024),
            "vms_mb": mem.vms / (1024 * 1024),
            "pct": proc.memory_percent(),
        }
    except ImportError:
        return {"rss_mb": -1, "vms_mb": -1, "pct": -1}


def get_disk_usage(path: Path) -> dict:
    """Return disk usage for the output directory."""
    try:
        import shutil
        usage = shutil.disk_usage(str(path))
        return {
            "total_gb": usage.total / (1024**3),
            "used_gb": usage.used / (1024**3),
            "free_gb": usage.free / (1024**3),
        }
    except Exception:
        return {"total_gb": -1, "used_gb": -1, "free_gb": -1}


def probe_mp4(path: Path) -> dict:
    """Probe an MP4 file for video duration and frame count."""
    cmd = [
        str(FFMPEG), "-hide_banner",
        "-i", str(path),
        "-map", "0:v:0", "-c", "copy",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except Exception as exc:
        logger.warning("Probe failed for %s: %s", path, exc)
        return {"duration_s": None, "frame_count": None, "fps": None}
    output = (proc.stderr or "") + (proc.stdout or "")

    import re
    dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    duration = None
    if dur_match:
        duration = (int(dur_match.group(1)) * 3600 +
                    int(dur_match.group(2)) * 60 +
                    float(dur_match.group(3)))

    frame_match = re.findall(r"\bframe=\s*(\d+)", output)
    frames = int(frame_match[-1]) if frame_match else None

    fps = frames / duration if (frames and duration and duration > 0) else None
    return {"duration_s": duration, "frame_count": frames, "fps": fps}


# ═══════════════════════════════════════════════════════════════════════════
# Test 1: Record Capability — 720p@60fps
# ═══════════════════════════════════════════════════════════════════════════

class MemoryMonitor:
    """Background thread that samples memory usage at regular intervals."""

    def __init__(self, interval: float = 0.5):
        self.interval = interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.samples: list[dict] = []

    def start(self, time_ref: float) -> None:
        self._start_ref = time_ref
        self._running = True
        self.samples = []
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> list[dict]:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        return self.samples

    def _loop(self) -> None:
        while self._running:
            t = time.perf_counter()
            try:
                mem = get_memory_usage()
                mem["elapsed_s"] = t - self._start_ref
            except Exception:
                pass
            else:
                self.samples.append(mem)
            time.sleep(self.interval)


def _build_ffmpeg_record_cmd(
    camera_id: str,
    device_number: int,
    resolution: tuple[int, int],
    fps: int,
    native_codec: str,
    output_path: Path,
    duration: float,
) -> list[str]:
    """Build ffmpeg command for recording with passthrough."""
    w, h = resolution
    cmd = [
        str(FFMPEG), "-hide_banner", "-loglevel", "info", "-debug_ts",
        "-f", "dshow",
        "-video_device_number", str(device_number),
        "-rtbufsize", "2000M",
        "-thread_queue_size", "1024",
        "-vcodec", native_codec,
        "-framerate", str(fps),
        "-video_size", f"{w}x{h}",
        "-i", camera_id,
        "-c:v", "copy", "-vsync", "0",
        "-an", "-y",
        "-t", str(duration),
        str(output_path),
    ]
    return cmd


def test_record_capability(duration: float = 30.0) -> dict:
    """
    Test 1: Record 720p@60fps and measure actual performance.

    Records from camera 0 at 1280×720 @ 60 fps with MJPEG passthrough.
    Monitors memory usage, disk I/O, and verifies actual frame rate.
    """
    logger.info("=" * 60)
    logger.info("TEST 1: Record Capability — 720p @ 60fps MJPEG passthrough")
    logger.info("=" * 60)

    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUTPUT_BASE / f"test1_record_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_mp4 = out_dir / "cam0_720p60.mp4"
    out_log = out_dir / "ffmpeg.log"

    resolution = (1280, 720)
    fps = 60
    native_codec = "mjpeg"

    # Pre-recording disk state
    disk_before = get_disk_usage(out_dir)
    logger.info("Disk before: free=%.1f GB", disk_before.get("free_gb", -1))

    cmd = _build_ffmpeg_record_cmd(
        CAM0_ID, CAM0_DEV, resolution, fps, native_codec, out_mp4, duration,
    )
    logger.info("ffmpeg cmd: %s", subprocess.list2cmdline(cmd))

    # Start memory monitor
    t0 = time.perf_counter()
    monitor = MemoryMonitor(interval=0.1)
    monitor.start(t0)

    # Run ffmpeg
    t_start = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=int(duration) + 30,
        )
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out!")
        return {"status": "timeout", "output_dir": str(out_dir)}
    t_end = time.perf_counter()
    wall_duration = t_end - t_start

    mem_samples = monitor.stop()

    # Save ffmpeg log
    out_log.write_text((proc.stderr or "") + "\n" + (proc.stdout or ""),
                       encoding="utf-8", errors="replace")
    logger.info("Wall duration: %.2f s", wall_duration)
    logger.info("ffmpeg return code: %d", proc.returncode)

    # Probe output
    probe = probe_mp4(out_mp4)
    file_size_mb = out_mp4.stat().st_size / (1024**2) if out_mp4.exists() else 0
    disk_after = get_disk_usage(out_dir)

    # Extract per-frame PTS timestamps from ffmpeg debug_ts.
    # Only capture from "demuxer+ffmpeg" lines — those carry the
    # offset-adjusted, relative-to-stream-start PTS.
    frame_pts: list[float] = []
    for line in (proc.stderr or "").splitlines():
        if "demuxer+ffmpeg" not in line:
            continue
        m = re.search(r"pkt_pts_time:([+-]?\d+(?:\.\d+)?)", line)
        if m:
            frame_pts.append(float(m.group(1)))

    # Memory stats
    rss_values = [s["rss_mb"] for s in mem_samples if s["rss_mb"] > 0]
    peak_rss = max(rss_values) if rss_values else 0
    avg_rss = sum(rss_values) / len(rss_values) if rss_values else 0

    # Inter-frame interval analysis
    if len(frame_pts) >= 2:
        intervals = [b - a for a, b in zip(frame_pts, frame_pts[1:])]
        expected_interval = 1.0 / fps
        jitter = [abs(v - expected_interval) for v in intervals]
        avg_interval = sum(intervals) / len(intervals)
        max_jitter = max(jitter)
        avg_jitter = sum(jitter) / len(jitter)
        actual_fps = 1.0 / avg_interval if avg_interval > 0 else 0
    else:
        intervals, avg_interval, max_jitter, avg_jitter, actual_fps = [], 0, 0, 0, 0

    # Build report
    report = {
        "test": "record_capability",
        "status": "ok" if proc.returncode == 0 and out_mp4.exists() else "failed",
        "output_dir": str(out_dir),
        "output_mp4": str(out_mp4),
        "ffmpeg_log": str(out_log),
        "requested": {"resolution": "1280x720", "fps": 60, "codec": "mjpeg"},
        "wall_duration_s": round(wall_duration, 3),
        "ffmpeg_exit_code": proc.returncode,
        "file_size_mb": round(file_size_mb, 2),
        "probe": probe,
        "timing": {
            "frame_count": len(frame_pts),
            "actual_fps": round(actual_fps, 2),
            "avg_interval_ms": round(avg_interval * 1000, 3),
            "expected_interval_ms": round(1000 / fps, 3),
            "avg_jitter_ms": round(avg_jitter * 1000, 3),
            "max_jitter_ms": round(max_jitter * 1000, 3),
        },
        "memory": {
            "peak_rss_mb": round(peak_rss, 1),
            "avg_rss_mb": round(avg_rss, 1),
            "sample_count": len(rss_values),
        },
        "disk": {
            "before_free_gb": round(disk_before.get("free_gb", -1), 1),
            "after_free_gb": round(disk_after.get("free_gb", -1), 1),
            "consumed_mb": round(file_size_mb, 2),
            "write_rate_mbps": round(file_size_mb / wall_duration * 8, 2) if wall_duration > 0 else 0,
        },
    }

    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                          encoding="utf-8")

    # Print summary
    logger.info("─" * 40)
    logger.info("TEST 1 RESULTS:")
    logger.info("  Status:        %s", report["status"])
    logger.info("  File size:     %.2f MB", file_size_mb)
    logger.info("  Actual FPS:    %.2f (requested: %d)", actual_fps, fps)
    logger.info("  Avg interval:  %.3f ms (expected: %.3f ms)",
                avg_interval * 1000, 1000 / fps)
    logger.info("  Avg jitter:    %.3f ms", avg_jitter * 1000)
    logger.info("  Max jitter:    %.3f ms", max_jitter * 1000)
    logger.info("  Peak RSS:      %.1f MB", peak_rss)
    logger.info("  Write rate:    %.2f Mbps", report["disk"]["write_rate_mbps"])
    logger.info("  Report:        %s", report_path)
    logger.info("─" * 40)

    return report


# ═══════════════════════════════════════════════════════════════════════════
# Test 2: Timestamp Quality — Dual-camera ffmpeg vs single-frame
# ═══════════════════════════════════════════════════════════════════════════

class SingleFrameCapture:
    """
    Python-based single-frame capture using OpenCV (cv2).

    Simulates the Media Foundation single-frame mode: grabs individual frames
    and records arrival QPC timestamps.  The timestamp semantics match the
    C++ helper: timestamps are recorded immediately after the frame grab
    returns (arrival timestamp, not exposure timestamp).
    """

    def __init__(self, camera_index: int, name: str = ""):
        self.camera_index = camera_index
        self.name = name
        self._cap: "cv2.VideoCapture | None" = None  # type: ignore
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.frames: list[dict] = []
        self.drop_count = 0

    def start(self, resolution: tuple[int, int] = (1280, 720),
              fps: int = 60) -> None:
        import cv2
        w, h = resolution
        self._cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        self._cap.set(cv2.CAP_PROP_FPS, fps)
        # Request MJPEG format (fourcc='MJPG') for compressed frames
        self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))

        # Read back actual settings
        actual_w = self._cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        actual_h = self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        actual_fps = self._cap.get(cv2.CAP_PROP_FPS)
        actual_fourcc = int(self._cap.get(cv2.CAP_PROP_FOURCC))
        fourcc_str = "".join(chr((actual_fourcc >> (8 * i)) & 0xFF) for i in range(4))
        logger.info("Single-frame [%s]: actual %dx%d @ %.1ffps codec=%s",
                    self.name, actual_w, actual_h, actual_fps, fourcc_str)

        self._actual_res = (int(actual_w), int(actual_h))
        self._actual_fps = actual_fps
        self._actual_codec = fourcc_str
        self._running = True
        self.frames = []
        self.drop_count = 0
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def stop(self) -> list[dict]:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        if self._cap:
            self._cap.release()
            self._cap = None
        return self.frames

    def _capture_loop(self) -> None:
        import cv2
        assert self._cap is not None
        frame_id = 0
        last_arrival = 0.0
        while self._running:
            ret = self._cap.grab()
            arrival_qpc = time.perf_counter()  # arrival timestamp

            if not ret:
                self.drop_count += 1
                continue

            ret, img = self._cap.retrieve()
            if not ret:
                self.drop_count += 1
                continue

            frame_id += 1
            delta_ms = ((arrival_qpc - last_arrival) * 1000) if last_arrival else 0.0
            last_arrival = arrival_qpc

            self.frames.append({
                "frame_id": frame_id,
                "arrival_qpc": arrival_qpc,
                "delta_ms": round(delta_ms, 6),
                "width": img.shape[1],
                "height": img.shape[0],
            })

    @property
    def actual_resolution(self) -> tuple[int, int]:
        return getattr(self, "_actual_res", (0, 0))

    @property
    def actual_fps(self) -> float:
        return getattr(self, "_actual_fps", 0.0)

    @property
    def actual_codec(self) -> str:
        return getattr(self, "_actual_codec", "")


def _run_ffmpeg_recorder(
    camera_id: str, device_number: int,
    resolution: tuple[int, int], fps: int, codec: str,
    output_path: Path, duration: float,
    log_path: Path,
) -> tuple[subprocess.Popen, threading.Thread]:
    """Launch ffmpeg recorder and continuously drain stderr to a file.

    ``-debug_ts`` produces ~200 lines/sec at 60 fps.
    The 64 KB pipe buffer would deadlock ffmpeg if not drained.
    A background thread reads stderr and writes it to *log_path*.
    """
    w, h = resolution
    cmd = [
        str(FFMPEG), "-hide_banner", "-loglevel", "info", "-debug_ts",
        "-f", "dshow",
        "-video_device_number", str(device_number),
        "-rtbufsize", "2000M",
        "-thread_queue_size", "1024",
        "-vcodec", codec,
        "-framerate", str(fps),
        "-video_size", f"{w}x{h}",
        "-i", camera_id,
        "-c:v", "copy", "-vsync", "0",
        "-an", "-y",
        "-t", str(duration),
        str(output_path),
    ]
    logger.info("ffmpeg cmd: %s", subprocess.list2cmdline(cmd))

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )

    # Background thread: drain stderr → file to avoid pipe buffer deadlock
    def _drain() -> None:
        with open(str(log_path), "w", encoding="utf-8", errors="replace") as f:
            if proc.stderr:
                for line in proc.stderr:
                    f.write(line)
                    f.flush()

    drain_thread = threading.Thread(target=_drain, daemon=True)
    drain_thread.start()
    return proc, drain_thread


def _read_ffmpeg_pts(proc: subprocess.Popen) -> list[float]:
    """Drain ffmpeg stderr and extract normalized PTS timestamps.

    Only captures from ``demuxer+ffmpeg`` lines — those have the
    offset-adjusted, relative-to-stream-start PTS.  The raw ``demuxer ->``
    lines carry absolute system-uptime values (~78856 s).
    """
    import re
    pts_times: list[float] = []
    if proc.stderr is None:
        return pts_times
    for line in proc.stderr:
        if "demuxer+ffmpeg" not in line:
            continue
        m = re.search(r"pkt_pts_time:([+-]?\d+(?:\.\d+)?)", line)
        if m:
            pts_times.append(float(m.group(1)))
    return pts_times


def test_timestamp_quality(duration: float = 30.0) -> dict:
    """
    Test 2: Simultaneous dual-camera recording — timestamp quality comparison.

    Camera A (index 0): ffmpeg passthrough mode, PTS timestamps from ffmpeg debug_ts
    Camera B (index 1): Python single-frame mode, QPC arrival timestamps

    Both cameras share a wall-clock start reference for cross-modal comparison.
    """
    logger.info("=" * 60)
    logger.info("TEST 2: Timestamp Quality — Dual-camera comparison")
    logger.info("  Camera A: ffmpeg passthrough (PTS timestamps)")
    logger.info("  Camera B: Python single-frame (QPC arrival timestamps)")
    logger.info("=" * 60)

    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUTPUT_BASE / f"test2_timestamp_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Two identical cameras on the same USB bus cannot stream
    # simultaneously at high bandwidth.  Record sequentially to compare
    # timestamp quality between ffmpeg PTS and single-frame QPC modes.
    # Each stream uses its own wall_start for independent time bases;
    # we compare the *interval statistics*, not absolute timestamps.
    resolution = (1280, 720)
    fps = 60
    codec = "mjpeg"

    # ── Phase 1: Camera A (device 1) — ffmpeg passthrough ──
    wall_start = time.time()
    steady_start = time.monotonic_ns()
    logger.info("Phase 1 (ffmpeg) shared time base: wall=%.6f steady=%d", wall_start, steady_start)

    mp4_a = out_dir / "camA_ffmpeg.mp4"
    log_a = out_dir / "camA_ffmpeg.log"
    t_a0 = time.perf_counter()
    cmd_a = _build_ffmpeg_record_cmd(
        CAM1_ID, CAM1_DEV, resolution, fps, codec, mp4_a, duration,
    )
    logger.info("Phase 1 ffmpeg cmd: %s", subprocess.list2cmdline(cmd_a))
    t_a1 = time.perf_counter()

    logger.info("Recording ffmpeg (device %d) for %.1f seconds...", CAM1_DEV, duration)
    ffmpeg_exit_code = -1
    try:
        proc_res = subprocess.run(
            cmd_a,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=int(duration) + 15,
        )
        t_a_end = time.perf_counter()
        ffmpeg_exit_code = proc_res.returncode
        ffmpeg_stderr = (proc_res.stderr or "") + (proc_res.stdout or "")
        log_a.write_text(ffmpeg_stderr, encoding="utf-8", errors="replace")
        logger.info("Phase 1 ffmpeg exited with code %d, log %d bytes",
                    ffmpeg_exit_code, len(ffmpeg_stderr))
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg timed out!")
        t_a_end = time.perf_counter()
        ffmpeg_stderr = ""

    # ── Phase 2: Camera B (device 0) — single-frame (OpenCV) ──
    logger.info("Waiting 2s for USB bus to settle...")
    time.sleep(2.0)

    wall_start_b = time.time()
    steady_start_b = time.monotonic_ns()
    logger.info("Phase 2 (single-frame) shared time base: wall=%.6f steady=%d", wall_start_b, steady_start_b)

    t_b0 = time.perf_counter()
    sf_cap = SingleFrameCapture(0, CAM0_NAME)
    sf_cap.start(resolution=resolution, fps=fps)
    t_b1 = time.perf_counter()
    logger.info("Phase 2 single-frame (device 0) launched in %.1f ms", (t_b1 - t_b0) * 1000)

    logger.info("Recording single-frame for %.1f seconds...", duration)
    time.sleep(duration)
    sf_frames = sf_cap.stop()
    t_b_end = time.perf_counter()

    # Parse ffmpeg PTS timestamps from the already-saved log
    import re
    ffmpeg_pts: list[float] = []
    for line in ffmpeg_stderr.splitlines():
        if "demuxer+ffmpeg" not in line:
            continue
        m = re.search(r"pkt_pts_time:([+-]?\d+(?:\.\d+)?)", line)
        if m:
            ffmpeg_pts.append(float(m.group(1)))

    # ── Probe ffmpeg output ──
    probe = probe_mp4(mp4_a)
    file_size_mb = mp4_a.stat().st_size / (1024**2) if mp4_a.exists() else 0

    # ── Timing analysis ──
    def analyze_frame_timing(
        timestamps: list[float],
        label: str,
        expected_interval: float,
    ) -> dict:
        if len(timestamps) < 2:
            return {"label": label, "frame_count": len(timestamps), "error": "too few frames"}

        intervals = [b - a for a, b in zip(timestamps, timestamps[1:])]
        jitter = [abs(v - expected_interval) for v in intervals]

        # Sort intervals for percentile analysis
        sorted_intervals = sorted(intervals)
        n = len(sorted_intervals)
        p50 = sorted_intervals[n // 2]
        p95 = sorted_intervals[int(n * 0.95)]
        p99 = sorted_intervals[int(n * 0.99)]

        # Detect frame drops: gap > 2x expected
        drops = sum(1 for v in intervals if v > expected_interval * 2.0)
        duplicates = sum(1 for v in intervals if v < expected_interval * 0.1)

        return {
            "label": label,
            "frame_count": len(timestamps),
            "expected_interval_ms": round(expected_interval * 1000, 3),
            "avg_interval_ms": round((sum(intervals) / len(intervals)) * 1000, 3),
            "median_interval_ms": round(p50 * 1000, 3),
            "p95_interval_ms": round(p95 * 1000, 3),
            "p99_interval_ms": round(p99 * 1000, 3),
            "min_interval_ms": round(min(intervals) * 1000, 3),
            "max_interval_ms": round(max(intervals) * 1000, 3),
            "avg_jitter_ms": round((sum(jitter) / len(jitter)) * 1000, 3),
            "max_jitter_ms": round(max(jitter) * 1000, 3),
            "std_interval_ms": round(
                (sum((v - (sum(intervals) / len(intervals)))**2 for v in intervals) / len(intervals)) ** 0.5 * 1000,
                3,
            ),
            "likely_drops": drops,
            "likely_dups": duplicates,
            "drop_rate_pct": round(drops / len(intervals) * 100, 2),
        }

    expected_interval = 1.0 / fps

    # PTS-based analysis (relative PTS times)
    ffmpeg_pts_analysis = analyze_frame_timing(ffmpeg_pts, "ffmpeg_PTS", expected_interval)

    # QPC-based analysis (relative arrival times from wall_start_b)
    if sf_frames:
        sf_qpc = [f["arrival_qpc"] - wall_start_b for f in sf_frames]
        sf_analysis = analyze_frame_timing(sf_qpc, "single_frame_QPC", expected_interval)
    else:
        sf_analysis = {"label": "single_frame_QPC", "error": "no frames captured"}

    # ── Cross-stream comparison ──
    # Both streams are recorded at different times, so we compare
    # interval statistics, not absolute timestamps.
    cross_analysis = {
        "note": "Sequential recording — compare interval statistics, not absolute time",
        "ffmpeg_frame_count": len(ffmpeg_pts),
        "single_frame_count": len(sf_frames),
        "frame_count_ratio": round(len(ffmpeg_pts) / max(1, len(sf_frames)), 2),
        "ffmpeg_duration_s": round(ffmpeg_pts[-1] - ffmpeg_pts[0], 3) if len(ffmpeg_pts) >= 2 else None,
        "single_frame_duration_s": round(
            (sf_frames[-1]["arrival_qpc"] - sf_frames[0]["arrival_qpc"]), 3
        ) if sf_frames else None,
    }

    # ── Save per-frame data ──
    frames_csv = out_dir / "frame_timestamps.csv"
    with open(frames_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["stream", "frame_id", "elapsed_s", "interval_ms"])
        for i, pts in enumerate(ffmpeg_pts):
            elapsed = pts - ffmpeg_pts[0] if ffmpeg_pts else 0
            interval = (ffmpeg_pts[i + 1] - pts) * 1000 if i + 1 < len(ffmpeg_pts) else 0
            writer.writerow(["ffmpeg_PTS", i + 1, round(elapsed, 6), round(interval, 3)])
        for f in sf_frames:
            elapsed = f["arrival_qpc"] - wall_start_b
            writer.writerow(["single_frame_QPC", f["frame_id"], round(elapsed, 6),
                            f["delta_ms"]])

    # ── Build report ──
    report = {
        "test": "timestamp_quality",
        "status": "ok" if (mp4_a.exists() and sf_frames) else "partial",
        "output_dir": str(out_dir),
        "recording_strategy": "sequential (USB bandwidth limitation prevents simultaneous dual-camera at 720p60)",
        "phase_1_ffmpeg": {
            "wall_start": wall_start,
            "steady_start_ns": steady_start,
            "launch_ms": round((t_a1 - t_a0) * 1000, 2),
            "wall_duration_s": round(t_a_end - t_a0, 2),
        },
        "phase_2_single_frame": {
            "wall_start": wall_start_b,
            "steady_start_ns": steady_start_b,
            "launch_ms": round((t_b1 - t_b0) * 1000, 2),
            "wall_duration_s": round(t_b_end - t_b0, 2),
        },
        "ffmpeg": {
            "output_mp4": str(mp4_a),
            "file_size_mb": round(file_size_mb, 2),
            "exit_code": ffmpeg_exit_code,
            "probe": probe,
            **ffmpeg_pts_analysis,
        },
        "single_frame": {
            "requested_format": f"{sf_cap.actual_resolution[0]}x{sf_cap.actual_resolution[1]}",
            "actual_codec": sf_cap.actual_codec,
            "drop_count": sf_cap.drop_count,
            **sf_analysis,
        },
        "cross_comparison": cross_analysis,
    }

    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                          encoding="utf-8")

    # Print summary
    logger.info("─" * 40)
    logger.info("TEST 2 RESULTS:")
    logger.info("  Strategy:       sequential (USB bandwidth limitation)")

    # ffmpeg stats
    fa = ffmpeg_pts_analysis
    if "error" not in fa:
        logger.info("  ffmpeg PTS:  %d frames, avg=%.3f ms, jitter=%.3f ms, drops=%d",
                    fa["frame_count"], fa["avg_interval_ms"], fa["avg_jitter_ms"],
                    fa.get("likely_drops", 0))
    else:
        logger.info("  ffmpeg PTS:  %s", fa.get("error"))

    # Single-frame stats
    if "error" not in sf_analysis:
        sa = sf_analysis
        logger.info("  single QPC:  %d frames, avg=%.3f ms, jitter=%.3f ms, drops=%d",
                    sa["frame_count"], sa["avg_interval_ms"], sa["avg_jitter_ms"],
                    sa.get("likely_drops", 0))
    else:
        logger.info("  single QPC:  %s", sf_analysis.get("error"))

    logger.info("  Report:        %s", report_path)
    logger.info("  Frame data:    %s", frames_csv)
    logger.info("─" * 40)

    return report


# ═══════════════════════════════════════════════════════════════════════════
# Test 3: Local Packaging
# ═══════════════════════════════════════════════════════════════════════════

def test_packaging() -> dict:
    """
    Test 3: Build the standalone executable via PyInstaller.

    Uses the micecam.spec file.  This test primarily validates that:
    - PyInstaller can resolve all imports
    - ffmpeg binary is bundled correctly
    - The output runs (if we can test headless)
    """
    logger.info("=" * 60)
    logger.info("TEST 3: Local Packaging — PyInstaller build")
    logger.info("=" * 60)

    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = OUTPUT_BASE / f"test3_packaging_{ts}.log"

    spec_path = PROJECT / "micecam.spec"
    if not spec_path.exists():
        logger.error("micecam.spec not found at %s", spec_path)
        return {"test": "packaging", "status": "error", "error": "spec not found"}

    # Check if the helper exe requirement can be satisfied
    spec_text = spec_path.read_text(encoding="utf-8")
    helper_in_spec = "mf_single_frame_helper.exe" in spec_text
    helper_exists = False
    if helper_in_spec:
        helper_paths = [
            PROJECT / "helpers" / "build" / "Release" / "mf_single_frame_helper.exe",
            PROJECT / "helpers" / "mf_single_frame_helper.exe",
        ]
        for hp in helper_paths:
            if hp.exists():
                helper_exists = True
                break
        if not helper_exists:
            logger.warning(
                "mf_single_frame_helper.exe not found — PyInstaller build will FAIL. "
                "Install Windows SDK and run: helpers/build_mf_helper.ps1"
            )
            logger.warning(
                "To install the Windows SDK, run Visual Studio Installer and add "
                "'Windows 10 SDK' component under 'Desktop development with C++'."
            )

    # Check PyInstaller
    try:
        import PyInstaller
        logger.info("PyInstaller version: %s", PyInstaller.__version__)
    except ImportError:
        logger.error("PyInstaller not installed")
        return {"test": "packaging", "status": "error", "error": "PyInstaller not installed"}

    # Dry-run analysis: check that PyInstaller can resolve imports
    logger.info("Running PyInstaller analysis (import check)...")
    t0 = time.perf_counter()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--clean",
        "--noconfirm",
        "--log-level", "INFO",
        str(spec_path),
    ]

    # On failure, fall back to analysis-only mode
    cmd_analysis = [
        sys.executable, "-c",
        "from PyInstaller.utils.hooks import collect_submodules; "
        "from micecam.camera_manager import list_cameras; "
        "print('All imports resolved OK')",
    ]

    logger.info("Quick import check...")
    try:
        result = subprocess.run(
            cmd_analysis,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=30,
            cwd=str(PROJECT),
        )
        import_ok = "All imports resolved OK" in result.stdout
        if not import_ok:
            logger.warning("Import check: %s", result.stdout.strip() or result.stderr.strip())
    except Exception as exc:
        logger.warning("Import check failed: %s", exc)
        import_ok = False

    t1 = time.perf_counter()
    analysis_time = t1 - t0

    # ── Attempt actual PyInstaller build ──
    build_ok = False
    build_output = ""
    exe_path = PROJECT / "dist" / "MiceCam.exe"
    exe_size_mb = 0

    if helper_in_spec and not helper_exists:
        logger.warning("Skipping full build — mf_single_frame_helper.exe not available")
        build_output = "SKIPPED: helper exe not found"
    else:
        logger.info("Running PyInstaller build (this may take 2-5 minutes)...")
        t_build = time.perf_counter()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=300,
                cwd=str(PROJECT),
            )
            build_ok = result.returncode == 0
            build_output = (result.stdout + "\n" + result.stderr)[-5000:]
            build_time = time.perf_counter() - t_build
            logger.info("PyInstaller exit code: %d, time: %.1f s",
                        result.returncode, build_time)
        except subprocess.TimeoutExpired:
            build_ok = False
            build_output = "TIMEOUT after 5 minutes"
            build_time = 0
            logger.error("PyInstaller build timed out")

        if exe_path.exists():
            exe_size_mb = exe_path.stat().st_size / (1024**2)

    # ── Build report ──
    report = {
        "test": "packaging",
        "status": "ok" if build_ok or (not helper_in_spec and import_ok) else "partial",
        "import_check_ok": import_ok,
        "import_check_time_s": round(analysis_time, 2),
        "build_ok": build_ok,
        "build_output_tail": build_output[-2000:] if build_output else "",
        "exe_path": str(exe_path) if exe_path.exists() else None,
        "exe_size_mb": round(exe_size_mb, 2) if exe_size_mb else None,
        "spec_file": str(spec_path),
        "helper_required": helper_in_spec,
        "helper_available": helper_exists,
        "warnings": [],
    }

    if helper_in_spec and not helper_exists:
        report["warnings"].append(
            "mf_single_frame_helper.exe must be built before packaging. "
            "Install Windows SDK via VS Installer → 'Desktop development with C++' → "
            "'Windows 10 SDK', then run: powershell helpers/build_mf_helper.ps1"
        )
    if not import_ok:
        report["warnings"].append("Import resolution failed — check hidden imports")

    report_path = OUTPUT_BASE / f"test3_packaging_{ts}.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                          encoding="utf-8")

    # Also save build log
    log_file.write_text(build_output, encoding="utf-8", errors="replace")

    logger.info("─" * 40)
    logger.info("TEST 3 RESULTS:")
    logger.info("  Import check:  %s", "OK" if import_ok else "FAILED")
    logger.info("  Build:         %s", "OK" if build_ok else "SKIPPED/FAILED")
    if exe_size_mb:
        logger.info("  EXE size:      %.1f MB", exe_size_mb)
    if report["warnings"]:
        for w in report["warnings"]:
            logger.warning("  [WARN] %s", w)
    logger.info("  Report:        %s", report_path)
    logger.info("─" * 40)

    return report


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(
        description="MiceCam full pipeline test harness",
    )
    parser.add_argument("--test", type=int, choices=[1, 2, 3],
                        help="Run a specific test (1=record, 2=timestamp, 3=packaging)")
    parser.add_argument("--duration", type=float, default=30.0,
                        help="Recording duration in seconds (default: 30)")
    parser.add_argument("--skip-single-frame", action="store_true",
                        help="Skip single-frame mode test (if OpenCV unavailable)")
    args = parser.parse_args()

    results: dict[str, dict] = {}

    # ── Pre-flight checks ──
    if not FFMPEG.exists():
        logger.error("ffmpeg.exe not found at %s", FFMPEG)
        return 1

    if args.test is None or args.test == 1:
        try:
            results["record_capability"] = test_record_capability(args.duration)
        except Exception as exc:
            logger.exception("Test 1 failed: %s", exc)
            results["record_capability"] = {"test": "record_capability",
                                             "status": "error", "error": str(exc)}

    if args.test is None or args.test == 2:
        if args.skip_single_frame:
            logger.warning("Skipping Test 2 (single-frame mode)")
        else:
            try:
                results["timestamp_quality"] = test_timestamp_quality(args.duration)
            except Exception as exc:
                logger.exception("Test 2 failed: %s", exc)
                results["timestamp_quality"] = {"test": "timestamp_quality",
                                                "status": "error", "error": str(exc)}

    if args.test is None or args.test == 3:
        try:
            results["packaging"] = test_packaging()
        except Exception as exc:
            logger.exception("Test 3 failed: %s", exc)
            results["packaging"] = {"test": "packaging",
                                    "status": "error", "error": str(exc)}

    # ── Overall summary ──
    logger.info("=" * 60)
    logger.info("FULL PIPELINE TEST SUMMARY")
    logger.info("=" * 60)
    all_ok = True
    for name, r in results.items():
        status = r.get("status", "unknown")
        ok = status == "ok"
        all_ok = all_ok and ok
        icon = "[OK]" if ok else ("[-]" if status == "partial" else "[FAIL]")
        logger.info("  %s %s: %s", icon, name, status)
    logger.info("=" * 60)

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
