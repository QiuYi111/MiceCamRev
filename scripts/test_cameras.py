"""
CLI camera test harness — enumerate, preview-test, and record-test
without launching the GUI.

Usage::

    uv run python scripts/test_cameras.py              # full test suite
    uv run python scripts/test_cameras.py --preview    # preview only
    uv run python scripts/test_cameras.py --record     # recording only
    uv run python scripts/test_cameras.py --fps 60     # test at 60 fps
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from micecam.camera_manager import (
    CameraInfo,
    choose_camera_input_codec,
    get_ffmpeg_path,
    list_cameras,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("test_cameras")

PREVIEW_DURATION = 5  # seconds to run each preview test
RECORD_DURATION = 5   # seconds to run each recording test
TEMP_DIR = Path.home() / "AppData" / "Local" / "Temp" / "micecam_test"


def _run_ffmpeg(args: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
    """Run ffmpeg and return the completed process."""
    ffmpeg = get_ffmpeg_path()
    return subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error"] + args,
        capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )


def test_preview(
    cam: CameraInfo,
    resolution: tuple[int, int],
    fps: int,
    label: str = "",
) -> bool:
    """Test preview capture on a single camera. Returns True on success."""
    w, h = resolution
    input_codec = choose_camera_input_codec(cam, resolution, fps)
    if not input_codec and not cam.mode_codecs:
        input_codec = cam.native_codec

    cmd = [
        "-f", "dshow",
        *(["-video_device_number", str(cam.device_number)]
          if cam.device_number is not None else []),
        "-rtbufsize", "2000M",
        "-thread_queue_size", "1024",
        *(["-vcodec", input_codec] if input_codec in {"mjpeg", "h264", "hevc"} else []),
        "-framerate", str(fps),
        "-video_size", f"{w}x{h}",
        "-i", cam.platform_id,
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-an", "-t", str(PREVIEW_DURATION), "NUL",
    ]
    logger.info("[%s] Preview test: %dx%d@%d codec=%s device=%s",
                label, w, h, fps, input_codec or "auto", cam.device_number)
    result = _run_ffmpeg(cmd, timeout=PREVIEW_DURATION + 10)
    ok = result.returncode == 0 and "error" not in result.stderr.lower()
    if ok:
        logger.info("[%s] [OK] Preview OK", label)
    else:
        logger.error("[%s] [FAIL] Preview FAILED (rc=%d): %s",
                     label, result.returncode, result.stderr.strip()[:300])
    return ok


def test_record(
    cam: CameraInfo,
    resolution: tuple[int, int],
    fps: int,
    output_path: Path,
    label: str = "",
) -> bool:
    """Test recording on a single camera. Returns True on success."""
    w, h = resolution
    input_codec = choose_camera_input_codec(cam, resolution, fps)
    if not input_codec and not cam.mode_codecs:
        input_codec = cam.native_codec

    cmd = [
        "-f", "dshow",
        "-use_wallclock_as_timestamps", "1",
        *(["-video_device_number", str(cam.device_number)]
          if cam.device_number is not None else []),
        "-rtbufsize", "2000M",
        "-thread_queue_size", "1024",
        *(["-vcodec", input_codec] if input_codec in {"mjpeg", "h264", "hevc"} else []),
        "-framerate", str(fps),
        "-video_size", f"{w}x{h}",
        "-i", cam.platform_id,
        "-c:v", "copy", "-vsync", "0",
        "-an", "-y", "-t", str(RECORD_DURATION),
        str(output_path),
    ]
    logger.info("[%s] Record test: %dx%d@%d codec=%s device=%s → %s",
                label, w, h, fps, input_codec or "auto",
                cam.device_number, output_path.name)
    result = _run_ffmpeg(cmd, timeout=RECORD_DURATION + 10)
    ok = (
        result.returncode == 0
        and output_path.exists()
        and output_path.stat().st_size > 1024
    )
    if ok:
        size_mb = output_path.stat().st_size / (1024 * 1024)
        logger.info("[%s] [OK] Recorded OK (%.1f MB)", label, size_mb)
    else:
        logger.error("[%s] [FAIL] Record FAILED (rc=%d): %s",
                     label, result.returncode, result.stderr.strip()[:300])
    return ok


def test_dual_preview(cameras: list[CameraInfo], fps: int = 30) -> bool:
    """Simultaneous preview on two cameras."""
    if len(cameras) < 2:
        logger.warning("Need ≥2 cameras for dual preview test")
        return False

    cam0, cam1 = cameras[0], cameras[1]
    res0 = _pick_preview_resolution(cam0)
    res1 = _pick_preview_resolution(cam1)
    logger.info("=== Dual preview: cam0=%dx%d@%d  cam1=%dx%d@%d ===",
                res0[0], res0[1], fps, res1[0], res1[1], fps)

    results: dict[str, bool] = {}

    def _run(label: str, cam: CameraInfo, res: tuple[int, int]) -> None:
        results[label] = test_preview(cam, res, fps, label=label)

    t0 = threading.Thread(target=_run, args=("cam0", cam0, res0), daemon=True)
    t1 = threading.Thread(target=_run, args=("cam1", cam1, res1), daemon=True)
    t0.start()
    t1.start()
    t0.join()
    t1.join()

    return all(results.values())


def test_dual_record(
    cameras: list[CameraInfo],
    resolution: tuple[int, int],
    fps: int,
) -> bool:
    """Simultaneous recording on two cameras."""
    if len(cameras) < 2:
        logger.warning("Need ≥2 cameras for dual record test")
        return False

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    cam0, cam1 = cameras[0], cameras[1]
    out0 = TEMP_DIR / f"dual_cam0_{resolution[0]}x{resolution[1]}@{fps}.mp4"
    out1 = TEMP_DIR / f"dual_cam1_{resolution[0]}x{resolution[1]}@{fps}.mp4"
    logger.info("=== Dual record: %dx%d@%d ===", resolution[0], resolution[1], fps)

    results: dict[str, bool] = {}

    def _run(label: str, cam: CameraInfo, out: Path) -> None:
        results[label] = test_record(cam, resolution, fps, out, label=label)

    t0 = threading.Thread(target=_run, args=("cam0", cam0, out0), daemon=True)
    t1 = threading.Thread(target=_run, args=("cam1", cam1, out1), daemon=True)
    t0.start()
    t1.start()
    t0.join()
    t1.join()

    return all(results.values())


def _pick_preview_resolution(cam: CameraInfo) -> tuple[int, int]:
    """Pick a small resolution with ≤30 fps for stable preview."""
    if cam.resolution_fps:
        candidates = sorted(
            cam.resolution_fps.keys(),
            key=lambda r: (r[0] * r[1], r[0], r[1]),
        )
        for res in candidates:
            fps_vals = cam.resolution_fps[res]
            if any(f <= 30 for f in fps_vals):
                return res
    return cam.supported_resolutions[-1] if cam.supported_resolutions else (640, 480)


def main() -> int:
    parser = argparse.ArgumentParser(description="MiceCam camera test harness")
    parser.add_argument("--preview", action="store_true", help="Run preview tests only")
    parser.add_argument("--record", action="store_true", help="Run recording tests only")
    parser.add_argument("--fps", type=int, default=30, help="FPS for tests (default: 30)")
    parser.add_argument("--resolution", type=str, default="",
                        help="Resolution WxH for record tests (default: auto)")
    args = parser.parse_args()

    # Discover cameras
    logger.info("Enumerating cameras...")
    cameras = list_cameras(probe_capabilities=True)
    if not cameras:
        logger.error("No cameras found!")
        return 1

    logger.info("Found %d camera(s):", len(cameras))
    for c in cameras:
        res_fps_str = ", ".join(
            f"{w}x{h}@{','.join(str(f) for f in fps)}"
            for (w, h), fps in sorted(c.resolution_fps.items())
        ) if c.resolution_fps else "n/a"
        logger.info("  [%d] %s  device_number=%s  native=%s",
                    c.index, c.name, c.device_number, c.native_codec)

    preview_ok = True
    record_ok = True

    if not args.record:
        # ── Preview tests ──────────────────────────────────────────
        # Single-camera preview (use resolution-appropriate fps)
        for cam in cameras:
            res = _pick_preview_resolution(cam)
            fps_vals = cam.resolution_fps.get(res, cam.supported_framerates) if cam.resolution_fps else cam.supported_framerates
            preview_fps = args.fps if args.fps in fps_vals else min(fps_vals)
            ok = test_preview(cam, res, preview_fps, label=f"cam{cam.index}")
            preview_ok = preview_ok and ok

        # Dual-camera preview
        if len(cameras) >= 2:
            ok = test_dual_preview(cameras, fps=30)
            preview_ok = preview_ok and ok

    if not args.preview:
        # ── Recording tests ────────────────────────────────────────
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

        if args.resolution:
            w, h = map(int, args.resolution.split("x"))
            test_resolutions = [(w, h)]
        else:
            # Test common resolutions that actually support the requested fps
            test_resolutions = [(1920, 1080), (1280, 720)]
            # Keep only resolutions that support the requested fps
            if cameras[0].resolution_fps:
                test_resolutions = [
                    r for r in test_resolutions
                    if r in cameras[0].resolution_fps and args.fps in cameras[0].resolution_fps[r]
                ]

        for res in test_resolutions:
            # Single-camera record
            for cam in cameras:
                out = TEMP_DIR / f"cam{cam.index}_{res[0]}x{res[1]}@{args.fps}.mp4"
                ok = test_record(cam, res, args.fps, out, label=f"cam{cam.index}")
                record_ok = record_ok and ok

            # Dual-camera record
            if len(cameras) >= 2:
                ok = test_dual_record(cameras, res, args.fps)
                record_ok = record_ok and ok

    all_ok = preview_ok and record_ok
    if all_ok:
        logger.info("[OK] All tests passed!")
        return 0
    else:
        logger.error("[FAIL] Some tests failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
