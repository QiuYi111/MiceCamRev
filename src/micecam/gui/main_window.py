"""
Main application window — orchestrates two camera panels side by side.
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6 import QtCore, QtWidgets

from micecam.camera_manager import CameraInfo, list_cameras
from micecam.gui.camera_panel import CameraPanel

logger = logging.getLogger(__name__)


class MainWindow(QtWidgets.QMainWindow):
    """
    Top-level window with two camera panels, a menu bar, and a status bar.

    Layout::

        ┌──────────────────────────────────────────────────────┐
        │  MiceCam — Dual Camera Recorder          [─][□][×]  │
        ├──────────────────────────────────────────────────────┤
        │  Menu: File | Help                                   │
        ├───────────────────────┬──────────────────────────────┤
        │   Camera 1 Panel      │    Camera 2 Panel            │
        │   ┌───────────────┐   │    ┌───────────────┐        │
        │   │   Preview     │   │    │   Preview     │        │
        │   │               │   │    │               │        │
        │   └───────────────┘   │    └───────────────┘        │
        │   [Settings...]       │    [Settings...]             │
        │   [● Record]          │    [● Record]               │
        ├───────────────────────┴──────────────────────────────┤
        │  Status: Ready  |  Camera 1: idle  |  Camera 2: idle │
        └──────────────────────────────────────────────────────┘
    """

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("MiceCam — Dual Camera Recorder")
        self.setMinimumSize(1100, 650)

        self._cameras: list[CameraInfo] = []
        self._panel1: Optional[CameraPanel] = None
        self._panel2: Optional[CameraPanel] = None

        self._build_ui()
        self._discover_cameras()

    # ── UI construction ──────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Central widget
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        main_layout = QtWidgets.QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # Two panels side by side
        panels_layout = QtWidgets.QHBoxLayout()
        panels_layout.setSpacing(8)

        # Placeholder panels — populated after camera discovery
        self._panel1 = CameraPanel(0, [], self)
        self._panel2 = CameraPanel(1, [], self)
        panels_layout.addWidget(self._panel1)
        panels_layout.addWidget(self._panel2)
        main_layout.addLayout(panels_layout)

        # Status bar
        self._status_bar = self.statusBar()
        self._status_bar.showMessage("Discovering cameras...")

        # Menu bar
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
        """Scan for available cameras and populate panels."""
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

    # ── About ────────────────────────────────────────────────────────

    def _show_about(self) -> None:
        QtWidgets.QMessageBox.about(
            self, "About MiceCam",
            "<h3>MiceCam</h3>"
            "<p>Dual-camera video recorder with precise SRT timestamps.</p>"
            "<p>ffmpeg backend · H.264/H.265 · nanosecond SRT timestamps</p>"
            "<p><i>Built with PyQt6 + ffmpeg</i></p>",
        )

    # ── Lifecycle ────────────────────────────────────────────────────

    def closeEvent(self, event: QtCore.QEvent) -> None:
        """Ensure clean shutdown of all previews and recordings."""
        logger.info("Shutting down...")
        if self._panel1:
            self._panel1.shutdown()
        if self._panel2:
            self._panel2.shutdown()
        event.accept()
