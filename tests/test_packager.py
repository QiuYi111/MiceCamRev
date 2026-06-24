from __future__ import annotations

from pathlib import Path

import packager


def test_generated_spec_includes_single_frame_helper_and_runtime_imports() -> None:
    spec = packager.build_spec(
        Path("ffmpeg_bundled/ffmpeg.exe"),
        Path("ffmpeg_bundled/mf_single_frame_helper.exe"),
    )

    assert "mf_single_frame_helper.exe" in spec
    assert "'micecam.single_frame_recorder'" in spec
    assert "'micecam.gui.camera_panel'" in spec
    assert "'PyQt6.sip'" in spec
    assert "'tkinter'" in spec
    assert "'ffmpeg.exe'" in spec
    assert "'Qt6Core.dll'" in spec


def test_makefile_mf_helper_target_has_windows_guard() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")

    assert "ifeq ($(OS),Windows_NT)" in makefile
    assert "mf-helper is Windows-only" in makefile
