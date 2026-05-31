"""Entry point module for the ``micecam`` console script.

Defines ``main()`` here rather than importing from the package to avoid
a name collision: ``from micecam import main`` would resolve to this very
module (``micecam.main``) before the function ``micecam.main()`` in
``__init__.py``, producing "module object is not callable".
"""

import logging
import sys


def main() -> None:
    """Entry point for the ``micecam`` console script."""
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


if __name__ == "__main__":
    main()
