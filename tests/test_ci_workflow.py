from __future__ import annotations

from pathlib import Path


WORKFLOW = Path(".github/workflows/build.yml")


def test_release_tag_includes_run_attempt_to_avoid_rerun_collision() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "github.run_attempt" in workflow
    assert 'v0.1.${{ github.run_number }}.${{ github.run_attempt }}' in workflow


def test_ffmpeg_download_uses_retry_loop() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "for ($attempt = 1; $attempt -le 3; $attempt++)" in workflow
    assert "Start-Sleep -Seconds" in workflow
