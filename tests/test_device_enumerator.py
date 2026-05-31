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
