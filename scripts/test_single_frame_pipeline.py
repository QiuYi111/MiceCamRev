"""
Full-pipeline test for single-frame mode with mf_single_frame_helper.exe.

Tests:
  1. Record capability: dual-camera 720p@60fps — actual fps, format, memory, disk
  2. Timestamp quality: both cameras in single frame mode — compare QPC/PTS distributions
  3. Local packaging: PyInstaller with helper bundled

Usage::

    uv run python scripts/test_single_frame_pipeline.py
    uv run python scripts/test_single_frame_pipeline.py --duration 20
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("test_single_frame")

PROJECT = Path(__file__).resolve().parents[1]
OUTPUT_BASE = PROJECT / "test_output"
HELPER_EXE = PROJECT / "helpers" / "build" / "Release" / "mf_single_frame_helper.exe"

CAM0_NAME = "LRCP  V1080P-60fps"
CAM0_DEV = 0
CAM1_NAME = "LRCP  V1080P-60fps"
CAM1_DEV = 1

RESOLUTION = (1280, 720)
FPS = 60
PIXEL_FORMAT = "mjpg"
RING_BUFFER = 256


# ═══════════════════════════════════════════════════════════════════════════
# Low-level helper runner — minimal wrapper, records everything
# ═══════════════════════════════════════════════════════════════════════════

class HelperRunner:
    """Launch and control a single mf_single_frame_helper.exe process.

    Captures all stdout JSON events and the final frame_log.csv.
    """

    def __init__(self, camera_name: str, device_number: int,
                 output_dir: Path, label: str = ""):
        self.camera_name = camera_name
        self.device_number = device_number
        self.output_dir = output_dir
        self.label = label

        self._proc: Optional[subprocess.Popen] = None
        self._events: list[dict] = []
        self._stderr_lines: list[str] = []
        self._ready = threading.Event()
        self._start_time: float = 0.0
        self._stop_time: float = 0.0

        # Populated from "ready" event
        self.qpc_frequency: int = 0
        self.capture_format: str = ""
        self.actual_width: int = 0
        self.actual_height: int = 0

        # Populated from "stopped" event & frame_log
        self.frame_count: int = 0
        self.drop_count: int = 0
        self.stop_reason: str = ""
        self.frame_log_rows: list[dict] = []

    def start(self, duration: float, wall_start: float, steady_start: int) -> None:
        """Launch the helper and wait for 'ready'."""
        if self._proc is not None:
            raise RuntimeError("Already started")

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        safe_name = self.camera_name.replace(" ", "_").replace('"', "")[:30]
        session_dir = self.output_dir / f"{safe_name}_dev{self.device_number}_{timestamp}"
        session_dir.mkdir(parents=True, exist_ok=True)

        self._session_dir = session_dir
        self._frame_log_path = session_dir / "frame_log.csv"
        self._events = []
        self._ready.clear()

        w, h = RESOLUTION
        cmd = [
            str(HELPER_EXE),
            "--camera-name", self.camera_name,
            "--device-number", str(self.device_number),
            "--width", str(w), "--height", str(h),
            "--fps", str(FPS),
            "--pixel-format", PIXEL_FORMAT,
            "--output-dir", str(session_dir),
            "--ring-buffer-capacity", str(RING_BUFFER),
            "--shared-wall-start", repr(wall_start),
            "--shared-steady-start-ns", str(steady_start),
        ]
        logger.info("[%s] %s", self.label, subprocess.list2cmdline(cmd))

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        self._start_time = time.perf_counter()

        # Background threads for stdout/stderr
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

        # Wait for ready
        if not self._ready.wait(timeout=8.0):
            self._terminate()
            raise RuntimeError(f"[{self.label}] Helper did not report ready within 8s")

        logger.info("[%s] Ready: %s %dx%d qpc_freq=%d",
                    self.label, self.capture_format,
                    self.actual_width, self.actual_height, self.qpc_frequency)

    def stop(self) -> None:
        """Send 'q' to helper, wait for exit, parse frame_log."""
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.write("q\n")
                self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._terminate()

        self._stop_time = time.perf_counter()
        if self._stdout_thread:
            self._stdout_thread.join(timeout=2)
        if self._stderr_thread:
            self._stderr_thread.join(timeout=2)

        self._parse_frame_log()

    @property
    def elapsed(self) -> float:
        return self._stop_time - self._start_time if self._start_time else 0

    def _terminate(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=3)
                except Exception:
                    pass

    def _read_stdout(self) -> None:
        if not self._proc or not self._proc.stdout:
            return
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._events.append(event)
            kind = event.get("event", "")
            if kind == "ready":
                self.qpc_frequency = int(event.get("qpc_frequency", 0))
                self.capture_format = str(event.get("format", ""))
                self.actual_width = int(event.get("width", 0))
                self.actual_height = int(event.get("height", 0))
                self._ready.set()
            elif kind == "frame_count":
                self.frame_count = int(event.get("frame_count", 0))
            elif kind == "dropped":
                self.drop_count = int(event.get("drop_count", 0))
            elif kind == "stopped":
                self.frame_count = int(event.get("frame_count", 0))
                self.drop_count = int(event.get("drop_count", 0))
                self.stop_reason = str(event.get("stop_reason", ""))
            elif kind == "error":
                self._ready.set()  # unblock on error too

    def _read_stderr(self) -> None:
        if not self._proc or not self._proc.stderr:
            return
        for line in self._proc.stderr:
            line = line.strip()
            if line:
                self._stderr_lines.append(line)

    def _parse_frame_log(self) -> None:
        csv_path = self._frame_log_path
        if not csv_path.exists():
            logger.warning("[%s] No frame_log.csv found", self.label)
            return
        rows: list[dict] = []
        with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        self.frame_log_rows = rows
        if rows:
            self.frame_count = len(rows)
            logger.info("[%s] Parsed %d frame log entries", self.label, len(rows))


# ═══════════════════════════════════════════════════════════════════════════
# Analysis helpers
# ═══════════════════════════════════════════════════════════════════════════

def analyze_qpc_intervals(frame_log: list[dict], expected_hz: int = 60) -> dict:
    """Analyze QPC arrival intervals from frame_log.csv rows."""
    if len(frame_log) < 2:
        return {"error": "too few frames", "count": len(frame_log)}

    intervals_ms: list[float] = []
    deltas = []
    for row in frame_log:
        d = float(row.get("arrival_delta_ms", 0))
        if d > 0:
            intervals_ms.append(d)

    if len(intervals_ms) < 2:
        return {"error": "too few valid deltas", "count": len(intervals_ms)}

    expected_ms = 1000.0 / expected_hz
    jitter = [abs(v - expected_ms) for v in intervals_ms]
    sorted_intervals = sorted(intervals_ms)
    n = len(sorted_intervals)

    # Detect drops: gap > 2.5x expected
    drops = sum(1 for v in intervals_ms if v > expected_ms * 2.5)

    return {
        "frame_count": len(frame_log),
        "valid_intervals": len(intervals_ms),
        "expected_interval_ms": round(expected_ms, 3),
        "avg_interval_ms": round(sum(intervals_ms) / len(intervals_ms), 3),
        "median_interval_ms": round(sorted_intervals[n // 2], 3),
        "p95_interval_ms": round(sorted_intervals[int(n * 0.95)], 3),
        "p99_interval_ms": round(sorted_intervals[int(n * 0.99)], 3),
        "min_interval_ms": round(min(intervals_ms), 3),
        "max_interval_ms": round(max(intervals_ms), 3),
        "avg_jitter_ms": round(sum(jitter) / len(jitter), 3),
        "max_jitter_ms": round(max(jitter), 3),
        "std_interval_ms": round(
            (sum((v - expected_ms) ** 2 for v in intervals_ms) / len(intervals_ms)) ** 0.5, 3
        ),
        "actual_fps": round(1000.0 / (sum(intervals_ms) / len(intervals_ms)), 1),
        "likely_drops": drops,
        "drop_rate_pct": round(drops / len(intervals_ms) * 100, 2),
    }


def analyze_pts_delta(frame_log: list[dict], expected_hz: int = 60) -> dict:
    """Analyze Media Foundation PTS intervals from frame_log.csv rows."""
    if len(frame_log) < 2:
        return {"error": "too few frames", "count": len(frame_log)}

    intervals_ms: list[float] = []
    for row in frame_log:
        d = float(row.get("mf_pts_delta_ms", 0))
        if d > 0:
            intervals_ms.append(d)

    if len(intervals_ms) < 2:
        return {"error": "too few valid PTS deltas"}

    expected_ms = 1000.0 / expected_hz
    jitter = [abs(v - expected_ms) for v in intervals_ms]
    sorted_intervals = sorted(intervals_ms)
    n = len(sorted_intervals)

    return {
        "valid_intervals": len(intervals_ms),
        "expected_interval_ms": round(expected_ms, 3),
        "avg_interval_ms": round(sum(intervals_ms) / len(intervals_ms), 3),
        "median_interval_ms": round(sorted_intervals[n // 2], 3),
        "p95_interval_ms": round(sorted_intervals[int(n * 0.95)], 3),
        "p99_interval_ms": round(sorted_intervals[int(n * 0.99)], 3),
        "min_interval_ms": round(min(intervals_ms), 3),
        "max_interval_ms": round(max(intervals_ms), 3),
        "avg_jitter_ms": round(sum(jitter) / len(jitter), 3),
        "max_jitter_ms": round(max(jitter), 3),
        "actual_fps": round(1000.0 / (sum(intervals_ms) / len(intervals_ms)), 1),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Test 1: Dual-camera record capability in single-frame mode
# ═══════════════════════════════════════════════════════════════════════════

def test_record_capability(duration: float = 15.0) -> dict:
    """Launch both cameras simultaneously in single-frame mode at 720p60."""
    logger.info("=" * 60)
    logger.info("TEST 1: Dual-Camera Single-Frame Record Capability")
    logger.info("  Both cameras: %dx%d @ %d fps %s", RESOLUTION[0], RESOLUTION[1], FPS, PIXEL_FORMAT)
    logger.info("  Duration: %.1f s", duration)
    logger.info("=" * 60)

    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUTPUT_BASE / f"test1_single_frame_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Memory monitoring ──
    mem_samples: list[dict] = []
    mem_stop = threading.Event()

    def mem_monitor():
        import psutil
        proc = psutil.Process()
        t0 = time.perf_counter()
        while not mem_stop.is_set():
            m = proc.memory_info()
            mem_samples.append({
                "elapsed": round(time.perf_counter() - t0, 3),
                "rss_mb": round(m.rss / (1024 * 1024), 1),
                "vms_mb": round(m.vms / (1024 * 1024), 1),
            })
            time.sleep(0.2)

    mem_thread = threading.Thread(target=mem_monitor, daemon=True)

    # ── Shared time base ──
    wall_start = time.time()
    steady_start = time.monotonic_ns()
    logger.info("Shared time base: wall=%.6f", wall_start)

    # ── Launch both helpers simultaneously ──
    runner0 = HelperRunner(CAM0_NAME, CAM0_DEV, out_dir, "CAM0")
    runner1 = HelperRunner(CAM1_NAME, CAM1_DEV, out_dir, "CAM1")

    # Start memory monitor
    mem_thread.start()

    t_launch = time.perf_counter()

    # Launch back-to-back
    errors: list[str] = []
    for runner, dev in [(runner0, CAM0_DEV), (runner1, CAM1_DEV)]:
        try:
            runner.start(duration, wall_start, steady_start)
        except Exception as e:
            errors.append(f"Cam dev={dev}: {e}")
            logger.error("Failed to start cam dev=%d: %s", dev, e)

    launch_ms = (time.perf_counter() - t_launch) * 1000
    logger.info("Both launched in %.1f ms", launch_ms)

    # ── Record for duration ──
    logger.info("Recording for %.1f seconds...", duration)
    time.sleep(duration)

    # ── Stop both ──
    for runner in [runner0, runner1]:
        try:
            runner.stop()
        except Exception as e:
            errors.append(f"Stop error: {e}")

    mem_stop.set()
    mem_thread.join(timeout=2)

    wall_duration = time.perf_counter() - t_launch

    # ── Analyze each stream ──
    results = {}
    for runner, label in [(runner0, "cam0_dev0"), (runner1, "cam1_dev1")]:
        qpc = analyze_qpc_intervals(runner.frame_log_rows, FPS)
        pts = analyze_pts_delta(runner.frame_log_rows, FPS)
        results[label] = {
            "label": label,
            "camera_name": runner.camera_name,
            "device_number": runner.device_number,
            "session_dir": str(runner._session_dir),
            "capture_format": runner.capture_format,
            "resolution": f"{runner.actual_width}x{runner.actual_height}",
            "qpc_frequency": runner.qpc_frequency,
            "frame_count": runner.frame_count,
            "drop_count": runner.drop_count,
            "stop_reason": runner.stop_reason,
            "wall_elapsed_s": round(runner.elapsed, 2),
            "qpc_analysis": qpc,
            "pts_analysis": pts,
        }

    # ── Memory stats ──
    rss_vals = [s["rss_mb"] for s in mem_samples if s.get("rss_mb", 0) > 0]
    peak_rss = max(rss_vals) if rss_vals else 0
    avg_rss = sum(rss_vals) / len(rss_vals) if rss_vals else 0

    # ── Disk stats ──
    total_size = 0
    total_frames = 0
    for runner in [runner0, runner1]:
        total_frames += runner.frame_count
        frames_dir = runner._session_dir / "frames"
        if frames_dir.exists():
            for f in frames_dir.iterdir():
                if f.is_file():
                    total_size += f.stat().st_size

    # ── Build report ──
    report = {
        "test": "record_capability_single_frame",
        "status": "ok" if not errors else "partial",
        "output_dir": str(out_dir),
        "requested": {
            "resolution": f"{RESOLUTION[0]}x{RESOLUTION[1]}",
            "fps": FPS,
            "pixel_format": PIXEL_FORMAT,
            "ring_buffer_capacity": RING_BUFFER,
            "duration_s": duration,
        },
        "shared_time_base": {
            "wall_start": wall_start,
            "steady_start_ns": steady_start,
            "wall_start_iso": datetime.fromtimestamp(wall_start, tz=timezone.utc).isoformat(),
        },
        "launch_ms": round(launch_ms, 1),
        "wall_duration_s": round(wall_duration, 2),
        "memory": {
            "peak_rss_mb": round(peak_rss, 1),
            "avg_rss_mb": round(avg_rss, 1),
            "sample_count": len(rss_vals),
        },
        "disk": {
            "total_size_mb": round(total_size / (1024**2), 2),
            "total_frames": total_frames,
            "write_rate_mbps": round(total_size / (1024**2) / duration * 8, 2),
        },
        "cameras": results,
        "errors": errors,
    }

    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    # ── Summary ──
    logger.info("─" * 40)
    logger.info("TEST 1 RESULTS:")
    for label, r in results.items():
        qpc = r["qpc_analysis"]
        logger.info("  [%s] %s  fmt=%s %s  frames=%d drops=%d  fps=%.1f  jitter=%.2f ms",
                    label, r["camera_name"][:20], r["capture_format"], r["resolution"],
                    r["frame_count"], r["drop_count"],
                    qpc.get("actual_fps", 0), qpc.get("avg_jitter_ms", 0))
    logger.info("  Memory: peak=%.1f MB  avg=%.1f MB", peak_rss, avg_rss)
    logger.info("  Disk: %.1f MB  rate=%.1f Mbps", total_size / (1024**2), report["disk"]["write_rate_mbps"])
    if errors:
        logger.warning("  Errors: %s", errors)
    logger.info("  Report: %s", report_path)

    return report


# ═══════════════════════════════════════════════════════════════════════════
# Test 2: Timestamp quality — both cameras single-frame, compare
# ═══════════════════════════════════════════════════════════════════════════

def test_timestamp_quality(duration: float = 15.0) -> dict:
    """Both cameras in single-frame mode, compare arrival QPC & MF PTS distributions."""
    logger.info("=" * 60)
    logger.info("TEST 2: Timestamp Quality — Dual Single-Frame Comparison")
    logger.info("  Both cameras: single-frame mode (MF arrival QPC + PTS)")
    logger.info("=" * 60)

    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUTPUT_BASE / f"test2_timestamp_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Shared time base ──
    wall_start = time.time()
    steady_start = time.monotonic_ns()
    logger.info("Shared time base: wall=%.6f", wall_start)

    # ── Launch both ──
    runner0 = HelperRunner(CAM0_NAME, CAM0_DEV, out_dir, "CAM0")
    runner1 = HelperRunner(CAM1_NAME, CAM1_DEV, out_dir, "CAM1")

    t0 = time.perf_counter()
    for runner in [runner0, runner1]:
        runner.start(duration, wall_start, steady_start)
    launch_ms = (time.perf_counter() - t0) * 1000
    logger.info("Both launched in %.1f ms", launch_ms)

    logger.info("Recording for %.1f seconds...", duration)
    time.sleep(duration)

    for runner in [runner0, runner1]:
        runner.stop()

    # ── Analyze ──
    def comprehensive_analysis(rows: list[dict], label: str) -> dict:
        if len(rows) < 2:
            return {"label": label, "error": "too few frames"}

        qpc_arrival = [float(r["arrival_qpc_ns"]) for r in rows]
        mf_pts = [float(r["mf_pts_100ns"]) for r in rows]

        # QPC intervals
        qpc_deltas_ms = []
        for i in range(1, len(qpc_arrival)):
            d = (qpc_arrival[i] - qpc_arrival[i - 1]) / 1_000_000
            if d > 0:
                qpc_deltas_ms.append(d)

        # PTS intervals
        pts_deltas_ms = []
        for i in range(1, len(mf_pts)):
            d = (mf_pts[i] - mf_pts[i - 1]) / 10_000  # 100ns → ms
            if d > 0:
                pts_deltas_ms.append(d)

        expected_ms = 1000.0 / FPS

        def stats(vals, name):
            if not vals:
                return {"error": "no valid deltas"}
            sorted_v = sorted(vals)
            n = len(sorted_v)
            jit = [abs(v - expected_ms) for v in vals]
            return {
                "count": len(vals),
                "avg_ms": round(sum(vals) / len(vals), 3),
                "median_ms": round(sorted_v[n // 2], 3),
                "p95_ms": round(sorted_v[int(n * 0.95)], 3),
                "p99_ms": round(sorted_v[int(n * 0.99)], 3),
                "min_ms": round(min(vals), 3),
                "max_ms": round(max(vals), 3),
                "std_ms": round((sum((v - (sum(vals) / len(vals))) ** 2 for v in vals) / len(vals)) ** 0.5, 3),
                "avg_jitter_ms": round(sum(jit) / len(jit), 3),
                "max_jitter_ms": round(max(jit), 3),
                "actual_fps": round(1000.0 / (sum(vals) / len(vals)), 1),
                "drops_gt_2_5x": sum(1 for v in vals if v > expected_ms * 2.5),
            }

        # Write-latency analysis (write_end_qpc_ns - write_start_qpc_ns)
        write_latencies = []
        for r in rows:
            lat = float(r.get("write_latency_ms", 0))
            if lat > 0:
                write_latencies.append(lat)

        return {
            "label": label,
            "frame_count": len(rows),
            "qpc_inter_arrival": stats(qpc_deltas_ms, "qpc"),
            "pts_inter_arrival": stats(pts_deltas_ms, "pts"),
            "write_latency_ms": {
                "avg": round(sum(write_latencies) / len(write_latencies), 3) if write_latencies else 0,
                "max": round(max(write_latencies), 3) if write_latencies else 0,
            },
        }

    cam0_analysis = comprehensive_analysis(runner0.frame_log_rows, "cam0_dev0")
    cam1_analysis = comprehensive_analysis(runner1.frame_log_rows, "cam1_dev1")

    # ── Cross-camera QPC alignment ──
    cross = {}
    if runner0.frame_log_rows and runner1.frame_log_rows:
        r0_qpc = [float(r["arrival_qpc_ns"]) / 1e9 for r in runner0.frame_log_rows]
        r1_qpc = [float(r["arrival_qpc_ns"]) / 1e9 for r in runner1.frame_log_rows]

        # Both QPC timelines share the same wall_start, so they're directly comparable
        cross = {
            "cam0_qpc_start_s": round(r0_qpc[0] / 1e9 - wall_start, 6) if r0_qpc else None,
            "cam1_qpc_start_s": round(r1_qpc[0] / 1e9 - wall_start, 6) if r1_qpc else None,
            "cam0_qpc_end_s": round(r0_qpc[-1] / 1e9 - wall_start, 6) if r0_qpc else None,
            "cam1_qpc_end_s": round(r1_qpc[-1] / 1e9 - wall_start, 6) if r1_qpc else None,
            "cam0_frame_count": len(r0_qpc),
            "cam1_frame_count": len(r1_qpc),
        }

    # ── Build report ──
    report = {
        "test": "timestamp_quality_single_frame",
        "status": "ok",
        "output_dir": str(out_dir),
        "shared_time_base": {
            "wall_start": wall_start,
            "wall_start_iso": datetime.fromtimestamp(wall_start, tz=timezone.utc).isoformat(),
        },
        "launch_ms": round(launch_ms, 1),
        "cam0_analysis": cam0_analysis,
        "cam1_analysis": cam1_analysis,
        "cross_camera": cross,
        "timestamp_semantics": {
            "arrival_qpc_ns": "QPC captured immediately after IMFSourceReader::ReadSample returns",
            "mf_pts_100ns": "Media Foundation sample timestamp (100ns units)",
            "note": (
                "QPC is the arrival timestamp — when the frame reached user mode. "
                "MF PTS is the sensor/capture timestamp set by the camera driver. "
                "Both are in the same QPC domain via shared wall_start."
            ),
        },
    }

    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    # ── Summary ──
    logger.info("─" * 40)
    logger.info("TEST 2 RESULTS:")
    for analysis in [cam0_analysis, cam1_analysis]:
        qpc = analysis.get("qpc_inter_arrival", {})
        pts = analysis.get("pts_inter_arrival", {})
        logger.info("  [%s] %d frames", analysis["label"], analysis["frame_count"])
        if "error" not in qpc:
            logger.info("    QPC arrival:  fps=%.1f  avg=%.2f ms  jitter=%.2f ms  p95=%.2f ms  drops=%d",
                        qpc.get("actual_fps", 0), qpc.get("avg_ms", 0),
                        qpc.get("avg_jitter_ms", 0), qpc.get("p95_ms", 0),
                        qpc.get("drops_gt_2_5x", 0))
        if "error" not in pts:
            logger.info("    MF PTS:       fps=%.1f  avg=%.2f ms  jitter=%.2f ms  p95=%.2f ms",
                        pts.get("actual_fps", 0), pts.get("avg_ms", 0),
                        pts.get("avg_jitter_ms", 0), pts.get("p95_ms", 0))
        wl = analysis.get("write_latency_ms", {})
        logger.info("    Write latency: avg=%.3f ms  max=%.3f ms", wl.get("avg", 0), wl.get("max", 0))
    if cross:
        logger.info("  Cross: cam0=%d frames  cam1=%d frames",
                    cross.get("cam0_frame_count", 0), cross.get("cam1_frame_count", 0))
    logger.info("  Report: %s", report_path)

    return report


# ═══════════════════════════════════════════════════════════════════════════
# Test 3: Full packaging
# ═══════════════════════════════════════════════════════════════════════════

def test_packaging() -> dict:
    """Run PyInstaller build with the now-compiled helper."""
    logger.info("=" * 60)
    logger.info("TEST 3: Local Packaging — PyInstaller with helper")
    logger.info("=" * 60)

    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    spec_path = PROJECT / "micecam.spec"
    if not spec_path.exists():
        return {"test": "packaging", "status": "error", "error": "spec not found"}

    # Verify helper is in expected path for the spec
    helper_expected = PROJECT / "helpers" / "build" / "Release" / "mf_single_frame_helper.exe"
    if not helper_expected.exists():
        return {"test": "packaging", "status": "error",
                "error": f"Helper not found at {helper_expected}"}

    # Quick import check
    logger.info("Import check...")
    cmd_check = [
        sys.executable, "-c",
        "from PyInstaller.utils.hooks import collect_submodules; "
        "from micecam.camera_manager import list_cameras; "
        "from micecam.single_frame_recorder import SingleFrameRecorder; "
        "print('All imports OK')",
    ]
    result = subprocess.run(cmd_check, capture_output=True, text=True, timeout=30, cwd=str(PROJECT))
    import_ok = "All imports OK" in (result.stdout or "")

    # Full build
    logger.info("Running PyInstaller build (may take 2-5 min)...")
    t0 = time.perf_counter()
    cmd = [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", str(spec_path)]
    try:
        build_res = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=600, cwd=str(PROJECT),
        )
        build_ok = build_res.returncode == 0
        build_time = time.perf_counter() - t0
        build_output = (build_res.stdout + "\n" + build_res.stderr)[-5000:]
    except subprocess.TimeoutExpired:
        build_ok = False
        build_time = 0
        build_output = "TIMEOUT after 10 min"

    exe_path = PROJECT / "dist" / "MiceCam.exe"
    exe_size_mb = exe_path.stat().st_size / (1024**2) if exe_path.exists() else 0

    report = {
        "test": "packaging",
        "status": "ok" if build_ok else "failed",
        "import_ok": import_ok,
        "build_ok": build_ok,
        "build_time_s": round(build_time, 1),
        "exe_size_mb": round(exe_size_mb, 2),
        "exe_path": str(exe_path) if exe_path.exists() else None,
        "helper_bundled": helper_expected.exists(),
    }

    report_path = OUTPUT_BASE / f"test3_packaging_{ts}.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info("─" * 40)
    logger.info("TEST 3 RESULTS:")
    logger.info("  Import check: %s", "OK" if import_ok else "FAIL")
    logger.info("  Build:        %s (%.1f s)", "OK" if build_ok else "FAILED", build_time)
    logger.info("  EXE size:     %.1f MB", exe_size_mb)
    logger.info("─" * 40)

    return report


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(description="Single-Frame Mode Full Pipeline Test")
    parser.add_argument("--test", type=int, choices=[1, 2, 3], help="Run specific test")
    parser.add_argument("--duration", type=float, default=15.0, help="Recording duration (default: 15s)")
    parser.add_argument("--skip-packaging", action="store_true", help="Skip PyInstaller build")
    args = parser.parse_args()

    if not HELPER_EXE.exists():
        logger.error("Helper not found: %s", HELPER_EXE)
        logger.error("Build it first: helpers/build_mf_helper.ps1")
        return 1

    results = {}

    if args.test is None or args.test == 1:
        try:
            results["record_capability"] = test_record_capability(args.duration)
        except Exception as exc:
            logger.exception("Test 1 failed")
            results["record_capability"] = {"test": "record_capability", "status": "error", "error": str(exc)}

    if args.test is None or args.test == 2:
        try:
            results["timestamp_quality"] = test_timestamp_quality(args.duration)
        except Exception as exc:
            logger.exception("Test 2 failed")
            results["timestamp_quality"] = {"test": "timestamp_quality", "status": "error", "error": str(exc)}

    if (args.test is None or args.test == 3) and not args.skip_packaging:
        try:
            results["packaging"] = test_packaging()
        except Exception as exc:
            logger.exception("Test 3 failed")
            results["packaging"] = {"test": "packaging", "status": "error", "error": str(exc)}

    # Summary
    logger.info("=" * 60)
    logger.info("FULL PIPELINE SUMMARY")
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
