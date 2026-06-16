from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets

from micecam.camera_manager import CameraInfo
from micecam.gui.camera_panel import CameraPanel
from micecam.recorder import Recorder
from micecam.single_frame_recorder import SingleFrameRecorder

_APP: QtWidgets.QApplication | None = None


def _app() -> QtWidgets.QApplication:
    global _APP
    _APP = QtWidgets.QApplication.instance()
    if _APP is None:
        _APP = QtWidgets.QApplication(sys.argv)
    return _APP


def _camera() -> CameraInfo:
    return CameraInfo(
        index=0,
        name="Test Camera",
        platform_id="video=Test Camera",
        device_number=1,
        supported_resolutions=[(1280, 720)],
        supported_framerates=[30],
        native_codec="mjpeg",
        mode_codecs={(1280, 720, 30): ["mjpeg"]},
    )


def test_backend_combo_defaults_to_ffmpeg_video(tmp_path: Path) -> None:
    _app()
    with mock.patch.object(CameraPanel, "_start_preview"):
        panel = CameraPanel(0, [_camera()])
    panel._output_edit.setText(str(tmp_path))

    assert panel._backend_combo.currentData() == "ffmpeg_video"
    assert isinstance(panel.create_recorder(), Recorder)


def test_backend_combo_creates_single_frame_recorder_on_windows(tmp_path: Path) -> None:
    _app()
    with mock.patch.object(CameraPanel, "_start_preview"):
        panel = CameraPanel(0, [_camera()])
    panel._output_edit.setText(str(tmp_path))
    panel._backend_combo.setCurrentIndex(panel._backend_combo.findData("single_frame_mode"))

    with mock.patch("micecam.gui.camera_panel.sys.platform", "win32"):
        recorder = panel.create_recorder()

    assert isinstance(recorder, SingleFrameRecorder)
    assert recorder.camera_id == "video=Test Camera"
    assert recorder.camera_device_number == 1


def test_shutdown_logs_recorder_stop_errors() -> None:
    _app()
    with mock.patch.object(CameraPanel, "_start_preview"):
        panel = CameraPanel(0, [_camera()])
    recorder = mock.Mock()
    recorder.is_recording.return_value = True
    recorder.stop.side_effect = RuntimeError("stop failed")
    panel.set_recorder(recorder)

    with (
        mock.patch.object(panel, "_stop_preview"),
        mock.patch("micecam.gui.camera_panel.logger") as logger,
    ):
        panel.shutdown()

    logger.exception.assert_called_once()
