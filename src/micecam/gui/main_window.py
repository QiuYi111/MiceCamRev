"""
Main application window — orchestrates two camera panels side by side
with soft-sync dual-camera recording via SyncController.
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6 import QtCore, QtWidgets

from micecam.camera_manager import CameraInfo, list_cameras
from micecam.core.sync_controller import SyncController
from micecam.gui.camera_panel import CameraPanel

logger = logging.getLogger(__name__)


class MainWindow(QtWidgets.QMainWindow):
    """
    Top-level window with two camera panels, sync controls, and status bar.

    Layout::

        ┌──────────────────────────────────────────────────────┐
        │  MiceCam — Dual Camera Recorder          [─][□][×]  │
        ├──────────────────────────────────────────────────────┤
        │  Menu: File | Help                                   │
        ├───────────────────────┬──────────────────────────────┤
        │   Camera 1 Panel      │    Camera 2 Panel            │
        │   ┌───────────────┐   │    ┌───────────────┐        │
        │   │   Preview     │   │    │   Preview     │        │
        │   └───────────────┘   │    └───────────────┘        │
        │   [● Record]          │    [● Record]               │
        ├───────────────────────┴──────────────────────────────┤
        │  [▶ Start Both]  [■ Stop Both]   (soft-sync)        │
        ├──────────────────────────────────────────────────────┤
        │  Status: Ready                                       │
        └──────────────────────────────────────────────────────┘
    """

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("MiceCam — Dual Camera Recorder")
        self.setMinimumSize(1100, 700)

        self._cameras: list[CameraInfo] = []
        self._panel1: Optional[CameraPanel] = None
        self._panel2: Optional[CameraPanel] = None
        self._sync = SyncController()

        self._build_ui()
        self._discover_cameras()

    # ── UI construction ──────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        main_layout = QtWidgets.QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # Two panels side by side
        panels_layout = QtWidgets.QHBoxLayout()
        panels_layout.setSpacing(8)

        self._panel1 = CameraPanel(0, [], self)
        self._panel2 = CameraPanel(1, [], self)
        panels_layout.addWidget(self._panel1)
        panels_layout.addWidget(self._panel2)
        main_layout.addLayout(panels_layout)

        # ── Sync control bar ──────────────────────────────────────
        sync_bar = QtWidgets.QHBoxLayout()
        sync_bar.setSpacing(8)

        sync_label = QtWidgets.QLabel("Sync Control:")
        sync_label.setStyleSheet("color: #aaa; font-size: 12px;")
        sync_bar.addWidget(sync_label)

        self._start_both_btn = QtWidgets.QPushButton("▶  Start Both (Synced)")
        self._start_both_btn.setMinimumHeight(32)
        self._start_both_btn.setToolTip(
            "Start both cameras simultaneously with a shared time base.\n"
            "Both SRT files will reference the same wall clock for "
            "post-hoc frame alignment."
        )
        self._start_both_btn.setStyleSheet(
            "QPushButton { background: #27ae60; color: white; font-weight: bold; "
            "border-radius: 4px; padding: 6px 16px; }"
            "QPushButton:hover { background: #2ecc71; }"
            "QPushButton:disabled { background: #555; color: #999; }"
        )
        self._start_both_btn.clicked.connect(self._start_both)
        sync_bar.addWidget(self._start_both_btn)

        self._stop_both_btn = QtWidgets.QPushButton("■  Stop Both")
        self._stop_both_btn.setMinimumHeight(32)
        self._stop_both_btn.setToolTip("Stop all active recordings.")
        self._stop_both_btn.setStyleSheet(
            "QPushButton { background: #555; color: white; font-weight: bold; "
            "border-radius: 4px; padding: 6px 16px; }"
            "QPushButton:hover { background: #777; }"
            "QPushButton:disabled { background: #444; color: #777; }"
        )
        self._stop_both_btn.clicked.connect(self._stop_both)
        self._stop_both_btn.setEnabled(False)
        sync_bar.addWidget(self._stop_both_btn)

        sync_bar.addStretch()
        main_layout.addLayout(sync_bar)

        # ── Status bar ─────────────────────────────────────────────
        self._status_bar = self.statusBar()
        self._status_bar.showMessage("Discovering cameras...")

        # ── Menu bar ───────────────────────────────────────────────
        menubar = self.menuBar()

        file_menu = menubar.addMenu("&File")
        refresh_action = file_menu.addAction("&Refresh Cameras")
        refresh_action.setShortcut("Ctrl+R")
        refresh_action.triggered.connect(self._discover_cameras)
        file_menu.addSeparator()
        quit_action = file_menu.addAction("&Quit")
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)

        help_menu = menubar.addMenu("&Help")
        about_action = help_menu.addAction("&About")
        about_action.triggered.connect(self._show_about)

    # ── Camera discovery ─────────────────────────────────────────────

    def _discover_cameras(self) -> None:
        self._status_bar.showMessage("Scanning for cameras...")
        QtCore.QTimer.singleShot(100, self._do_discover)

    def _do_discover(self) -> None:
        try:
            self._cameras = list_cameras(probe_capabilities=True)
        except Exception as exc:
            logger.error("Camera discovery failed: %s", exc)
            self._status_bar.showMessage(f"Camera discovery error: {exc}")
            return

        if not self._cameras:
            self._status_bar.showMessage(
                "No cameras found. Check connections and try File → Refresh."
            )
            return

        logger.info("Found %d camera(s)", len(self._cameras))
        for cam in self._cameras:
            logger.info("  [%d] %s — res=%s fps=%s",
                        cam.index, cam.name,
                        cam.supported_resolutions, cam.supported_framerates)

        self._panel1.refresh_cameras(self._cameras)
        self._panel2.refresh_cameras(self._cameras)

        self._status_bar.showMessage(
            f"Ready — {len(self._cameras)} camera(s) found"
        )

    # ── Sync control (soft-sync dual recording) ──────────────────────

    def _start_both(self) -> None:
        """Launch both cameras with a shared wall-clock reference."""
        cfg1 = self._panel1.get_config()
        cfg2 = self._panel2.get_config()

        rec_a = self._panel1.create_recorder()
        rec_b = self._panel2.create_recorder()

        if rec_a is None or rec_b is None:
            QtWidgets.QMessageBox.warning(
                self, "Cannot Start",
                "Both cameras must be selected and configured before synced start.",
            )
            return

        try:
            self._sync.start_both(
                rec_a, cfg1["resolution"], cfg1["fps"], cfg1["codec"],
                rec_b, cfg2["resolution"], cfg2["fps"], cfg2["codec"],
            )
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                self, "Sync Error", f"Failed to start synced recording:\n{exc}",
            )
            return

        # Hand recorders back to panels so they own the stop lifecycle
        self._panel1.set_recorder(rec_a)
        self._panel2.set_recorder(rec_b)

        # Update panel UIs
        self._panel1._update_ui_recording_started(
            rec_a._output_path.name, rec_a.camera_name,
        )
        self._panel2._update_ui_recording_started(
            rec_b._output_path.name, rec_b.camera_name,
        )

        # Update sync buttons
        self._start_both_btn.setEnabled(False)
        self._stop_both_btn.setEnabled(True)
        self._stop_both_btn.setStyleSheet(
            "QPushButton { background: #c0392b; color: white; font-weight: bold; "
            "border-radius: 4px; padding: 6px 16px; }"
            "QPushButton:hover { background: #e74c3c; }"
        )

        self._status_bar.showMessage(
            f"● Recording — shared time base | "
            f"wall_start={self._sync.wall_start:.3f}"
        )

    def _stop_both(self) -> None:
        """Stop all synced recordings."""
        self._sync.stop_both()

        # Notify panels to refresh UI
        for panel in (self._panel1, self._panel2):
            rec = panel._recorder  # type: ignore[union-attr]
            if rec is not None and not rec.is_recording():
                mp4 = rec.output_path.name if rec.output_path else "unknown"
                srt = rec.srt_path.name if rec.srt_path else "unknown"
                panel._update_ui_recording_stopped(mp4, srt)  # type: ignore[union-attr]

        self._start_both_btn.setEnabled(True)
        self._stop_both_btn.setEnabled(False)
        self._stop_both_btn.setStyleSheet(
            "QPushButton { background: #555; color: white; font-weight: bold; "
            "border-radius: 4px; padding: 6px 16px; }"
            "QPushButton:hover { background: #777; }"
        )

        self._status_bar.showMessage("Recording stopped — files saved")

    # ── About ────────────────────────────────────────────────────────

    def _show_about(self) -> None:
        QtWidgets.QMessageBox.about(
            self, "About MiceCam",
            "<h3>MiceCam</h3>"
            "<p>Dual-camera video recorder with precise SRT timestamps.</p>"
            "<p>ffmpeg backend · H.264/H.265 · nanosecond SRT timestamps</p>"
            "<p><b>Soft sync:</b> shared wall-clock reference for cross-camera "
            "temporal alignment</p>"
            "<p><i>Built with PyQt6 + ffmpeg</i></p>",
        )

    # ── Lifecycle ────────────────────────────────────────────────────

    def closeEvent(self, event: QtCore.QEvent) -> None:
        """Ensure clean shutdown of all previews and recordings."""
        logger.info("Shutting down...")
        if self._sync.is_recording:
            self._sync.stop_both()
        if self._panel1:
            self._panel1.shutdown()
        if self._panel2:
            self._panel2.shutdown()
        event.accept()
