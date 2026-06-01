"""
Tests for device enumerator — verifies ffmpeg output parsing and
platform-specific device listing.
"""

from __future__ import annotations

from unittest import mock

import pytest

from micecam.camera_manager import (
    CameraInfo,
    EncoderInfo,
    _dshow_alt_from_instance_id,
    _dshow_alt_from_registry_child,
    _list_devices_darwin,
    _list_devices_windows,
    _query_caps_windows,
    _run_ffmpeg,
    get_preferred_encoder,
)


# ── Sample ffmpeg outputs ───────────────────────────────────────────────

SAMPLE_AVFOUNDATION_OUTPUT = """
[AVFoundation indev @ 0x7f8b9a80a000] AVFoundation video devices:
[AVFoundation indev @ 0x7f8b9a80a000] [0] MacBook Pro Camera
[AVFoundation indev @ 0x7f8b9a80a000] [1] External USB Camera
[AVFoundation indev @ 0x7f8b9a80a000] AVFoundation audio devices:
[AVFoundation indev @ 0x7f8b9a80a000] [0] MacBook Pro Microphone
[AVFoundation indev @ 0x7f8b9a80a000] [1] External USB Microphone
"""

SAMPLE_DSHOW_OUTPUT = """
[dshow @ 0000021b8a9e2c00] DirectShow video devices (some may be both video and audio devices)
[dshow @ 0000021b8a9e2c00]  "Logitech HD Webcam C920" (video)
[dshow @ 0000021b8a9e2c00]  "Integrated Camera" (video)
[dshow @ 0000021b8a9e2c00] DirectShow audio devices
[dshow @ 0000021b8a9e2c00]  "Microphone (Realtek Audio)" (audio)
"""

# New ffmpeg 8+ output format — flat list, no section headers
SAMPLE_DSHOW_OUTPUT_V8 = """
[in#0 @ 0000023be5915d00] "Integrated Camera" (video)
[in#0 @ 0000023be5915d00]   Alternative name "@device_pnp_\\\\?\\usb#vid_5986&pid_115f&mi_00#7&18f24fa2&1&0000#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\\global"
[in#0 @ 0000023be5915d00] "Headset Microphone (Oculus Virtual Audio Device)" (audio)
[in#0 @ 0000023be5915d00]   Alternative name "@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\\wave_{82E191EC-03B1-48CF-8A24-763FCBAEDF88}"
[in#0 @ 0000023be5915d00] "麦克风阵列 (Realtek(R) Audio)" (audio)
Error opening input file dummy.
"""

SAMPLE_ENCODERS_OUTPUT = """
Encoders:
 V..... libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part10 (codec h264)
 V..... libx265              libx265 H.265 / HEVC (codec hevc)
 V..... h264_videotoolbox    VideoToolbox H.264 Encoder (codec h264)
 V..... hevc_videotoolbox    VideoToolbox H.265 Encoder (codec hevc)
"""

# Sample -list_options true output with integer fps values
SAMPLE_LIST_OPTIONS_OUTPUT = """
[dshow @ 0000021b8a9e2c00] DirectShow video device options (from video devices)
[dshow @ 0000021b8a9e2c00]  Pin "Capture" (alternative pin name "0")
[dshow @ 0000021b8a9e2c00]   pixel_format=mjpeg  min s=320x240 fps=15 max s=1920x1080 fps=30
[dshow @ 0000021b8a9e2c00]   pixel_format=yuyv422 min s=640x480 fps=30 max s=640x480 fps=30
"""

# Sample with decimal fps values (some ffmpeg builds)
SAMPLE_LIST_OPTIONS_DECIMAL_FPS = """
[dshow @ 0000021b8a9e2c00] DirectShow video device options (from video devices)
[dshow @ 0000021b8a9e2c00]  Pin "Capture" (alternative pin name "0")
[dshow @ 0000021b8a9e2c00]   pixel_format=mjpeg  min s=320x240 fps=15.00 max s=1920x1080 fps=30.00
[dshow @ 0000021b8a9e2c00]   pixel_format=yuyv422 min s=160x120 fps=10.00 max s=640x480 fps=30.00
"""

# Single-resolution mode line (no separate min/max)
SAMPLE_LIST_OPTIONS_SINGLE = """
[dshow @ 0000021b8a9e2c00] DirectShow video device options (from video devices)
[dshow @ 0000021b8a9e2c00]  Pin "Capture" (alternative pin name "0")
[dshow @ 0000021b8a9e2c00]   pixel_format=yuyv422  s=640x480 fps=30
"""

# Real-world output with vcodec=mjpeg (hardware-compressed, passthrough-capable)
SAMPLE_LIST_OPTIONS_VCODEC = """
[dshow @ 0000021b8a9e2c00] DirectShow video device options (from video devices)
[dshow @ 0000021b8a9e2c00]  Pin "Capture" (alternative pin name "0")
[dshow @ 0000021b8a9e2c00]   vcodec=mjpeg  min s=1280x720 fps=30 max s=1280x720 fps=30
[dshow @ 0000021b8a9e2c00]   vcodec=mjpeg  min s=640x480 fps=30 max s=640x480 fps=30
[dshow @ 0000021b8a9e2c00]   vcodec=mjpeg  min s=320x240 fps=30 max s=320x240 fps=30
[dshow @ 0000021b8a9e2c00]   pixel_format=yuyv422  min s=640x480 fps=30 max s=640x480 fps=30
"""

# Camera with no compressed codec, only raw pixel formats
SAMPLE_LIST_OPTIONS_RAW_ONLY = """
[dshow @ 0000021b8a9e2c00] DirectShow video device options (from video devices)
[dshow @ 0000021b8a9e2c00]  Pin "Capture" (alternative pin name "0")
[dshow @ 0000021b8a9e2c00]   pixel_format=yuyv422  min s=640x480 fps=30 max s=640x480 fps=30
[dshow @ 0000021b8a9e2c00]   pixel_format=nv12  min s=320x240 fps=30 max s=320x240 fps=30
"""


class TestDarwinDeviceListing:
    def test_parse_avfoundation_devices(self) -> None:
        with mock.patch(
            "micecam.camera_manager._run_ffmpeg",
            return_value=SAMPLE_AVFOUNDATION_OUTPUT,
        ):
            cameras = _list_devices_darwin()

        assert len(cameras) == 2
        assert cameras[0].name == "MacBook Pro Camera"
        assert cameras[0].index == 0
        assert cameras[0].platform_id == "0"
        assert cameras[1].name == "External USB Camera"
        assert cameras[1].index == 1

    def test_no_cameras(self) -> None:
        with mock.patch(
            "micecam.camera_manager._run_ffmpeg",
            return_value="No devices found",
        ):
            cameras = _list_devices_darwin()
        assert cameras == []


class TestWindowsDeviceListing:
    def test_parse_dshow_devices(self) -> None:
        with mock.patch(
            "micecam.camera_manager._run_ffmpeg",
            return_value=SAMPLE_DSHOW_OUTPUT,
        ):
            cameras = _list_devices_windows()

        assert len(cameras) == 2
        assert cameras[0].name == "Logitech HD Webcam C920"
        assert cameras[0].platform_id == 'video=Logitech HD Webcam C920'
        assert cameras[1].name == "Integrated Camera"

    def test_no_cameras(self) -> None:
        with (
            mock.patch("micecam.camera_manager._run_ffmpeg", return_value=""),
            mock.patch("micecam.camera_manager._list_devices_windows_pnp", return_value=[]),
        ):
            cameras = _list_devices_windows()
        assert cameras == []

    def test_parse_v8_flat_format(self) -> None:
        """ffmpeg 8+ output: uses unique Alternative name as platform_id."""
        with mock.patch(
            "micecam.camera_manager._run_ffmpeg",
            return_value=SAMPLE_DSHOW_OUTPUT_V8,
        ):
            cameras = _list_devices_windows()

        assert len(cameras) == 1
        assert cameras[0].name == "Integrated Camera"
        # Alternative name is used as the unique platform_id
        assert cameras[0].platform_id == (
            'video=@device_pnp_\\\\?\\usb#vid_5986&pid_115f&mi_00'
            '#7&18f24fa2&1&0000#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\\global'
        )

    def test_v8_uses_alternative_name_as_platform_id(self) -> None:
        """Alternative name → unique platform_id; still one camera entry."""
        output = """
[in#0 @ 0000023be5915d00] "My Webcam" (video)
[in#0 @ 0000023be5915d00]   Alternative name "@device_pnp_\\\\?\\usb#..."
"""
        with mock.patch(
            "micecam.camera_manager._run_ffmpeg",
            return_value=output,
        ):
            cameras = _list_devices_windows()
        assert len(cameras) == 1
        assert cameras[0].name == "My Webcam"
        assert cameras[0].platform_id == 'video=@device_pnp_\\\\?\\usb#...'

    def test_v8_skips_audio_devices(self) -> None:
        """Audio devices with (audio) suffix must not appear."""
        output = """
[in#0 @ 0000023be5915d00] "Integrated Camera" (video)
[in#0 @ 0000023be5915d00] "Microphone" (audio)
"""
        with mock.patch(
            "micecam.camera_manager._run_ffmpeg",
            return_value=output,
        ):
            cameras = _list_devices_windows()
        assert len(cameras) == 1
        assert cameras[0].name == "Integrated Camera"

    def test_duplicate_names_without_alternative_use_device_numbers(self) -> None:
        """Old dshow output has no hardware path; use device_number fallback."""
        output = """
[dshow @ 0000021b8a9e2c00] DirectShow video devices
[dshow @ 0000021b8a9e2c00]  "Twin Camera" (video)
[dshow @ 0000021b8a9e2c00]  "Twin Camera" (video)
[dshow @ 0000021b8a9e2c00] DirectShow audio devices
"""
        with (
            mock.patch("micecam.camera_manager._run_ffmpeg", return_value=output),
            mock.patch("micecam.camera_manager._list_devices_windows_pnp", return_value=[]),
        ):
            cameras = _list_devices_windows()

        assert [c.name for c in cameras] == ["Twin Camera #1", "Twin Camera #2"]
        assert [c.platform_id for c in cameras] == [
            "video=Twin Camera",
            "video=Twin Camera",
        ]
        assert [c.device_number for c in cameras] == [0, 1]

    def test_duplicate_names_with_alternative_keep_unique_platform_ids(self) -> None:
        """When hardware paths exist, no device number is needed."""
        output = """
[in#0 @ 0000023be5915d00] "Twin Camera" (video)
[in#0 @ 0000023be5915d00]   Alternative name "@device_pnp_\\\\?\\usb#one"
[in#0 @ 0000023be5915d00] "Twin Camera" (video)
[in#0 @ 0000023be5915d00]   Alternative name "@device_pnp_\\\\?\\usb#two"
"""
        with (
            mock.patch("micecam.camera_manager._list_devices_windows_pnp", return_value=[]),
            mock.patch(
                "micecam.camera_manager._run_ffmpeg",
                return_value=output,
            ),
        ):
            cameras = _list_devices_windows()

        assert [c.name for c in cameras] == ["Twin Camera #1", "Twin Camera #2"]
        assert [c.platform_id for c in cameras] == [
            "video=@device_pnp_\\\\?\\usb#one",
            "video=@device_pnp_\\\\?\\usb#two",
        ]
        assert [c.device_number for c in cameras] == [None, None]

    def test_duplicate_names_use_pnp_stable_ids_when_available(self) -> None:
        """Duplicate friendly names should prefer stable PnP monikers over ordinals."""
        output = """
[dshow @ 0000021b8a9e2c00] DirectShow video devices
[dshow @ 0000021b8a9e2c00]  "Twin Camera" (video)
[dshow @ 0000021b8a9e2c00]  "Twin Camera" (video)
[dshow @ 0000021b8a9e2c00] DirectShow audio devices
"""
        pnp = [
            CameraInfo(0, "Twin Camera", "video=@device_pnp_\\\\?\\usb#one"),
            CameraInfo(1, "Twin Camera", "video=@device_pnp_\\\\?\\usb#two"),
        ]
        with (
            mock.patch("micecam.camera_manager._run_ffmpeg", return_value=output),
            mock.patch("micecam.camera_manager._list_devices_windows_pnp", return_value=pnp),
        ):
            cameras = _list_devices_windows()

        assert [c.name for c in cameras] == ["Twin Camera #1", "Twin Camera #2"]
        assert [c.platform_id for c in cameras] == [
            "video=@device_pnp_\\\\?\\usb#one",
            "video=@device_pnp_\\\\?\\usb#two",
        ]
        assert [c.device_number for c in cameras] == [None, None]

    def test_falls_back_to_pnp_when_ffmpeg_outputs_only_errors(self) -> None:
        """Non-empty ffmpeg error output must not block PnP fallback."""
        pnp = [
            CameraInfo(0, "HD USB Camera", "video=@device_pnp_\\\\?\\usb#stable")
        ]
        with (
            mock.patch(
                "micecam.camera_manager._run_ffmpeg",
                return_value="Could not enumerate video devices\nError opening input file dummy.",
            ),
            mock.patch("micecam.camera_manager._list_devices_windows_pnp", return_value=pnp),
        ):
            cameras = _list_devices_windows()

        assert cameras == pnp

    def test_construct_dshow_alt_from_pnp_instance_id(self) -> None:
        alt = _dshow_alt_from_instance_id(
            r"USB\VID_05A3&PID_9230&MI_00\6&1A643E73&0&0000"
        )

        assert alt == (
            r"@device_pnp_\\?\usb#vid_05a3&pid_9230&mi_00"
            r"#6&1a643e73&0&0000#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
        )

    def test_construct_dshow_alt_from_registry_child(self) -> None:
        alt = _dshow_alt_from_registry_child(
            r"##?#USB#VID_05A3&PID_9230&MI_00#6&1a643e73&0&0000"
            r"#{65e8773d-8f56-11d0-a3b9-00a0c9223196}"
        )

        assert alt == (
            r"@device_pnp_\\?\usb#vid_05a3&pid_9230&mi_00"
            r"#6&1a643e73&0&0000#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
        )


class TestCameraInfo:
    def test_default_values(self) -> None:
        cam = CameraInfo(index=0, name="Test", platform_id="0")
        assert cam.supported_resolutions == []
        assert cam.supported_framerates == []


class TestEncoderPreference:
    def test_fallback_to_libx264(self) -> None:
        """When no hardware encoders available, fall back to libx264."""
        with mock.patch(
            "micecam.camera_manager.get_available_encoders",
            return_value=[
                EncoderInfo(name="libx264", hardware_accelerated=False, codec="h264"),
            ],
        ):
            encoder = get_preferred_encoder("h264")
            assert encoder == "libx264"

    def test_prefer_hardware_on_darwin(self) -> None:
        with (
            mock.patch("micecam.camera_manager.platform.system", return_value="Darwin"),
            mock.patch(
                "micecam.camera_manager.get_available_encoders",
                return_value=[
                    EncoderInfo(
                        name="h264_videotoolbox",
                        hardware_accelerated=True,
                        codec="h264",
                    ),
                    EncoderInfo(
                        name="libx264", hardware_accelerated=False, codec="h264"
                    ),
                ],
            ),
        ):
            encoder = get_preferred_encoder("h264")
            assert encoder == "h264_videotoolbox"


class TestListDevicesFallback:
    """Verify -list_devices argument-order fallback behaviour."""

    def test_generic_succeeds_no_fallback(self) -> None:
        """When generic style (ffmpeg 7+) returns data, don't try fallback."""
        with mock.patch(
            "micecam.camera_manager._run_ffmpeg",
        ) as mock_run:
            mock_run.return_value = SAMPLE_DSHOW_OUTPUT
            cameras = _list_devices_windows()

        assert len(cameras) == 2
        # Only one call — generic style succeeded
        assert mock_run.call_count == 1
        # First call uses generic option ordering
        first_call_args = mock_run.call_args_list[0][0][0]
        assert first_call_args[0] == "-list_devices"

    def test_fallback_to_format_specific(self) -> None:
        """When generic style returns empty, try format-specific style."""
        with mock.patch(
            "micecam.camera_manager._run_ffmpeg",
        ) as mock_run:
            # First call (generic) → empty, second (format-specific) → success
            mock_run.side_effect = ["", SAMPLE_DSHOW_OUTPUT]
            cameras = _list_devices_windows()

        assert len(cameras) == 2
        assert mock_run.call_count == 2
        # Second call uses format-specific ordering
        second_call_args = mock_run.call_args_list[1][0][0]
        assert second_call_args[0] == "-f"
        assert second_call_args[1] == "dshow"

    @mock.patch("micecam.camera_manager._list_devices_windows_pnp", return_value=[])
    def test_both_fail_returns_empty(self, _pnp: mock.Mock) -> None:
        """When both styles fail, return empty list."""
        with mock.patch(
            "micecam.camera_manager._run_ffmpeg",
            side_effect=["", ""],  # generic empty → format-specific empty
        ):
            cameras = _list_devices_windows()
        assert cameras == []


class TestQueryCapsWindows:
    """Verify -list_options output parsing, including native codec."""

    def test_parse_integer_fps(self) -> None:
        with mock.patch(
            "micecam.camera_manager._run_ffmpeg",
            return_value=SAMPLE_LIST_OPTIONS_OUTPUT,
        ):
            res, fps, native, res_fps = _query_caps_windows('video=Test')

        assert (1920, 1080) in res
        assert (640, 480) in res
        assert (320, 240) in res  # min resolution, captured by second regex
        assert 30 in fps
        assert 15 in fps  # min fps
        # Highest resolution first
        assert res[0] == (1920, 1080)
        # pixel_format=mjpeg is preferred (first compressed codec)
        assert native == "mjpeg"
        # Per-resolution FPS: MJPEG pin covers 320x240→1920x1080
        assert (1920, 1080) in res_fps
        assert 30 in res_fps[(1920, 1080)]
        assert (320, 240) in res_fps
        assert 15 in res_fps[(320, 240)]

    def test_parse_decimal_fps(self) -> None:
        """Decimal fps values like 30.00 must be parsed as int 30."""
        with mock.patch(
            "micecam.camera_manager._run_ffmpeg",
            return_value=SAMPLE_LIST_OPTIONS_DECIMAL_FPS,
        ):
            res, fps, native, res_fps = _query_caps_windows('video=Test')

        assert (1920, 1080) in res
        assert (640, 480) in res
        assert 30 in fps  # 30.00 → 30
        assert 15 in fps  # 15.00 → 15
        assert 10 in fps  # 10.00 → 10
        # No float values should leak through
        assert all(isinstance(f, int) for f in fps)
        assert native == "mjpeg"
        assert (1920, 1080) in res_fps

    def test_parse_single_resolution(self) -> None:
        """Devices with only one resolution (no min/max split)."""
        with mock.patch(
            "micecam.camera_manager._run_ffmpeg",
            return_value=SAMPLE_LIST_OPTIONS_SINGLE,
        ):
            res, fps, native, res_fps = _query_caps_windows('video=Test')

        assert res == [(640, 480)]
        assert fps == [30]
        assert native == "yuyv422"
        # pixel_format=yuyv422 → no MJPEG pin, falls back to raw_res_fps
        assert (640, 480) in res_fps
        assert res_fps[(640, 480)] == [30]

    def test_empty_output(self) -> None:
        with mock.patch(
            "micecam.camera_manager._run_ffmpeg",
            return_value="",
        ):
            res, fps, native, res_fps = _query_caps_windows('video=Test')
        assert res == []
        assert fps == []
        assert native == ""
        assert res_fps == {}

    def test_query_caps_passes_device_number(self) -> None:
        with mock.patch(
            "micecam.camera_manager._run_ffmpeg",
            return_value=SAMPLE_LIST_OPTIONS_SINGLE,
        ) as run:
            _query_caps_windows("video=Twin Camera", device_number=1)

        args = run.call_args.args[0]
        assert args[0:2] == ["-f", "dshow"]
        assert args[args.index("-video_device_number") + 1] == "1"
        assert args[args.index("-i") + 1] == "video=Twin Camera"

    def test_vcodec_mjpeg_parsing(self) -> None:
        """vcodec=mjpeg (hardware-compressed) → native_codec = 'mjpeg'."""
        with mock.patch(
            "micecam.camera_manager._run_ffmpeg",
            return_value=SAMPLE_LIST_OPTIONS_VCODEC,
        ):
            res, fps, native, res_fps = _query_caps_windows('video=Test')

        assert (1280, 720) in res
        assert (640, 480) in res
        assert (320, 240) in res
        assert fps == [30]
        # vcodec=mjpeg takes priority over pixel_format=yuyv422
        assert native == "mjpeg"
        # Per-resolution map from vcodec=mjpeg pins
        assert (1280, 720) in res_fps
        assert res_fps[(1280, 720)] == [30]
        assert (640, 480) in res_fps
        assert (320, 240) in res_fps

    def test_raw_only_no_passthrough(self) -> None:
        """Camera with only raw pixel formats — no passthrough codec."""
        with mock.patch(
            "micecam.camera_manager._run_ffmpeg",
            return_value=SAMPLE_LIST_OPTIONS_RAW_ONLY,
        ):
            res, fps, native, res_fps = _query_caps_windows('video=Test')

        assert (640, 480) in res
        assert (320, 240) in res
        assert native == "nv12"  # alphabetically first raw format
        # No MJPEG pin → falls back to raw pixel_format map
        assert (640, 480) in res_fps
        assert (320, 240) in res_fps


class TestRunFfmpegEncoding:
    """Verify _run_ffmpeg passes UTF-8 encoding to subprocess."""

    def test_uses_utf8_encoding(self) -> None:
        """subprocess.run must be called with encoding='utf-8'."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value.stderr = "test output"
            mock_run.return_value.stdout = ""
            _run_ffmpeg(["-version"], timeout=5)

        _, kwargs = mock_run.call_args
        assert kwargs.get("encoding") == "utf-8"
        assert kwargs.get("errors") == "replace"
        assert kwargs.get("text") is True
