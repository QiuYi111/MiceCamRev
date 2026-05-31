"""MiceCam — Dual-camera video recording with precise SRT timestamps."""

__version__ = "0.1.0"


def main() -> None:
    """Entry point for ``micecam`` console script and ``python -m micecam``."""
    import logging
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    from PyQt6 import QtWidgets

    from micecam.gui.main_window import MainWindow

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("MiceCam")
    app.setOrganizationName("MiceCam")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())
