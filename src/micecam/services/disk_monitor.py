"""
Periodic disk space monitoring with warning / critical thresholds.

Emits Qt signals so the UI can react without polling.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from PyQt6 import QtCore

logger = logging.getLogger(__name__)

# Thresholds in megabytes
DEFAULT_WARNING_MB = 1000
DEFAULT_CRITICAL_MB = 200


class DiskMonitor(QtCore.QObject):
    """
    Periodic disk-space checker for output directories.

    Emits:
        warning(str) — free space below warning threshold
        critical(str) — free space below critical threshold (UI should stop recording)
    """

    warning = QtCore.pyqtSignal(str)
    critical = QtCore.pyqtSignal(str)
    status_update = QtCore.pyqtSignal(str, float)  # path, free_mb

    def __init__(
        self,
        interval_ms: int = 30_000,
        warning_mb: int = DEFAULT_WARNING_MB,
        critical_mb: int = DEFAULT_CRITICAL_MB,
        parent: QtCore.QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._check)
        self._output_dirs: list[Path] = []
        self._warning_mb = warning_mb
        self._critical_mb = critical_mb
        self._active = False

    def start(self, output_dirs: list[Path]) -> None:
        """Begin monitoring. Runs an immediate check, then at the configured interval."""
        self._output_dirs = output_dirs
        self._active = True
        self._timer.start()
        self._check()

    def stop(self) -> None:
        """Stop monitoring."""
        self._active = False
        self._timer.stop()

    def set_thresholds(self, warning_mb: int, critical_mb: int) -> None:
        self._warning_mb = warning_mb
        self._critical_mb = critical_mb

    # ── internals ─────────────────────────────────────────────────────

    def _check(self) -> None:
        if not self._active:
            return
        for d in self._output_dirs:
            try:
                usage = shutil.disk_usage(d)
                free_mb = usage.free / (1024 * 1024)
            except OSError:
                logger.warning("Cannot check disk usage for %s", d)
                continue

            self.status_update.emit(str(d), free_mb)

            if free_mb < self._critical_mb:
                msg = (
                    f"CRITICAL: Only {free_mb:.0f} MB free on {d}. "
                    f"Recording should stop immediately."
                )
                logger.critical(msg)
                self.critical.emit(msg)
            elif free_mb < self._warning_mb:
                msg = (
                    f"WARNING: Only {free_mb:.0f} MB free on {d}."
                )
                logger.warning(msg)
                self.warning.emit(msg)
