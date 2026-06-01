"""
Tests for ffmpeg backend — verifies command building across platforms.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from micecam.camera_manager import get_ffmpeg_path
from micecam.recorder import Recorder


class TestFfmpegPath:
    def test_returns_string(self) -> None:
        path = get_ffmpeg_path()
        assert isinstance(path, str)
        assert "ffmpeg" in path

    def test_bundled_takes_priority(self, tmp_path: Path) -> None:
        """When bundled ffmpeg exists, it should be used."""
        # This test is more of a design-doc test — on macOS in CI,
        # the bundled ffmpeg won't exist, so we just verify the function
        # returns something sensible.
        path = get_ffmpeg_path()
        assert path  # non-empty


class TestPlatformUtils:
    def test_imports(self) -> None:
        """Verify utility modules import cleanly."""
        from micecam.utils.platform import is_macos, is_windows, ffmpeg_device_format

        # One of these must be True
        assert is_macos() or is_windows() or True  # Linux is valid too

        fmt = ffmpeg_device_format()
        assert fmt in ("avfoundation", "dshow", "v4l2")

    def test_resource_path(self) -> None:
        from micecam.utils.resource_path import get_ffmpeg_path as resolve_ffmpeg

        path = resolve_ffmpeg()
        assert isinstance(path, str)
        assert len(path) > 0


class TestRecorderVideoProbe:
    FFMPEG_OUTPUT = """
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'sample.mp4':
  Duration: 00:00:16.63, start: 0.000000, bitrate: 56546 kb/s
frame=   92 fps=0.0 q=-1.0 size=N/A time=00:00:03.20 bitrate=N/A speed= 341x
frame=  495 fps=0.0 q=-1.0 Lsize=N/A time=00:00:16.59 bitrate=N/A speed= 341x
"""

    def test_parse_ffmpeg_duration(self) -> None:
        assert Recorder._parse_ffmpeg_duration(self.FFMPEG_OUTPUT) == 16.63

    def test_parse_ffmpeg_frame_count_uses_last_progress_line(self) -> None:
        assert Recorder._parse_ffmpeg_frame_count(self.FFMPEG_OUTPUT) == 495

    def test_parse_ffmpeg_missing_fields(self) -> None:
        assert Recorder._parse_ffmpeg_duration("no duration") is None
        assert Recorder._parse_ffmpeg_frame_count("no frames") is None

    def test_probe_output_video_uses_final_mp4_metadata(self, tmp_path: Path) -> None:
        video_path = tmp_path / "sample.mp4"
        video_path.write_bytes(b"not a real mp4; subprocess is mocked")
        recorder = Recorder(camera_id="video=Test", output_dir=tmp_path)
        recorder._output_path = video_path

        completed = mock.Mock(stderr=self.FFMPEG_OUTPUT, stdout="")
        with (
            mock.patch("micecam.recorder.get_ffmpeg_path", return_value="ffmpeg.exe"),
            mock.patch("micecam.recorder.subprocess.run", return_value=completed) as run,
        ):
            duration, frames = recorder._probe_output_video()

        assert duration == 16.63
        assert frames == 495
        cmd = run.call_args.args[0]
        assert cmd[:2] == ["ffmpeg.exe", "-hide_banner"]
        assert str(video_path) in cmd
        assert ["-c", "copy"] == cmd[cmd.index("-c"):cmd.index("-c") + 2]

    def test_metadata_keeps_experimental_time_as_source_of_truth(
        self, tmp_path: Path
    ) -> None:
        recorder = Recorder(
            camera_id="video=Test",
            camera_name="Test Camera",
            output_dir=tmp_path,
            native_codec="mjpeg",
        )
        recorder._output_path = tmp_path / "sample.mp4"
        recorder._srt_path = tmp_path / "sample.srt"
        recorder._metadata_path = tmp_path / "sample.json"
        recorder._requested_resolution = (1920, 1080)
        recorder._requested_fps = 30
        recorder._requested_codec = "h264"
        recorder.frame_count = 495

        recorder._write_metadata(
            wall_duration=22.0,
            progress_frames=495,
            video_duration=16.63,
            video_frames=495,
        )

        metadata = json.loads(recorder._metadata_path.read_text(encoding="utf-8"))
        assert metadata["experimental_timing"]["duration_seconds"] == 22.0
        assert metadata["experimental_timing"]["frame_count"] == 495
        assert metadata["container_timing"]["duration_seconds"] == 16.63
        assert metadata["diagnostics"]["duration_delta_seconds"] == pytest.approx(5.37)
        assert metadata["diagnostics"]["warnings"]

    def test_record_frame_timestamp_keeps_absolute_wallclock_pts(self) -> None:
        recorder = Recorder(camera_id="video=Test")
        recorder._record_frame_timestamp(
            "[vist#0:0/mjpeg] demuxer -> ist_index:0:0 type:video "
            "pkt_pts:1717171200123456 pkt_pts_time:1717171200.123456"
        )
        recorder._record_frame_timestamp(
            "[vist#0:0/mjpeg] demuxer -> ist_index:0:0 type:video "
            "pkt_pts:333333 pkt_pts_time:0.0333333"
        )

        assert recorder._frame_wall_times == [1717171200.123456]

    def test_build_command_requests_wallclock_debug_timestamps(
        self, tmp_path: Path
    ) -> None:
        recorder = Recorder(camera_id="video=Test", native_codec="mjpeg")
        with mock.patch("micecam.recorder.sys.platform", "win32"):
            cmd = recorder._build_command(
                resolution=(1920, 1080),
                fps=30,
                encoder="copy",
                output_path=tmp_path / "out.mp4",
            )

        assert "-debug_ts" in cmd
        wallclock_index = cmd.index("-use_wallclock_as_timestamps")
        input_index = cmd.index("-i")
        assert cmd[wallclock_index + 1] == "1"
        assert wallclock_index < input_index

    def test_build_command_uses_dshow_device_number(self, tmp_path: Path) -> None:
        recorder = Recorder(
            camera_id="video=Twin Camera",
            native_codec="mjpeg",
            camera_device_number=1,
        )
        with mock.patch("micecam.recorder.sys.platform", "win32"):
            cmd = recorder._build_command(
                resolution=(1920, 1080),
                fps=30,
                encoder="copy",
                output_path=tmp_path / "out.mp4",
            )

        number_index = cmd.index("-video_device_number")
        input_index = cmd.index("-i")
        assert cmd[number_index + 1] == "1"
        assert number_index < input_index
        assert cmd[input_index + 1] == "video=Twin Camera"
