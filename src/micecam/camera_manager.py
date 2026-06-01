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
    device_number: int | None = None  # Windows dshow duplicate-name ordinal
    supported_resolutions: list[tuple[int, int]] = field(default_factory=list)
    supported_framerates: list[int] = field(default_factory=list)
    native_codec: str = ""  # camera's native output (e.g. "mjpeg", "yuyv422"), or "" if unknown
    # Per-resolution FPS limits: {(w,h): [fps_values]}.
    # Populated on Windows via -list_options; empty on macOS/Linux (fallback to probing).
    resolution_fps: dict[tuple[int, int], list[int]] = field(default_factory=dict)


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
    """Run ffmpeg and return combined stderr+stdout as string.

    On Windows, ``text=True`` defaults to the system ANSI code page
    (e.g. cp1252 or gbk), but ffmpeg always emits UTF-8.  We force
    UTF-8 with surrogate escaping so non-ASCII camera names (Chinese,
    Japanese, etc.) on non-UTF-8 Windows systems don't cause silent
    decode errors that would make *all* cameras invisible.
    """
    ffmpeg = get_ffmpeg_path()
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner"] + args,
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
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

    Handles two ffmpeg output formats:
    - Old (ffmpeg <7): ``[dshow @ ...] DirectShow video devices`` section header
    - New (ffmpeg 8+): ``[in#0 @ ...] "Camera Name" (video)`` flat list

    Tries two argument orders so both old and new ffmpeg builds work:
    1. ffmpeg 7+  treats ``-list_devices`` as a *generic* option (before ``-f``)
    2. ffmpeg <7 requires the *format-specific* position (after ``-f dshow``)

    **Duplicate-camera disambiguation:** When two cameras share the same
    friendly name (e.g. two identical USB cameras), the "Alternative name"
    line from ffmpeg 8+ provides a unique DirectShow device path that
    reliably identifies each physical device.  We use it as the
    ``platform_id`` so ffmpeg can distinguish them.  On older ffmpeg
    builds that lack alternative-name output we keep the friendly-name
    ``platform_id`` and set ``device_number`` so ffmpeg can disambiguate
    via ``-video_device_number``.
    """
    # ffmpeg 7+: generic option before -f
    output = _run_ffmpeg(["-list_devices", "true", "-f", "dshow", "-i", "dummy"])

    # Fallback for older ffmpeg: format-specific option after -f dshow
    if not output.strip():
        logger.debug("generic -list_devices returned empty, trying format-specific")
        output = _run_ffmpeg(["-f", "dshow", "-list_devices", "true", "-i", "dummy"])

    if not output.strip():
        logger.warning("ffmpeg -list_devices returned empty output "
                       "(both argument orders tried)")
        return []

    logger.debug("dshow device listing output:\n%s", output)

    cameras: list[CameraInfo] = []
    has_section_headers = "DirectShow video devices" in output
    in_video_section = not has_section_headers  # if no headers, parse all lines

    # Track the most recently seen *video* camera so we can associate
    # the following "Alternative name" line with it.  Clear on audio
    # devices to avoid cross-wiring their alternative names.
    pending_camera: CameraInfo | None = None

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue

        # Old-format section boundaries
        if "DirectShow video devices" in line:
            in_video_section = True
            continue
        if "DirectShow audio devices" in line:
            break

        if not in_video_section:
            continue

        # Match: "Camera Name" (video)  or  "Microphone" (audio)
        dev_match = re.search(r'"(.+?)"\s*\((\w+)\)', line)
        if dev_match:
            dev_name = dev_match.group(1)
            dev_kind = dev_match.group(2)
            if dev_kind == "video":
                idx = len(cameras)
                escaped = dev_name.replace("\\", "\\\\").replace(":", "\\:")
                pending_camera = CameraInfo(
                    index=idx, name=dev_name,
                    platform_id=f'video={escaped}',
                )
                cameras.append(pending_camera)
            else:
                # Audio device — clear pending so its Alternative name
                # doesn't overwrite the preceding video camera.
                pending_camera = None
            continue

        # New-format "Alternative name" — the unique DirectShow device
        # path (e.g. @device_pnp_\\?\usb#vid_...).  Associate it with
        # the video camera we just parsed.
        if 'Alternative name' in line and pending_camera is not None:
            alt_match = re.search(r'"(@[^"]+)"', line)
            if alt_match:
                alt = alt_match.group(1)
                pending_camera.platform_id = f'video={alt}'
                logger.debug("  unique device path: %s", alt)
            continue

    # ── Post-process: disambiguate duplicate friendly names ──
    # Two identical cameras (same model) share a friendly name.  Even when
    # we have unique hardware paths (Alternative name → platform_id), the
    # *display name* must differ so output directories and UI labels don't
    # collide.  We append #1 / #2 suffixes to every duplicate.
    name_counts: dict[str, int] = {}
    for cam in cameras:
        name_counts[cam.name] = name_counts.get(cam.name, 0) + 1

    dup_names = {n for n, c in name_counts.items() if c > 1}
    if dup_names:
        name_seen: dict[str, int] = {}
        for cam in cameras:
            if cam.name in dup_names:
                name_seen[cam.name] = name_seen.get(cam.name, 0) + 1
                suffix = f" #{name_seen[cam.name]}"
                # If the platform_id is just the naive name-based id
                # (no Alternative name available, old ffmpeg), keep the
                # real DirectShow friendly name and disambiguate with
                # -video_device_number instead of inventing a fake name.
                _escaped = cam.name.replace("\\", "\\\\").replace(":", "\\:")
                if cam.platform_id == f'video={_escaped}':
                    cam.device_number = name_seen[cam.name] - 1
                    logger.warning(
                        "Duplicate camera '%s' disambiguated with "
                        "-video_device_number %d. "
                        "Consider upgrading ffmpeg for hardware-path-based IDs.",
                        cam.name, cam.device_number,
                    )
                cam.name += suffix

    logger.info("Found %d dshow camera(s)", len(cameras))
    for c in cameras:
        logger.debug("  [%d] %s  →  %s", c.index, c.name, c.platform_id)
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
                      fps: int, platform_name: str,
                      device_number: int | None = None) -> bool:
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
            *(
                ["-video_device_number", str(device_number)]
                if device_number is not None else []
            ),
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


# Compressed codecs that can be stream-copied into MP4 without re-encoding.
_PASSTHROUGH_CODECS = frozenset({"mjpeg", "h264", "hevc"})


def _query_caps_windows(platform_id: str, device_number: int | None = None) -> tuple[list[tuple[int, int]], list[int], str, dict[tuple[int, int], list[int]]]:
    """Query supported resolutions, framerates & native codec via ``-list_options``.

    Much faster than probing — ffmpeg directly enumerates the dshow pin caps.
    Returns (resolutions, framerates, native_codec, resolution_fps_map).

    *native_codec* is the camera's preferred compressed output (e.g. ``"mjpeg"``),
    or a raw pixel format (``"yuyv422"``), or ``""`` if parsing failed.

    *resolution_fps_map* maps each (width, height) to the list of FPS values
    the camera actually supports at that resolution — this prevents the UI
    from offering impossible combinations (e.g. 1920×1080 @ 120 fps).
    """
    output = _run_ffmpeg(
        [
            "-f", "dshow",
            "-list_options", "true",
            *(
                ["-video_device_number", str(device_number)]
                if device_number is not None else []
            ),
            "-i", platform_id,
        ],
        timeout=10,
    )

    # Collect per-format capabilities: {(w,h): set(fps)} for each codec type
    mjpeg_res_fps: dict[tuple[int, int], set[int]] = {}
    raw_res_fps: dict[tuple[int, int], set[int]] = {}
    vcodecs: set[str] = set()
    pixel_formats: set[str] = set()

    # Also collect flat sets for backward compatibility
    all_resolutions: set[tuple[int, int]] = set()
    all_framerates: set[int] = set()

    for line in output.splitlines():
        # Capture the prefix: vcodec=XXX or pixel_format=XXX
        codec_match = re.search(r'\b(vcodec|pixel_format)=(\S+)', line)
        codec_kind = codec_match.group(1) if codec_match else None
        codec_value = codec_match.group(2) if codec_match else None

        if codec_kind == "vcodec":
            vcodecs.add(codec_value)
        elif codec_kind == "pixel_format":
            pixel_formats.add(codec_value)

        # Determine whether this line describes a passthrough-capable MJPEG pin.
        # Old ffmpeg builds use "pixel_format=mjpeg"; new builds use "vcodec=mjpeg".
        # Both mean the camera can deliver hardware-compressed MJPEG frames.
        is_mjpeg_pin = (
            (codec_kind == "vcodec" and codec_value in _PASSTHROUGH_CODECS)
            or (codec_kind == "pixel_format" and codec_value == "mjpeg")
        )

        # Parse fps and resolution from min/max lines.
        # Pattern:  min s=WxH fps=F  max s=WxH fps=F
        # fps may be integer ("30") or decimal ("30.00", "120.101")
        for match in re.finditer(r's=(\d+)x(\d+)\s+fps=(\d+(?:\.\d+)?)', line):
            w, h = int(match.group(1)), int(match.group(2))
            fps = int(float(match.group(3)))
            all_resolutions.add((w, h))
            all_framerates.add(fps)
            # Store per-codec: MJPEG pins support higher FPS than raw pins
            if is_mjpeg_pin:
                mjpeg_res_fps.setdefault((w, h), set()).add(fps)
            else:
                raw_res_fps.setdefault((w, h), set()).add(fps)

    # Prefer MJPEG (compressed) capabilities; fall back to raw pixel formats.
    # MJPEG pins always offer higher FPS at any given resolution.
    if mjpeg_res_fps:
        res_fps_map = {res: sorted(fps, reverse=True) for res, fps in mjpeg_res_fps.items()}
    else:
        res_fps_map = {res: sorted(fps, reverse=True) for res, fps in raw_res_fps.items()}

    # Prefer compressed vcodecs (passthrough-capable), then raw pixel formats
    native_codec = ""
    if vcodecs:
        for c in sorted(vcodecs):
            if c in _PASSTHROUGH_CODECS:
                native_codec = c
                break
        if not native_codec:
            native_codec = sorted(vcodecs)[0]
    elif pixel_formats:
        native_codec = sorted(pixel_formats)[0]

    res_list = sorted(all_resolutions, key=lambda r: (-r[0], -r[1]))  # highest first
    fps_list = sorted(all_framerates, reverse=True)
    logger.info("dshow caps for %s: res=%s fps=%s native=%s",
                platform_id, res_list, fps_list, native_codec)
    for (rw, rh), rfps in res_fps_map.items():
        logger.debug("  %dx%d → %s fps", rw, rh, rfps)
    return res_list, fps_list, native_codec, res_fps_map


def _query_camera_caps(camera: CameraInfo, platform_name: str) -> None:
    """Probe which resolutions/framerates the camera supports.

    On Windows, uses the fast ``-list_options`` path which also captures
    the camera's native output codec (e.g. ``mjpeg``) for passthrough recording
    and per-resolution FPS limits so the UI can offer valid combinations only.
    On macOS/Linux, falls back to probe-each-combo (slower but works).
    """
    if platform_name == "Windows":
        res, fps, native, res_fps = _query_caps_windows(
            camera.platform_id, camera.device_number,
        )
        if res:
            camera.supported_resolutions = res
            camera.supported_framerates = fps
            camera.native_codec = native
            camera.resolution_fps = res_fps
            return
        # Fall through to probing if list_options gave nothing
        logger.warning("_query_caps_windows empty, falling back to probing")

    # Slow path: probe each common resolution/fps combo
    supported_res = []
    for w, h in COMMON_RESOLUTIONS:
        if _probe_capability(
            camera.platform_id, w, h, 30, platform_name, camera.device_number,
        ):
            supported_res.append((w, h))
    if not supported_res:
        supported_res = [(640, 480)]

    supported_fps = []
    test_res = supported_res[0]
    for fps in COMMON_FRAMERATES:
        if _probe_capability(
            camera.platform_id, test_res[0], test_res[1],
            fps, platform_name, camera.device_number,
        ):
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
