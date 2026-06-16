from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from micecam.single_frame_recorder import SingleFrameRecorder


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, value: str) -> None:
        self.writes.append(value)

    def flush(self) -> None:
        pass


class _FakeProcess:
    def __init__(self, poll_result=None, returncode: int | None = None) -> None:
        self.stdin = _FakeStdin()
        self.returncode = returncode
        self._poll_result = poll_result
        self.terminated = False
        self.killed = False
        self.wait_calls: list[float | None] = []

    def poll(self):
        return self._poll_result

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0
        self._poll_result = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._poll_result = -9


class _FakeThread:
    def __init__(self) -> None:
        self.join_timeouts: list[float | None] = []

    def join(self, timeout=None) -> None:
        self.join_timeouts.append(timeout)


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


def test_start_timeout_joins_reader_threads_after_terminating_helper(
    tmp_path: Path,
) -> None:
    recorder = SingleFrameRecorder(camera_id="video=Test", output_dir=tmp_path)
    fake_process = _FakeProcess()
    stdout_thread = _FakeThread()
    stderr_thread = _FakeThread()

    def fake_thread(*args, **kwargs):
        del args, kwargs
        thread = stdout_thread if recorder._stdout_thread is None else stderr_thread
        thread.start = lambda: None  # type: ignore[attr-defined]
        return thread

    with (
        mock.patch("micecam.single_frame_recorder.sys.platform", "win32"),
        mock.patch.object(recorder, "_resolve_helper_path", return_value=tmp_path / "helper.exe"),
        mock.patch.object(recorder._ready_event, "wait", return_value=False),
        mock.patch("micecam.single_frame_recorder.subprocess.Popen", return_value=fake_process),
        mock.patch("micecam.single_frame_recorder.threading.Thread", side_effect=fake_thread),
    ):
        with pytest.raises(RuntimeError, match="did not report ready"):
            recorder.start()

    assert fake_process.terminated is True
    assert stdout_thread.join_timeouts == [2]
    assert stderr_thread.join_timeouts == [2]
    assert recorder.is_recording() is False


def test_terminate_helper_logs_when_terminate_and_kill_fail(
    tmp_path: Path,
) -> None:
    recorder = SingleFrameRecorder(camera_id="video=Test", output_dir=tmp_path)
    fake_process = mock.Mock()
    fake_process.poll.return_value = None
    fake_process.returncode = None
    fake_process.terminate.side_effect = PermissionError("no terminate")
    fake_process.kill.side_effect = PermissionError("no kill")
    recorder._process = fake_process
    recorder._is_recording = True

    with mock.patch("micecam.single_frame_recorder.logger") as logger:
        recorder._terminate_helper()

    assert logger.warning.call_count == 2
    assert recorder.is_recording() is False


def test_stop_records_nonzero_exit_code_even_when_error_already_set(
    tmp_path: Path,
) -> None:
    recorder = SingleFrameRecorder(camera_id="video=Test", output_dir=tmp_path)
    recorder._process = _FakeProcess(returncode=7)
    recorder._stdout_thread = _FakeThread()  # type: ignore[assignment]
    recorder._stderr_thread = _FakeThread()  # type: ignore[assignment]
    recorder._is_recording = True
    recorder._session_dir = tmp_path / "session"
    recorder._frame_log_path = recorder._session_dir / "frame_log.csv"
    recorder._metadata_path = recorder._session_dir / "metadata.json"
    recorder._mark_failed("helper error event")

    with pytest.raises(RuntimeError, match="helper error event"):
        recorder.stop()

    assert "helper exited with code 7" in recorder.warnings
