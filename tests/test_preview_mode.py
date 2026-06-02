"""Tests for preview display sizing."""

from __future__ import annotations

from micecam.gui.camera_panel import _fit_preview_output_size


def test_preview_output_scales_large_capture_modes_for_ui() -> None:
    assert _fit_preview_output_size(1920, 1080) == (640, 360)
    assert _fit_preview_output_size(1280, 720) == (640, 360)
    assert _fit_preview_output_size(800, 600) == (640, 480)
    assert _fit_preview_output_size(320, 240) == (320, 240)
