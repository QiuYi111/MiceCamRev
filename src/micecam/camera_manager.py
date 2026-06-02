"""
Camera enumeration and capability querying.

Cross-platform: macOS (AVFoundation), Windows (dshow), Linux (v4l2).
Falls back to probing common resolution/framerate combos when the platform
doesn't provide direct enumeration.
"""

from __future__ import annotations

import json
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
    # Per-mode input codecs: {(w,h,fps): ["h264", "mjpeg", ...]}.
    # DirectShow cameras often expose different codecs for 30 fps vs 60 fps.
    mode_codecs: dict[tuple[int, int, int], list[str]] = field(default_factory=dict)


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
    friendly name (e.g. two identical USB cameras), prefer a stable
    ``@device_pnp_...`` DirectShow moniker over the fragile friendly-name
    ordinal, but only when that moniker comes from ffmpeg's own
    "Alternative name" output or from DirectShow COM enumeration. PnP records
    are used only as a friendly-name fallback; they are not trusted as dshow
    input IDs.
    """
    cameras: list[CameraInfo] = []
    for args in (
        ["-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        ["-f", "dshow", "-list_devices", "true", "-i", "dummy"],
    ):
        candidate = _run_ffmpeg(args)
        logger.debug("dshow device listing output:\n%s", candidate)
        cameras = _parse_dshow_device_list(candidate)
        if cameras:
            break

    if not cameras:
        com_cameras = _list_devices_windows_com()
        if com_cameras:
            _validate_or_fallback_stable_ids(com_cameras, "COM")
            _suffix_duplicate_camera_names(com_cameras)
            logger.info("Found %d dshow camera(s) via COM", len(com_cameras))
            for c in com_cameras:
                logger.info("  [%d] %s -> %s", c.index, c.name, c.platform_id)
            return com_cameras

        pnp_cameras = _list_devices_windows_pnp()
        if pnp_cameras:
            _force_friendly_dshow_ids(pnp_cameras)
            _suffix_duplicate_camera_names(pnp_cameras)
            logger.info("Found %d dshow camera(s) via PnP friendly-name fallback", len(pnp_cameras))
            for c in pnp_cameras:
                logger.info("  [%d] %s -> %s", c.index, c.name, c.platform_id)
            return pnp_cameras
        logger.warning("No Windows camera devices found")
        return []

    _validate_or_fallback_stable_ids(cameras, "ffmpeg Alternative name")

    if _needs_pnp_stable_ids(cameras):
        stable_cameras = _list_devices_windows_com()
        cameras = _apply_stable_dshow_ids(cameras, stable_cameras)

    # Keep UI/output labels unique without changing the DirectShow ID.
    _suffix_duplicate_camera_names(cameras)

    logger.info("Found %d dshow camera(s)", len(cameras))
    for c in cameras:
        logger.info("  [%d] %s -> %s", c.index, c.name, c.platform_id)
    return cameras


def _parse_dshow_device_list(output: str) -> list[CameraInfo]:
    """Parse ffmpeg dshow -list_devices output without side effects."""
    cameras: list[CameraInfo] = []
    has_section_headers = "DirectShow video devices" in output
    in_video_section = not has_section_headers
    pending_camera: CameraInfo | None = None

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if "DirectShow video devices" in line:
            in_video_section = True
            continue
        if "DirectShow audio devices" in line:
            break
        if not in_video_section:
            continue

        dev_match = re.search(r'"(.+?)"\s*\((\w+)\)', line)
        if dev_match:
            dev_name = dev_match.group(1)
            dev_kind = dev_match.group(2)
            if dev_kind == "video":
                idx = len(cameras)
                escaped = dev_name.replace("\\", "\\\\").replace(":", "\\:")
                pending_camera = CameraInfo(
                    index=idx,
                    name=dev_name,
                    platform_id=f"video={escaped}",
                )
                cameras.append(pending_camera)
            else:
                pending_camera = None
            continue

        if "Alternative name" in line and pending_camera is not None:
            alt_match = re.search(r'"(@[^"]+)"', line)
            if alt_match:
                pending_camera.platform_id = f"video={alt_match.group(1)}"
            continue

    return cameras


def _suffix_duplicate_camera_names(cameras: list[CameraInfo]) -> None:
    """Append #1/#2 to duplicate display names and set ordinal fallback."""
    name_counts: dict[str, int] = {}
    for cam in cameras:
        name_counts[cam.name] = name_counts.get(cam.name, 0) + 1

    dup_names = {n for n, c in name_counts.items() if c > 1}
    if not dup_names:
        return

    name_seen: dict[str, int] = {}
    for cam in cameras:
        if cam.name not in dup_names:
            continue
        name_seen[cam.name] = name_seen.get(cam.name, 0) + 1
        suffix = f" #{name_seen[cam.name]}"
        escaped = cam.name.replace("\\", "\\\\").replace(":", "\\:")
        if cam.platform_id == f"video={escaped}":
            cam.device_number = name_seen[cam.name] - 1
            logger.warning(
                "Duplicate camera '%s' disambiguated with -video_device_number %d. "
                "This is an ordinal fallback; prefer PnP/Alternative-name IDs.",
                cam.name,
                cam.device_number,
            )
        cam.name += suffix


def _apply_stable_dshow_ids(
    cameras: list[CameraInfo],
    stable_cameras: list[CameraInfo],
) -> list[CameraInfo]:
    """Replace friendly-name duplicate IDs with stable DirectShow monikers."""
    if not cameras or not stable_cameras:
        return cameras

    stable_by_name: dict[str, list[CameraInfo]] = {}
    for cam in stable_cameras:
        stable_by_name.setdefault(cam.name, []).append(cam)

    occurrence: dict[str, int] = {}
    for cam in cameras:
        if _is_dshow_alternative_id(cam.platform_id):
            continue
        occurrence[cam.name] = occurrence.get(cam.name, 0) + 1
        candidates = stable_by_name.get(cam.name, [])
        candidate_index = occurrence[cam.name] - 1
        if candidate_index >= len(candidates):
            continue
        stable = candidates[candidate_index]
        if not _is_dshow_alternative_id(stable.platform_id):
            continue
        if not _dshow_device_id_usable(stable.platform_id):
            logger.warning(
                "Stable DirectShow ID for camera '%s' is not usable; keeping "
                "friendly-name ordinal fallback: %s",
                cam.name,
                stable.platform_id,
            )
            continue
        logger.info("Using stable DirectShow ID for camera '%s': %s", cam.name, stable.platform_id)
        cam.platform_id = stable.platform_id
        cam.device_number = None
    return cameras


def _validate_or_fallback_stable_ids(cameras: list[CameraInfo], source: str) -> None:
    """Keep stable monikers only when dshow can query options for them."""
    for cam in cameras:
        if not _is_dshow_alternative_id(cam.platform_id):
            continue
        if _dshow_device_id_usable(cam.platform_id):
            continue
        logger.warning(
            "%s ID for camera '%s' is not usable by dshow; falling back "
            "to friendly-name opening: %s",
            source,
            cam.name,
            cam.platform_id,
        )
        escaped = cam.name.replace("\\", "\\\\").replace(":", "\\:")
        cam.platform_id = f"video={escaped}"
        cam.device_number = None


def _force_friendly_dshow_ids(cameras: list[CameraInfo]) -> None:
    """Use only friendly-name dshow IDs for non-DirectShow enumeration fallbacks."""
    for cam in cameras:
        escaped = cam.name.replace("\\", "\\\\").replace(":", "\\:")
        cam.platform_id = f"video={escaped}"
        cam.device_number = None


def _dshow_device_id_usable(
    platform_id: str,
    device_number: int | None = None,
) -> bool:
    """Return True when DirectShow can enumerate options for a device id."""
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
        timeout=8,
    )
    lower = output.lower()
    has_options = (
        "directshow video device options" in lower
        or "vcodec=" in lower
        or "pixel_format=" in lower
        or re.search(r"\bs=\d+x\d+\s+fps=", lower) is not None
    )
    if has_options:
        return True
    if output.strip():
        logger.debug("dshow device id unusable for %s:\n%s", platform_id, output)
    return False


def _is_dshow_alternative_id(platform_id: str) -> bool:
    """Return True for ffmpeg dshow alternative-name monikers."""
    return platform_id.lower().startswith("video=@")


def _needs_pnp_stable_ids(cameras: list[CameraInfo]) -> bool:
    """Return True when parsed dshow devices still depend on friendly-name ordinals."""
    if not cameras:
        return True
    name_counts: dict[str, int] = {}
    for cam in cameras:
        name_counts[cam.name] = name_counts.get(cam.name, 0) + 1
    for cam in cameras:
        if name_counts[cam.name] <= 1:
            continue
        if not _is_dshow_alternative_id(cam.platform_id):
            return True
    return False


def _list_devices_windows_com() -> list[CameraInfo]:
    """Enumerate DirectShow video devices through COM monikers.

    FFmpeg's dshow "Alternative name" is derived from the same moniker
    display name, with ':' replaced by '_'.  Querying COM directly gives us
    the exact per-device identifier even when ffmpeg's device-list output is
    unavailable or only contains duplicate friendly names.
    """
    records = _enumerate_dshow_video_monikers_com()
    cameras: list[CameraInfo] = []
    seen: set[str] = set()
    for friendly_name, display_name in records:
        if not display_name:
            continue
        alt_name = _dshow_unique_name_from_display_name(display_name)
        platform_id = f"video={alt_name}"
        seen_key = platform_id.lower()
        if seen_key in seen:
            continue
        seen.add(seen_key)
        cameras.append(CameraInfo(
            index=len(cameras),
            name=friendly_name or display_name,
            platform_id=platform_id,
        ))
    return cameras


def _dshow_unique_name_from_display_name(display_name: str) -> str:
    """Convert an IMoniker display name to ffmpeg's dshow alternative name.

    ffmpeg's ``libavdevice/dshow.c`` replaces every ``:`` in the device
    display name with ``_`` to produce its "Alternative name".  We do the
    same here so the resulting ``@device_pnp_…`` ID matches exactly what
    ffmpeg would generate internally.
    """
    alt_name = display_name.replace(":", "_")
    logger.debug(
        "dshow display_name → alternative name:\n  raw  = %s\n  alt  = %s",
        display_name, alt_name,
    )
    return alt_name


def _enumerate_dshow_video_monikers_com() -> list[tuple[str, str]]:
    """Return ``(friendly_name, moniker_display_name)`` for dshow video devices."""
    if platform.system() != "Windows":
        return []

    try:
        import ctypes
        import uuid
        from ctypes import wintypes
    except Exception:
        logger.debug("ctypes COM support is unavailable", exc_info=True)
        return []

    HRESULT = ctypes.c_long
    ULONG = wintypes.ULONG
    DWORD = wintypes.DWORD
    WORD = wintypes.WORD
    USHORT = wintypes.USHORT
    WINFUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)

    S_OK = 0
    S_FALSE = 1
    RPC_E_CHANGED_MODE = 0x80010106
    CLSCTX_INPROC_SERVER = 0x1
    VT_EMPTY = 0
    VT_BSTR = 8

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", DWORD),
            ("Data2", WORD),
            ("Data3", WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class VARIANT_UNION(ctypes.Union):
        _fields_ = [
            ("bstrVal", ctypes.c_void_p),
            ("byref", ctypes.c_void_p),
        ]

    class VARIANT(ctypes.Structure):
        _anonymous_ = ("data",)
        _fields_ = [
            ("vt", USHORT),
            ("wReserved1", USHORT),
            ("wReserved2", USHORT),
            ("wReserved3", USHORT),
            ("data", VARIANT_UNION),
        ]

    def guid(value: str) -> GUID:
        parsed = uuid.UUID(value.strip("{}"))
        return GUID(
            parsed.time_low,
            parsed.time_mid,
            parsed.time_hi_version,
            (ctypes.c_ubyte * 8).from_buffer_copy(parsed.bytes[8:]),
        )

    def succeeded(hr: int) -> bool:
        return ctypes.c_long(hr).value >= 0

    def hresult_u32(hr: int) -> int:
        return ctypes.c_ulong(hr).value & 0xFFFFFFFF

    def com_method(ptr: ctypes.c_void_p, index: int, *argtypes: object) -> object:
        vtable = ctypes.cast(
            ptr,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ).contents
        return WINFUNCTYPE(HRESULT, ctypes.c_void_p, *argtypes)(vtable[index])

    def release(ptr: ctypes.c_void_p | None) -> None:
        if not ptr:
            return
        release_method = com_method(ptr, 2)
        release_method(ptr)

    ole32 = ctypes.OleDLL("ole32")
    oleaut32 = ctypes.OleDLL("oleaut32")

    ole32.CoInitialize.argtypes = [ctypes.c_void_p]
    ole32.CoInitialize.restype = HRESULT
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(GUID),
        ctypes.c_void_p,
        DWORD,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = HRESULT
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    oleaut32.VariantClear.argtypes = [ctypes.POINTER(VARIANT)]
    oleaut32.VariantClear.restype = HRESULT

    clsid_system_device_enum = guid("{62BE5D10-60EB-11D0-BD3B-00A0C911CE86}")
    clsid_video_input_device_category = guid("{860BB310-5D01-11D0-BD3B-00A0C911CE86}")
    iid_icreate_dev_enum = guid("{29840822-5B84-11D0-BD3B-00A0C911CE86}")
    iid_iproperty_bag = guid("{55272A00-42CB-11CE-8135-00AA004BB851}")

    com_initialized = False
    dev_enum = ctypes.c_void_p()
    enum_moniker = ctypes.c_void_p()
    records: list[tuple[str, str]] = []

    try:
        hr = ole32.CoInitialize(None)
        if hresult_u32(hr) == RPC_E_CHANGED_MODE:
            logger.debug("COM already initialized with a different apartment model")
        elif not succeeded(hr):
            logger.debug("CoInitialize failed: 0x%08x", hresult_u32(hr))
            return []
        else:
            com_initialized = True

        hr = ole32.CoCreateInstance(
            ctypes.byref(clsid_system_device_enum),
            None,
            CLSCTX_INPROC_SERVER,
            ctypes.byref(iid_icreate_dev_enum),
            ctypes.byref(dev_enum),
        )
        if not succeeded(hr) or not dev_enum:
            logger.debug("CoCreateInstance(ICreateDevEnum) failed: 0x%08x", hresult_u32(hr))
            return []

        create_class_enum = com_method(
            dev_enum,
            3,
            ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p),
            DWORD,
        )
        hr = create_class_enum(
            dev_enum,
            ctypes.byref(clsid_video_input_device_category),
            ctypes.byref(enum_moniker),
            0,
        )
        if hr == S_FALSE or not enum_moniker:
            return []
        if not succeeded(hr):
            logger.debug("CreateClassEnumerator(video) failed: 0x%08x", hresult_u32(hr))
            return []

        # No CreateBindCtx — we pass NULL to IMoniker::GetDisplayName
        # (matching ffmpeg's own dshow code), so no bind context is needed.

        enum_next = com_method(
            enum_moniker,
            3,
            ULONG,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ULONG),
        )

        while True:
            moniker = ctypes.c_void_p()
            fetched = ULONG(0)
            hr = enum_next(enum_moniker, 1, ctypes.byref(moniker), ctypes.byref(fetched))
            if hr != S_OK or not moniker:
                break
            try:
                display_name = _read_dshow_moniker_display_name(
                    moniker,
                    com_method,
                    ole32.CoTaskMemFree,
                )
                friendly_name = _read_dshow_moniker_property(
                    moniker,
                    "FriendlyName",
                    iid_iproperty_bag,
                    com_method,
                    oleaut32.VariantClear,
                    VT_EMPTY,
                    VT_BSTR,
                    VARIANT,
                )
                if display_name:
                    logger.debug(
                        "COM moniker display_name=%r friendly_name=%r",
                        display_name, friendly_name,
                    )
                    records.append((friendly_name, display_name))
            finally:
                release(moniker)
    except Exception:
        logger.debug("DirectShow COM camera enumeration failed", exc_info=True)
        return []
    finally:
        release(enum_moniker)
        release(dev_enum)
        if com_initialized:
            ole32.CoUninitialize()

    return records


def _read_dshow_moniker_display_name(
    moniker: object,
    com_method: object,
    co_task_mem_free: object,
) -> str:
    """Read an IMoniker display name and free COM-allocated memory.

    Passes NULL for *pbc* (bind context) — matching ffmpeg's own dshow code
    in ``libavdevice/dshow.c``.  Passing a non-NULL bind context can cause
    ``IMoniker::GetDisplayName`` to return a different display-name format,
    which after ``:`` → ``_`` replacement produces a ``@device_pnp_…`` ID
    that ffmpeg will reject for capture (even though ``-list_options`` may
    succeed against it).
    """
    import ctypes

    get_display_name = com_method(
        moniker,
        20,  # IMoniker::GetDisplayName
        ctypes.c_void_p,  # IBindCtx *pbc — NULL, matching ffmpeg
        ctypes.c_void_p,  # IMoniker *pmkToLeft — NULL
        ctypes.POINTER(ctypes.c_void_p),  # LPOLESTR *ppszDisplayName
    )
    name_ptr = ctypes.c_void_p()
    hr = get_display_name(moniker, None, None, ctypes.byref(name_ptr))
    if ctypes.c_long(hr).value < 0 or not name_ptr:
        return ""
    try:
        return ctypes.wstring_at(name_ptr.value)
    finally:
        co_task_mem_free(name_ptr)


def _read_dshow_moniker_property(
    moniker: object,
    property_name: str,
    iid_iproperty_bag: object,
    com_method: object,
    variant_clear: object,
    vt_empty: int,
    vt_bstr: int,
    variant_type: object,
) -> str:
    """Read a string property from an IMoniker's IPropertyBag."""
    import ctypes

    property_bag = ctypes.c_void_p()
    bind_to_storage = com_method(
        moniker,
        9,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(type(iid_iproperty_bag)),
        ctypes.POINTER(ctypes.c_void_p),
    )
    hr = bind_to_storage(
        moniker,
        None,
        None,
        ctypes.byref(iid_iproperty_bag),
        ctypes.byref(property_bag),
    )
    if ctypes.c_long(hr).value < 0 or not property_bag:
        return ""

    try:
        read_property = com_method(
            property_bag,
            3,
            ctypes.c_wchar_p,
            ctypes.POINTER(variant_type),
            ctypes.c_void_p,
        )
        value = variant_type()
        value.vt = vt_bstr
        try:
            hr = read_property(property_bag, property_name, ctypes.byref(value), None)
            if ctypes.c_long(hr).value < 0:
                return ""
            if value.vt != vt_bstr or not value.bstrVal:
                return ""
            return ctypes.wstring_at(value.bstrVal)
        finally:
            if value.vt != vt_empty:
                variant_clear(ctypes.byref(value))
    finally:
        release_property_bag = com_method(property_bag, 2)
        release_property_bag(property_bag)


def _list_devices_windows_pnp() -> list[CameraInfo]:
    """Enumerate Windows cameras from PnP records as friendly-name fallbacks."""
    records = _query_pnp_camera_records()
    if not records:
        return []

    cameras: list[CameraInfo] = []
    seen_instances: set[str] = set()
    for rec in records:
        name = rec.get("FriendlyName") or rec.get("Name") or ""
        instance_id = rec.get("InstanceId") or rec.get("PNPDeviceID") or ""
        if not name or not instance_id:
            continue
        instance_key = instance_id.strip().lower()
        if instance_key in seen_instances:
            continue
        seen_instances.add(instance_key)

        escaped = name.replace("\\", "\\\\").replace(":", "\\:")
        platform_id = f"video={escaped}"
        cameras.append(CameraInfo(
            index=len(cameras),
            name=name,
            platform_id=platform_id,
        ))
    return cameras


def _query_pnp_camera_records() -> list[dict[str, str]]:
    """Return Windows camera PnP records via PowerShell."""
    ps = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "$OutputEncoding = [Console]::OutputEncoding; "
        "$devices = @(); "
        "foreach ($class in @('Camera','Image')) { "
        "  try { $devices += Get-PnpDevice -Class $class -ErrorAction Stop } catch {} "
        "} "
        "$devices | Select-Object FriendlyName,Class,InstanceId,Status | "
        "ConvertTo-Json -Depth 3"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
    except Exception:
        logger.debug("Windows PnP camera query failed", exc_info=True)
        return []

    output = (proc.stdout or "").strip()
    if not output:
        if proc.stderr:
            logger.debug("Windows PnP camera query stderr: %s", proc.stderr.strip())
        return []

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        logger.debug("Could not parse Windows PnP camera JSON: %s", output)
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


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


_PASSTHROUGH_CODEC_PRIORITY = ("h264", "hevc", "mjpeg")

# Compressed codecs that can be stream-copied into MP4 without re-encoding.
_PASSTHROUGH_CODECS = frozenset(_PASSTHROUGH_CODEC_PRIORITY)


def choose_camera_input_codec(
    camera: CameraInfo,
    resolution: tuple[int, int],
    fps: int,
) -> str:
    """Return the DirectShow input codec that supports a specific mode."""
    codecs = camera.mode_codecs.get((resolution[0], resolution[1], fps), [])
    for codec in _PASSTHROUGH_CODEC_PRIORITY:
        if codec in codecs:
            return codec
    for codec in codecs:
        if codec in _PASSTHROUGH_CODECS:
            return codec
    return ""


def _query_caps_windows(
    platform_id: str,
    device_number: int | None = None,
) -> tuple[
    list[tuple[int, int]],
    list[int],
    str,
    dict[tuple[int, int], list[int]],
    dict[tuple[int, int, int], list[str]],
]:
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
    mode_codecs: dict[tuple[int, int, int], set[str]] = {}
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

        mode_codec = ""
        if codec_kind == "vcodec":
            mode_codec = codec_value
        elif codec_kind == "pixel_format":
            mode_codec = "mjpeg" if codec_value == "mjpeg" else codec_value

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
            if mode_codec:
                mode_codecs.setdefault((w, h, fps), set()).add(mode_codec)
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
    mode_codec_map = {
        mode: sorted(codecs)
        for mode, codecs in mode_codecs.items()
    }
    logger.info("dshow caps for %s: res=%s fps=%s native=%s",
                platform_id, res_list, fps_list, native_codec)
    for (rw, rh), rfps in res_fps_map.items():
        logger.debug("  %dx%d → %s fps", rw, rh, rfps)
    return res_list, fps_list, native_codec, res_fps_map, mode_codec_map


def _query_camera_caps(camera: CameraInfo, platform_name: str) -> None:
    """Probe which resolutions/framerates the camera supports.

    On Windows, uses the fast ``-list_options`` path which also captures
    the camera's native output codec (e.g. ``mjpeg``) for passthrough recording
    and per-resolution FPS limits so the UI can offer valid combinations only.
    On macOS/Linux, falls back to probe-each-combo (slower but works).
    """
    if platform_name == "Windows":
        res, fps, native, res_fps, mode_codecs = _query_caps_windows(
            camera.platform_id, camera.device_number,
        )
        if res:
            camera.supported_resolutions = res
            camera.supported_framerates = fps
            camera.native_codec = native
            camera.resolution_fps = res_fps
            camera.mode_codecs = mode_codecs
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
