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
    _list_devices_darwin,
    _list_devices_windows,
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
        assert cameras[0].platform_id == 'video="Logitech HD Webcam C920"'
        assert cameras[1].name == "Integrated Camera"

    def test_no_cameras(self) -> None:
        with mock.patch(
            "micecam.camera_manager._run_ffmpeg",
            return_value="",
        ):
            cameras = _list_devices_windows()
        assert cameras == []

    def test_parse_v8_flat_format(self) -> None:
        """ffmpeg 8+ output: flat list, no section headers."""
        with mock.patch(
            "micecam.camera_manager._run_ffmpeg",
            return_value=SAMPLE_DSHOW_OUTPUT_V8,
        ):
            cameras = _list_devices_windows()

        assert len(cameras) == 1
        assert cameras[0].name == "Integrated Camera"
        assert cameras[0].platform_id == 'video="Integrated Camera"'

    def test_v8_skips_alternative_names(self) -> None:
        """Alternative name lines must not create duplicate entries."""
        # Single camera with alternative name
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
