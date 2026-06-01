"""
Per-camera control panel with live preview, settings, and recording controls.

Architecture
------------
*Preview*: A lightweight ffmpeg subprocess decodes the camera feed to raw
RGB24 frames piped to stdout. A background QThread reads these frames and
emits them as QImage signals for the UI thread to display in a QLabel.

*Recording*: A separate Recorder instance runs a full-quality ffmpeg encode
to MP4 while the preview continues (albeit at a lower resolution / framerate
to avoid saturating the camera hardware).
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PyQt6 import QtCore, QtGui, QtWidgets

from micecam.camera_manager import CameraInfo, get_ffmpeg_path
from micecam.recorder import Recorder

logger = logging.getLogger(__name__)

# Preview dimensions (kept low to avoid taxing the camera / USB bus)
PREVIEW_W = 640
PREVIEW_H = 480


# ── Preview capture thread ──────────────────────────────────────────

class PreviewThread(QtCore.QThread):
    """
    Runs ffmpeg in a subprocess to capture raw RGB24 frames for preview.

    Emits ``frame_ready(QImage)`` on each decoded frame and
    ``preview_error(str)`` when the capture pipeline fails.
    """

    frame_ready = QtCore.pyqtSignal(QtGui.QImage)
    preview_error = QtCore.pyqtSignal(str)

    def __init__(self, camera_id: str, fps: int = 30,
                 parent: Optional[QtCore.QObject] = None):
        super().__init__(parent)
        self.camera_id = camera_id
        self.fps = fps
        self._running = False
        self._process: Optional[subprocess.Popen] = None

    def run(self) -> None:
        self._running = True
        ffmpeg = get_ffmpeg_path()
        system = sys.platform

        if system == "darwin":
            input_args = [
                "-f", "avfoundation",
                "-framerate", str(self.fps),
                "-video_size", f"{PREVIEW_W}x{PREVIEW_H}",
                "-i", self.camera_id,
            ]
        elif system == "win32":
            # camera_id is already the full dshow device specifier
            # e.g. 'video=Integrated Camera' (NO shell quotes — subprocess list mode)
            input_args = [
                "-f", "dshow",
                "-framerate", str(self.fps),
                "-video_size", f"{PREVIEW_W}x{PREVIEW_H}",
                "-i", self.camera_id,
            ]
        else:
            input_args = [
                "-f", "v4l2",
                "-framerate", str(self.fps),
                "-video_size", f"{PREVIEW_W}x{PREVIEW_H}",
                "-i", self.camera_id,
            ]

        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            *input_args,
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-an",
            "-",
        ]
        logger.debug("Preview cmd: %s", " ".join(cmd))

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self.preview_error.emit("ffmpeg not found")
            return

        frame_size = PREVIEW_W * PREVIEW_H * 3  # RGB24 = 3 bytes/pixel
        first_frame = True
        stdout = self._process.stdout
        while self._running and self._process.poll() is None:
            try:
                raw = stdout.read(frame_size)  # type: ignore[union-attr]
            except Exception:
                break
            if len(raw) < frame_size:
                break

            if first_frame:
                first_frame = False
                logger.debug("Preview: first frame received for %s", self.camera_id)

            # Build QImage from raw RGB24 data
            image = QtGui.QImage(
                raw, PREVIEW_W, PREVIEW_H, PREVIEW_W * 3,
                QtGui.QImage.Format.Format_RGB888,
            )
            if not image.isNull():
                self.frame_ready.emit(image.copy())  # copy for thread safety

        # If the process exited before we ever got a frame, read stderr for diagnostics
        if first_frame and self._process is not None:
            stderr_output = ""
            try:
                stderr_output = self._process.stderr.read().decode(
                    "utf-8", errors="replace"
                ) if self._process.stderr else ""
            except Exception:
                pass
            err_msg = stderr_output.strip() or "Preview process exited without output"
            logger.error("Preview failed for %s: %s", self.camera_id, err_msg)
            self.preview_error.emit(err_msg[:200])

        self._cleanup()

    def stop(self) -> None:
        """Signal the thread to stop and clean up the ffmpeg process."""
        self._running = False
        self._cleanup()
        self.wait(3000)  # PyQt6: QThread.wait(time) — positional, milliseconds

    def _cleanup(self) -> None:
        proc = self._process
        if proc is None:
            return
        if proc.poll() is not None:
            return  # already exited

        # Close stdout to unblock any pending read
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass

        # Graceful terminate, then force-kill after a short grace period
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


# ── Camera panel widget ─────────────────────────────────────────────

class CameraPanel(QtWidgets.QGroupBox):
    """
    A self-contained panel for one camera: preview + controls + recording.

    Layout::

        ┌──────────────────────────────┐
        │  Camera Panel — "Cam Name"   │
        ├──────────────────────────────┤
        │                              │
        │     [ Live Preview ]         │
        │      640 × 480               │
        │                              │
        ├──────────────────────────────┤
        │  Camera: [dropdown       ▼] │
        │  Res:    [1920×1080     ▼]  │
        │  FPS:    [30            ▼]  │
        │  Codec:  [H.264         ▼]  │
        │  Output: [ ...  ] [Browse]  │
        │                              │
        │  [● Start Recording]         │
        │  Status: idle                │
        └──────────────────────────────┘
    """

    recording_started = QtCore.pyqtSignal(str)   # camera_name
    recording_stopped = QtCore.pyqtSignal(str)   # camera_name
    _props_closed = QtCore.pyqtSignal()          # internal: camera property dialog closed

    def __init__(self, panel_id: int,
                 cameras: list[CameraInfo],
                 parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.panel_id = panel_id
        self._cameras = cameras
        self._preview_thread: Optional[PreviewThread] = None
        self._recorder: Optional[Recorder] = None
        self._current_camera: Optional[CameraInfo] = None

        self.setTitle(f"Camera {panel_id + 1}")
        self._props_closed.connect(self._on_properties_closed)
        self._build_ui()
        self._populate_cameras()

    # ── UI construction ──────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(6)

        # --- Preview ---
        self._preview_label = QtWidgets.QLabel()
        self._preview_label.setFixedSize(PREVIEW_W, PREVIEW_H)
        self._preview_label.setStyleSheet(
            "QLabel { background: #1a1a1a; border: 1px solid #444; "
            "color: #666; font-size: 14px; }"
        )
        self._preview_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setText("No preview")
        layout.addWidget(self._preview_label, alignment=QtCore.Qt.AlignmentFlag.AlignCenter)

        # --- Controls form ---
        form = QtWidgets.QFormLayout()
        form.setSpacing(4)

        self._cam_combo = QtWidgets.QComboBox()
        self._cam_combo.currentIndexChanged.connect(self._on_camera_changed)

        self._cam_props_btn = QtWidgets.QPushButton("⚙ Properties")
        self._cam_props_btn.setToolTip(
            "Open the camera's DirectShow property dialog.\n"
            "Use this to adjust exposure, gain, anti-flicker, and other\n"
            "driver-level settings that affect framerate."
        )
        self._cam_props_btn.setMinimumHeight(24)
        self._cam_props_btn.clicked.connect(self._open_camera_properties)

        cam_row = QtWidgets.QHBoxLayout()
        cam_row.addWidget(self._cam_combo, 1)
        cam_row.addWidget(self._cam_props_btn, 0)
        form.addRow("Camera:", cam_row)

        self._res_combo = QtWidgets.QComboBox()
        self._res_combo.currentIndexChanged.connect(self._on_resolution_changed)
        form.addRow("Resolution:", self._res_combo)

        self._fps_combo = QtWidgets.QComboBox()
        form.addRow("FPS:", self._fps_combo)

        self._codec_combo = QtWidgets.QComboBox()
        self._codec_combo.addItems(["H.264", "H.265 (HEVC)"])
        form.addRow("Codec:", self._codec_combo)

        # Output directory
        dir_row = QtWidgets.QHBoxLayout()
        self._output_edit = QtWidgets.QLineEdit(str(Path.cwd() / "output"))
        self._output_btn = QtWidgets.QPushButton("Browse")
        self._output_btn.clicked.connect(self._browse_output)
        dir_row.addWidget(self._output_edit)
        dir_row.addWidget(self._output_btn)
        form.addRow("Output:", dir_row)

        layout.addLayout(form)

        # --- Record button ---
        btn_row = QtWidgets.QHBoxLayout()
        self._record_btn = QtWidgets.QPushButton("●  Start Recording")
        self._record_btn.setMinimumHeight(36)
        self._record_btn.setStyleSheet(
            "QPushButton { background: #c0392b; color: white; font-weight: bold; "
            "border-radius: 4px; padding: 6px 16px; }"
            "QPushButton:hover { background: #e74c3c; }"
            "QPushButton:disabled { background: #555; color: #999; }"
        )
        self._record_btn.clicked.connect(self._toggle_recording)
        btn_row.addWidget(self._record_btn)
        layout.addLayout(btn_row)

        # --- Status ---
        self._status_label = QtWidgets.QLabel("●  Idle")
        self._status_label.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(self._status_label)

    # ── Camera selection ─────────────────────────────────────────────

    def _populate_cameras(self) -> None:
        self._cam_combo.clear()
        for cam in self._cameras:
            self._cam_combo.addItem(f"[{cam.index}] {cam.name}", cam)
        if self._cameras:
            self._on_camera_changed(0)

    def _on_camera_changed(self, index: int) -> None:
        if index < 0 or index >= len(self._cameras):
            return
        cam = self._cameras[index]
        self._current_camera = cam

        # Populate resolution dropdown
        self._res_combo.blockSignals(True)
        self._res_combo.clear()
        for w, h in cam.supported_resolutions:
            self._res_combo.addItem(f"{w}×{h}", (w, h))
        self._res_combo.blockSignals(False)

        # Populate FPS dropdown for the first resolution
        if cam.supported_resolutions:
            self._update_fps_for_resolution(cam.supported_resolutions[0])

        # Start preview for this camera
        self._start_preview(cam)

    def _on_resolution_changed(self, index: int) -> None:
        """When the user picks a different resolution, update the FPS dropdown
        to show only framerates the camera actually supports at that resolution."""
        res = self._res_combo.itemData(index)
        if res:
            self._update_fps_for_resolution(res)

    def _open_camera_properties(self) -> None:
        """Open the camera's DirectShow property dialog.

        This allows the user to adjust driver-level settings (exposure,
        auto-exposure, low-light compensation, gain, anti-flicker, etc.)
        that ffmpeg cannot control via CLI.  These settings directly affect
        the camera's actual output framerate.

        The preview is paused while the dialog is open because the camera
        cannot be accessed by two processes simultaneously.
        """
        cam = self._current_camera
        if cam is None:
            QtWidgets.QMessageBox.information(
                self, "No Camera", "Select a camera first.",
            )
            return

        # Pause preview — the property dialog needs exclusive camera access
        self._stop_preview()

        # Launch ffmpeg to show the DirectShow property page.
        # We use a subprocess in a daemon thread: the Windows property sheet
        # is a modal dialog that blocks ffmpeg, but Qt's event loop stays
        # responsive because we don't call .join() on the main thread.
        import threading

        cam_id = cam.platform_id

        def _show_dialog_then_restart():
            try:
                proc = subprocess.run(
                    [
                        get_ffmpeg_path(), "-hide_banner", "-loglevel", "error",
                        "-f", "dshow",
                        "-show_video_device_dialog", "true",
                        "-i", cam_id,
                        "-vframes", "0",
                        "-f", "null", "-",
                    ],
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    timeout=120,
                )
                if proc.returncode != 0:
                    logger.warning(
                        "Camera property dialog exited with code %d: %s",
                        proc.returncode, proc.stderr.strip() or proc.stdout.strip(),
                    )
            except subprocess.TimeoutExpired:
                logger.warning("Camera property dialog timed out")
            except Exception:
                logger.exception("Unexpected error opening camera properties")
            finally:
                # Restart preview from the main thread via signal
                self._props_closed.emit()

        t = threading.Thread(target=_show_dialog_then_restart, daemon=True)
        t.start()

    def _on_properties_closed(self) -> None:
        """Callback after the camera property dialog closes — restart preview."""
        if self._current_camera:
            self._start_preview(self._current_camera)

    def _update_fps_for_resolution(self, res: tuple[int, int]) -> None:
        """Populate the FPS dropdown with values valid for *res*.

        If the camera has per-resolution FPS data (Windows dshow), use it.
        Otherwise fall back to the global framerate list.
        """
        cam = self._current_camera
        if cam is None:
            return

        # Use per-resolution FPS map if available (Windows); fall back to flat list
        if cam.resolution_fps and res in cam.resolution_fps:
            fps_list = cam.resolution_fps[res]
        else:
            fps_list = cam.supported_framerates

        previous = self._fps_combo.currentData()
        self._fps_combo.clear()
        for fps in fps_list:
            self._fps_combo.addItem(f"{fps} fps", fps)

        # Restore previous FPS selection if still valid
        if previous and previous in fps_list:
            idx = fps_list.index(previous)
            self._fps_combo.setCurrentIndex(idx)
        elif fps_list:
            # Default to a moderate FPS: prefer 30, then the closest value
            if 30 in fps_list:
                self._fps_combo.setCurrentIndex(fps_list.index(30))
            else:
                self._fps_combo.setCurrentIndex(0)

    def refresh_cameras(self, cameras: list[CameraInfo]) -> None:
        """Update the camera list (e.g., after a device change)."""
        current_id = self._cam_combo.currentData()
        self._cameras = cameras
        self._cam_combo.blockSignals(True)
        self._cam_combo.clear()
        for cam in cameras:
            self._cam_combo.addItem(f"[{cam.index}] {cam.name}", cam)
        # Restore selection
        restored = False
        if current_id:
            for i in range(self._cam_combo.count()):
                if self._cam_combo.itemData(i) is current_id:
                    self._cam_combo.setCurrentIndex(i)
                    restored = True
                    break
        self._cam_combo.blockSignals(False)
        # If no previous selection or it wasn't found, select first camera
        if not restored and cameras:
            self._on_camera_changed(0)

    # ── Preview ──────────────────────────────────────────────────────

    def _start_preview(self, cam: CameraInfo) -> None:
        self._stop_preview()
        # Preview always runs at a moderate framerate — high FPS (e.g. 120)
        # is rarely supported at 640×480 and would cause ffmpeg to fail.
        # Pick the closest fps ≤ 30 from the camera's supported list.
        supported = cam.supported_framerates or [30]
        preview_fps = 30
        for f in supported:
            if f <= 30 and (30 - f) < (30 - preview_fps):
                preview_fps = f
        logger.debug("Preview using %d fps (supported: %s)", preview_fps, supported)
        self._preview_thread = PreviewThread(cam.platform_id, fps=preview_fps, parent=self)
        self._preview_thread.frame_ready.connect(self._on_frame)
        self._preview_thread.preview_error.connect(self._on_preview_error)
        self._preview_thread.start()
        self._preview_label.setText("Connecting...")

    def _stop_preview(self) -> None:
        if self._preview_thread is not None:
            self._preview_thread.stop()
            self._preview_thread = None

    def _on_frame(self, image: QtGui.QImage) -> None:
        pixmap = QtGui.QPixmap.fromImage(image)
        self._preview_label.setPixmap(pixmap)

    def _on_preview_error(self, msg: str) -> None:
        self._preview_label.setText(f"Error: {msg}")

    def _browse_output(self) -> None:
        """Open a directory chooser dialog for the output folder."""
        from PyQt6 import QtWidgets

        current = self._output_edit.text()
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select Output Directory", current,
        )
        if folder:
            self._output_edit.setText(folder)

    # ── Recording ────────────────────────────────────────────────────

    def _toggle_recording(self) -> None:
        if self._recorder and self._recorder.is_recording():
            self._stop_recording()
        else:
            self._start_recording()

    def create_recorder(self) -> Recorder | None:
        """
        Build a configured (but not started) Recorder from the current UI settings.

        Returns None if no camera is selected or settings are invalid.
        """
        if not self._current_camera:
            return None

        res = self._res_combo.currentData()
        fps = self._fps_combo.currentData()
        if not res or not fps:
            return None

        output_dir = Path(self._output_edit.text())
        cam = self._current_camera
        return Recorder(
            camera_id=cam.platform_id,
            camera_name=cam.name,
            output_dir=output_dir,
            native_codec=cam.native_codec,
        )

    def get_config(self) -> dict:
        """Return the current UI settings as a dict (for SyncController use)."""
        return {
            "resolution": self._res_combo.currentData() or (1920, 1080),
            "fps": self._fps_combo.currentData() or 30,
            "codec": "hevc" if "265" in self._codec_combo.currentText() else "h264",
        }

    def set_recorder(self, recorder: Recorder) -> None:
        """Accept an externally-created Recorder (e.g., from SyncController)."""
        self._recorder = recorder

    def _update_ui_recording_started(self, output_name: str, cam_name: str) -> None:
        """Update UI state to reflect active recording."""
        self._record_btn.setText("■  Stop Recording")
        self._record_btn.setStyleSheet(
            "QPushButton { background: #555; color: white; font-weight: bold; "
            "border-radius: 4px; padding: 6px 16px; }"
            "QPushButton:hover { background: #777; }"
        )
        self._status_label.setText(f"●  Recording → {output_name}")
        self._status_label.setStyleSheet("color: #e74c3c; font-size: 12px;")
        self.setTitle(f"🔴 Camera {self.panel_id + 1} — {cam_name}")
        self.recording_started.emit(cam_name)

    def _update_ui_recording_stopped(self, mp4_name: str, srt_name: str) -> None:
        """Update UI state to reflect stopped recording."""
        self._record_btn.setText("●  Start Recording")
        self._record_btn.setStyleSheet(
            "QPushButton { background: #c0392b; color: white; font-weight: bold; "
            "border-radius: 4px; padding: 6px 16px; }"
            "QPushButton:hover { background: #e74c3c; }"
        )
        self._status_label.setText(f"✓  Saved: {mp4_name}  |  SRT: {srt_name}")
        self._status_label.setStyleSheet("color: #27ae60; font-size: 12px;")
        cam_name = self._current_camera.name if self._current_camera else ""
        self.setTitle(f"Camera {self.panel_id + 1} — {cam_name}")
        self.recording_stopped.emit(cam_name)

    def _start_recording(self) -> None:
        """Single-camera start (no sync). Uses independent clock references."""
        if not self._current_camera:
            return

        cfg = self.get_config()
        if not cfg["resolution"] or not cfg["fps"]:
            QtWidgets.QMessageBox.warning(self, "Settings", "Select resolution and FPS first.")
            return

        self._recorder = self.create_recorder()
        if self._recorder is None:
            return

        # Pause preview during recording — many dshow cameras cannot serve
        # two ffmpeg clients simultaneously, and sharing USB bandwidth between
        # a preview stream and a recording stream causes frame drops.
        self._stop_preview()

        try:
            output_path = self._recorder.start(**cfg)
        except Exception as exc:
            # Restart preview on failure
            if self._current_camera:
                self._start_preview(self._current_camera)
            QtWidgets.QMessageBox.critical(self, "Error", f"Failed to start recording:\n{exc}")
            return

        cam_name = self._current_camera.name
        self._update_ui_recording_started(output_path.name, cam_name)

    def _stop_recording(self) -> None:
        if not self._recorder:
            return

        try:
            mp4_path, srt_path = self._recorder.stop()
        except Exception as exc:
            logger.error("Error stopping recorder: %s", exc)
            return

        self._update_ui_recording_stopped(mp4_path.name, srt_path.name)

        # Restart preview now that recording has finished
        if self._current_camera:
            self._start_preview(self._current_camera)

    # ── Lifecycle ────────────────────────────────────────────────────

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # type: ignore[override]
        self._stop_preview()
        if self._recorder and self._recorder.is_recording():
            self._recorder.stop()
        super().closeEvent(event)

    def shutdown(self) -> None:
        """Clean shutdown — stop preview and recording."""
        self._stop_preview()
        if self._recorder and self._recorder.is_recording():
            try:
                self._recorder.stop()
            except Exception:
                pass
