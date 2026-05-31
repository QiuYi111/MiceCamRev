"""
SyncController — coordinates simultaneous dual-camera recording with a
shared wall-clock reference for soft synchronization.

How soft sync works
-------------------
Hardware genlock (frame-level sync) is not possible with consumer cameras.
Instead we provide **soft sync** via a shared time base:

1. ``time.time()`` is captured **once** at the moment both recordings start.
2. ``time.monotonic_ns()`` is captured **once** as the steady-clock anchor.
3. Both Recorders receive these shared references, so their SRT timestamps
   are directly comparable — you can align frames in post-processing by
   matching the sub-millisecond wall-clock values in both SRT files.

The two ffmpeg processes are launched back-to-back (no blocking I/O between
them), minimising the inter-start delay to < 10 ms in practice.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from micecam.recorder import Recorder

logger = logging.getLogger(__name__)


class SyncController:
    """
    Orchestrates synchronised start/stop of two Recorder instances.

    Usage::

        ctrl = SyncController()
        ctrl.start_both(rec_a, rec_b, fps=30, codec="h264")
        # ... recording ...
        ctrl.stop_both()

    After ``start_both``, ``ctrl.wall_start`` and ``ctrl.steady_start``
    hold the shared clock anchors.  Both SRT files will reference the
    same wall clock, making timestamps directly comparable.
    """

    def __init__(self) -> None:
        # Shared clock anchors — populated by start_both()
        self.wall_start: float = 0.0
        self.steady_start: int = 0

        self._recorders: list[Recorder] = []

    # ── public API ────────────────────────────────────────────────────

    def start_both(
        self,
        rec_a: Recorder,
        res_a: tuple[int, int],
        fps_a: int,
        codec_a: str,
        rec_b: Recorder,
        res_b: tuple[int, int],
        fps_b: int,
        codec_b: str,
    ) -> None:
        """
        Start two recorders with a shared clock reference.

        Both ffmpeg subprocesses are launched back-to-back so the
        inter-start delay is minimised.
        """
        # 1. Capture shared time reference — ONE atomic snapshot
        self.wall_start = time.time()
        self.steady_start = time.monotonic_ns()

        logger.info(
            "Sync start: wall=%.6f steady=%d",
            self.wall_start, self.steady_start,
        )

        # 2. Launch both ffmpeg processes back-to-back (no blocking between them)
        rec_a.start(
            resolution=res_a, fps=fps_a, codec=codec_a,
            wall_start=self.wall_start, steady_start=self.steady_start,
        )
        self._recorders.append(rec_a)

        rec_b.start(
            resolution=res_b, fps=fps_b, codec=codec_b,
            wall_start=self.wall_start, steady_start=self.steady_start,
        )
        self._recorders.append(rec_b)

        logger.info("Both recorders started with shared time base")

    def stop_both(self) -> None:
        """Stop all managed recorders."""
        for rec in self._recorders:
            if rec.is_recording():
                try:
                    rec.stop()
                except Exception:
                    logger.exception("Error stopping recorder %s", rec.camera_name)
        self._recorders.clear()
        logger.info("All recorders stopped")

    @property
    def is_recording(self) -> bool:
        return any(r.is_recording() for r in self._recorders)

    @property
    def active_count(self) -> int:
        return sum(1 for r in self._recorders if r.is_recording())
