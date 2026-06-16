from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from micecam.single_frame_recorder import SingleFrameRecorder


def test_single_frame_mode_rejects_non_windows(tmp_path: Path) -> None:
    recorder = SingleFrameRecorder(camera_id="video=Test", output_dir=tmp_path)

    with mock.patch("micecam.single_frame_recorder.sys.platform", "darwin"):
        with pytest.raises(RuntimeError, match="Windows-only"):
            recorder.start()


def test_single_frame_mode_reports_missing_helper(tmp_path: Path) -> None:
    recorder = SingleFrameRecorder(camera_id="video=Test", output_dir=tmp_path)

    with (
        mock.patch("micecam.single_frame_recorder.sys.platform", "win32"),
        mock.patch.object(recorder, "_resolve_helper_path", return_value=None),
    ):
        with pytest.raises(RuntimeError, match="helper not found"):
            recorder.start()


def test_build_helper_command_contains_capture_settings(tmp_path: Path) -> None:
    recorder = SingleFrameRecorder(
        camera_id="video=Twin Camera",
        camera_name="Twin Camera",
        output_dir=tmp_path,
        camera_device_number=1,
        pixel_format="mjpg",
        ring_buffer_capacity=128,
    )
    recorder._session_dir = tmp_path / "session"

    cmd = recorder._build_helper_command(
        helper_path=Path("mf_single_frame_helper.exe"),
        resolution=(1280, 720),
        fps=60,
        wall_start=123.5,
        steady_start=987654321,
    )

    assert cmd[:2] == ["mf_single_frame_helper.exe", "--camera-id"]
    assert "video=Twin Camera" in cmd
    assert ["--camera-name", "Twin Camera"] == cmd[cmd.index("--camera-name"):cmd.index("--camera-name") + 2]
    assert ["--device-number", "1"] == cmd[cmd.index("--device-number"):cmd.index("--device-number") + 2]
    assert ["--width", "1280"] == cmd[cmd.index("--width"):cmd.index("--width") + 2]
    assert ["--height", "720"] == cmd[cmd.index("--height"):cmd.index("--height") + 2]
    assert ["--fps", "60"] == cmd[cmd.index("--fps"):cmd.index("--fps") + 2]
    assert ["--pixel-format", "mjpg"] == cmd[cmd.index("--pixel-format"):cmd.index("--pixel-format") + 2]
    assert ["--ring-buffer-capacity", "128"] == cmd[
        cmd.index("--ring-buffer-capacity"):cmd.index("--ring-buffer-capacity") + 2
    ]
    assert ["--shared-steady-start-ns", "987654321"] == cmd[
        cmd.index("--shared-steady-start-ns"):cmd.index("--shared-steady-start-ns") + 2
    ]


def test_helper_json_events_update_recorder_state(tmp_path: Path) -> None:
    recorder = SingleFrameRecorder(camera_id="video=Test", output_dir=tmp_path)

    recorder._handle_helper_event({"event": "ready", "qpc_frequency": 10_000_000})
    recorder._handle_helper_event({"event": "frame_count", "frame_count": 42})
    recorder._handle_helper_event({"event": "dropped", "drop_count": 3})
    recorder._handle_helper_event({"event": "warning", "message": "slow writer"})
    recorder._handle_helper_event({"event": "error", "message": "camera disconnected"})
    recorder._handle_helper_event({"event": "stopped", "stop_reason": "user"})

    assert recorder.is_ready is True
    assert recorder.frame_count == 42
    assert recorder.drop_count == 3
    assert recorder.warnings == ["slow writer"]
    assert recorder.last_error == "camera disconnected"
    assert recorder.stop_reason == "user"
    assert recorder.qpc_frequency == 10_000_000


def test_metadata_contains_single_frame_contract(tmp_path: Path) -> None:
    recorder = SingleFrameRecorder(
        camera_id="video=Test",
        camera_name="Test Camera",
        output_dir=tmp_path,
        camera_device_number=0,
        pixel_format="mjpg",
        ring_buffer_capacity=64,
    )
    recorder._session_dir = tmp_path / "Test_Camera" / "20260616_120000"
    recorder._frames_dir = recorder._session_dir / "frames"
    recorder._frame_log_path = recorder._session_dir / "frame_log.csv"
    recorder._metadata_path = recorder._session_dir / "metadata.json"
    recorder._helper_path = Path("mf_single_frame_helper.exe")
    recorder._requested_resolution = (640, 480)
    recorder._requested_fps = 30
    recorder._wall_start = 10.5
    recorder._steady_start = 111
    recorder.qpc_frequency = 10_000_000
    recorder.frame_count = 9
    recorder.drop_count = 2
    recorder.stop_reason = "user"
    recorder._helper_exit_code = 0
    recorder._write_metadata()

    metadata = json.loads(recorder._metadata_path.read_text(encoding="utf-8"))
    assert metadata["backend"] == "single_frame_mode"
    assert metadata["timestamp_semantics"]["alignment_timestamp"] == "arrival_qpc_ns"
    assert metadata["capture"]["ring_buffer_capacity"] == 64
    assert metadata["diagnostics"]["drop_count"] == 2
    assert metadata["files"]["frame_log"].endswith("frame_log.csv")
