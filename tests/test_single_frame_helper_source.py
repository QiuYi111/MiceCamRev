from __future__ import annotations

from pathlib import Path


HELPER_SOURCE = Path("helpers/mf_single_frame_helper.cpp")
BUILD_SCRIPT = Path("helpers/build_mf_helper.ps1")


def test_helper_main_does_not_block_on_stdin_after_capture_exits() -> None:
    source = HELPER_SOURCE.read_text(encoding="utf-8")

    assert "std::thread command" in source
    assert "capture.join();" in source
    assert "while (std::getline(std::cin, line))" not in source
    assert "state.capture_error" in source
    assert '"capture_error"' in source


def test_helper_uses_negotiated_capture_dimensions() -> None:
    source = HELPER_SOURCE.read_text(encoding="utf-8")

    assert "struct CaptureMode" in source
    assert "MFGetAttributeSize(current.Get(), MF_MT_FRAME_SIZE" in source
    assert "frame.width = mode.width" in source
    assert "frame.height = mode.height" in source


def test_helper_reports_lock_failures_and_throttles_dropped_events() -> None:
    source = HELPER_SOURCE.read_text(encoding="utf-8")

    assert "lock_failure_count" in source
    assert "Buffer Lock failed" in source
    assert "last_drop_emit_count" in source
    assert "drop_count % 30 == 0" in source


def test_build_script_supports_debug_and_validates_source_path() -> None:
    script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "Test-Path $Source" in script
    assert '"/Od", "/Zi"' in script
    assert '"/O2"' in script
    assert "$OldErrorActionPreference" in script
