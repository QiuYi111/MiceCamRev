"""
Camera enumeration and capability querying.

Cross-platform: macOS (AVFoundation), Windows (dshow), Linux (v4l2).
Falls back to probing common resolution/framerate combos when the platform
doesn't provide direct enumeration.
"""

from __future__ import annotations

import logging
import platform
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Common resolution / framerate presets ──────────────────────────────────
COMMON_RESOLUTIONS = [
    (3840, 2160),  # 4K UHD
    (2560, 1440),  # 2K QHD
    (1920, 1080),  # Full HD
    (1280, 720),   # HD
    (960, 540),    # qHD
    (640, 480),    # VGA
    (320, 240),    # QVGA
]

COMMON_FRAMERATES = [60, 30, 25, 24, 15, 10, 5]

# Hardware-accelerated encoders in preference order per platform
ENCODER_PRIORITY: dict[str, list[str]] = {
    "Darwin": ["h264_videotoolbox", "hevc_videotoolbox"],
    "Windows": ["h264_amf", "hevc_amf", "h264_nvenc", "hevc_nvenc"],
    "Linux": ["h264_vaapi", "hevc_vaapi"],
}
FALLBACK_ENCODERS = ["libx264", "libx265"]


@dataclass
class CameraInfo:
    """Metadata for a detected camera."""
    index: int
    name: str
    platform_id: str  # platform-specific identifier (e.g. AVFoundation index)
    supported_resolutions: list[tuple[int, int]] = field(default_factory=list)
    supported_framerates: list[int] = field(default_factory=list)


@dataclass
class EncoderInfo:
    """Available encoder with its properties."""
    name: str
    hardware_accelerated: bool
    codec: str  # "h264" or "hevc"


def get_ffmpeg_path() -> str:
    """Return the path to ffmpeg, searching bundled location first."""
    import sys as _sys

    exe_name = "ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg"

    # 1. PyInstaller bundle: ffmpeg placed at MEIPASS root
    if getattr(_sys, "frozen", False) and hasattr(_sys, "_MEIPASS"):
        bundled = Path(_sys._MEIPASS) / exe_name
        if bundled.exists():
            return str(bundled)

    # 2. Development: project /ffmpeg/ directory
    bundled = Path(__file__).parent.parent.parent / "ffmpeg" / exe_name
    if bundled.exists():
        return str(bundled)

    # 3. System PATH
    return exe_name if platform.system() == "Windows" else "ffmpeg"


def _run_ffmpeg(args: list[str], timeout: float = 10) -> str:
    """Run ffmpeg and return combined stderr+stdout as string."""
    ffmpeg = get_ffmpeg_path()
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return (proc.stderr or "") + (proc.stdout or "")
    except FileNotFoundError:
        logger.error("ffmpeg not found at %s", ffmpeg)
        return ""
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg timed out: %s", args)
        return ""
    except Exception:
        logger.exception("ffmpeg unexpected error: %s", args)
        return ""


# ── Platform-specific device listing ──────────────────────────────────────

def _list_devices_darwin() -> list[CameraInfo]:
    """List AVFoundation cameras on macOS."""
    # -list_devices must come before -f (generic option, not format-specific)
    output = _run_ffmpeg(["-list_devices", "true", "-f", "avfoundation", "-i", ""])

    cameras: list[CameraInfo] = []
    in_video_section = False
    for line in output.splitlines():
        line = line.strip()
        if "AVFoundation video devices:" in line:
            in_video_section = True
            continue
        if "AVFoundation audio devices:" in line:
            break
        if in_video_section:
            # Parse lines like:
            # [AVFoundation indev @ ...] [0] MacBook Pro Camera
            # The device index is in the second bracket group
            match = re.search(r'\]\s*\[(\d+)\]\s+(.+)', line)
            if match:
                idx = int(match.group(1))
                name = match.group(2).strip('"')
                cameras.append(CameraInfo(
                    index=idx, name=name,
                    platform_id=str(idx),
                ))
    return cameras


def _list_devices_windows() -> list[CameraInfo]:
    """List dshow cameras on Windows.

    Uses ``-list_devices true`` (ffmpeg 7+) to enumerate video sources.
    The flag must come *before* ``-f dshow`` so ffmpeg treats it as a
    generic option, not a dshow-specific one.
    """
    output = _run_ffmpeg(["-list_devices", "true", "-f", "dshow", "-i", "dummy"])

    if not output.strip():
        logger.warning("ffmpeg -list_devices returned empty output")
        return []

    logger.debug("dshow device listing output:\n%s", output)

    cameras: list[CameraInfo] = []
    in_video_section = False
    for line in output.splitlines():
        line = line.strip()
        # Detect section boundaries
        if "DirectShow video devices" in line:
            in_video_section = True
            continue
        if "DirectShow audio devices" in line:
            break

        if in_video_section:
            # Try two patterns:
            # 1. "Camera Name" (video)   ← standard dshow output
            # 2. Alternative: [dshow @ ...]  "Camera Name"
            match = re.search(r'"(.+?)"\s*\(video\)', line)
            if not match:
                # Try alternative: quoted name anywhere in the line
                match = re.search(r'"([^"]+)"', line)
            if match:
                name = match.group(1)
                idx = len(cameras)
                cameras.append(CameraInfo(
                    index=idx, name=name,
                    platform_id=f'video="{name}"',
                ))

    logger.info("Found %d dshow camera(s)", len(cameras))
    return cameras


def _list_devices_linux() -> list[CameraInfo]:
    """List v4l2 cameras on Linux."""
    output = _run_ffmpeg(["-f", "v4l2", "-list_devices", "true", "-i", "dummy"])
    cameras: list[CameraInfo] = []
    for line in output.splitlines():
        match = re.search(r'\[video4linux2[^]]*\]\s+(.+)', line)
        if match:
            name = match.group(1).strip()
            idx = len(cameras)
            cameras.append(CameraInfo(
                index=idx, name=name,
                platform_id=name,
            ))
    return cameras


# ── Capability probing ────────────────────────────────────────────────────

def _probe_capability(platform_id: str, width: int, height: int,
                      fps: int, platform_name: str) -> bool:
    """Test whether a camera supports a given resolution/fps combo.

    Prefer ``_query_caps_windows`` on Windows — this is a slow fallback.
    """
    if platform_name == "Darwin":
        args = [
            "-f", "avfoundation",
            "-framerate", str(fps),
            "-video_size", f"{width}x{height}",
            "-i", platform_id,
            "-vframes", "1", "-f", "null", "-"
        ]
    elif platform_name == "Windows":
        args = [
            "-f", "dshow",
            "-framerate", str(fps),
            "-video_size", f"{width}x{height}",
            "-i", platform_id,
            "-vframes", "1", "-f", "null", "-"
        ]
    else:  # Linux
        args = [
            "-f", "v4l2",
            "-framerate", str(fps),
            "-video_size", f"{width}x{height}",
            "-i", platform_id,
            "-vframes", "1", "-f", "null", "-"
        ]
    output = _run_ffmpeg(args, timeout=8)
    success = "Error" not in output and "Invalid" not in output
    logger.debug("Probe %s %dx%d@%d → %s", platform_id, width, height, fps,
                 "OK" if success else "FAIL")
    return success


def _query_caps_windows(platform_id: str) -> tuple[list[tuple[int, int]], list[int]]:
    """Query supported resolutions & framerates via ``-list_options true``.

    Much faster than probing — ffmpeg directly enumerates the dshow pin caps.
    Returns (resolutions, framerates).
    """
    output = _run_ffmpeg(
        ["-f", "dshow", "-list_options", "true", "-i", platform_id],
        timeout=10,
    )

    resolutions: set[tuple[int, int]] = set()
    framerates: set[int] = set()

    # Parse lines like:
    #   pixel_format=mjpeg  min s=320x240 fps=15 max s=1920x1080 fps=30
    #   pixel_format=yuyv422 min s=640x480 fps=30 max s=640x480 fps=30
    for line in output.splitlines():
        # Look for max resolution/fps at the end of capability lines
        match = re.search(r'max\s+s=(\d+)x(\d+)\s+fps=(\d+)', line)
        if match:
            w, h, fps = int(match.group(1)), int(match.group(2)), int(match.group(3))
            resolutions.add((w, h))
            framerates.add(fps)
        # Some lines have only one resolution (min == max omitted)
        match = re.search(r's=(\d+)x(\d+)\s+fps=(\d+)', line)
        if match:
            w, h, fps = int(match.group(1)), int(match.group(2)), int(match.group(3))
            resolutions.add((w, h))
            framerates.add(fps)

    res_list = sorted(resolutions, key=lambda r: (-r[0], -r[1]))  # highest first
    fps_list = sorted(framerates, reverse=True)
    logger.info("dshow caps for %s: res=%s fps=%s", platform_id, res_list, fps_list)
    return res_list, fps_list


def _query_camera_caps(camera: CameraInfo, platform_name: str) -> None:
    """Probe which resolutions/framerates the camera supports.

    On Windows, uses the fast ``-list_options`` path.
    On macOS/Linux, falls back to probe-each-combo (slower but works).
    """
    if platform_name == "Windows":
        res, fps = _query_caps_windows(camera.platform_id)
        if res:
            camera.supported_resolutions = res
            camera.supported_framerates = fps
            return
        # Fall through to probing if list_options gave nothing
        logger.warning("_query_caps_windows empty, falling back to probing")

    # Slow path: probe each common resolution/fps combo
    supported_res = []
    for w, h in COMMON_RESOLUTIONS:
        if _probe_capability(camera.platform_id, w, h, 30, platform_name):
            supported_res.append((w, h))
    if not supported_res:
        supported_res = [(640, 480)]

    supported_fps = []
    test_res = supported_res[0]
    for fps in COMMON_FRAMERATES:
        if _probe_capability(camera.platform_id, test_res[0], test_res[1],
                             fps, platform_name):
            supported_fps.append(fps)
    if not supported_fps:
        supported_fps = [30]

    camera.supported_resolutions = supported_res
    camera.supported_framerates = supported_fps


# ── Encoder detection ─────────────────────────────────────────────────────

def get_available_encoders() -> list[EncoderInfo]:
    """Detect which hardware + software encoders are usable."""
    output = _run_ffmpeg(["-encoders"], timeout=5)
    system = platform.system()
    encoders: list[EncoderInfo] = []

    # Check hardware encoders for current platform
    hw_names = ENCODER_PRIORITY.get(system, [])
    for name in hw_names:
        if name in output:
            codec = "hevc" if "hevc" in name else "h264"
            encoders.append(EncoderInfo(
                name=name, hardware_accelerated=True, codec=codec,
            ))

    # Always add software fallbacks
    for name in FALLBACK_ENCODERS:
        if name in output:
            codec = "hevc" if "265" in name else "h264"
            encoders.append(EncoderInfo(
                name=name, hardware_accelerated=False, codec=codec,
            ))

    return encoders


# ── Public API ─────────────────────────────────────────────────────────────

def list_cameras(probe_capabilities: bool = True) -> list[CameraInfo]:
    """
    List all available cameras with their capabilities.

    Args:
        probe_capabilities: If True, probe each camera for supported
                            resolutions and framerates.
    """
    system = platform.system()
    if system == "Darwin":
        cameras = _list_devices_darwin()
    elif system == "Windows":
        cameras = _list_devices_windows()
    else:
        cameras = _list_devices_linux()

    if probe_capabilities:
        for cam in cameras:
            logger.info("Probing capabilities for camera %d: %s", cam.index, cam.name)
            _query_camera_caps(cam, system)

    return cameras


def get_preferred_encoder(for_codec: str = "h264") -> str:
    """
    Return the best available encoder for the given codec.

    Prefers hardware-accelerated encoders, falls back to software.
    """
    available = get_available_encoders()
    system = platform.system()
    priority = ENCODER_PRIORITY.get(system, []) + FALLBACK_ENCODERS

    # Filter by codec
    for name in priority:
        codec_type = "hevc" if ("hevc" in name or "265" in name) else "h264"
        if codec_type == for_codec and any(e.name == name for e in available):
            return name

    # Ultimate fallback
    return "libx264" if for_codec == "h264" else "libx265"
