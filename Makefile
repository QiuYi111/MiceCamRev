# ── MiceCam Makefile  (cross-platform: Windows / macOS / Linux) ───────
ROOT    := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
SCRIPTS := $(ROOT)scripts/
OUTPUT  := $(ROOT)output/

.PHONY: help setup run test check build rebuild analyze health ci clean nuke \
        ffmpeg deps build-only smoke-exe health-all

# ── help ───────────────────────────────────────────────────────────────
help:
	@echo ==================================================
	@echo   MiceCam -- Quick Reference
	@echo ==================================================
	@echo   make setup        First-time: ffmpeg + deps
	@echo   make run           Launch GUI
	@echo   make test          Run pytest suite
	@echo   make check         Smoke test - no GUI
	@echo   make build         Package standalone .exe
	@echo   make rebuild       Clean + full build
	@echo   make analyze       Frame-interval HTML report
	@echo   make health        Terminal health check
	@echo   make health-all    Health check for ALL recordings
	@echo   make ci            Full CI pipeline locally
	@echo   make clean         Remove build artifacts
	@echo   make nuke          Nuke venv + ffmpeg + build

# ── setup ──────────────────────────────────────────────────────────────
ffmpeg:
	@echo === Downloading ffmpeg ===
	uv run python "$(SCRIPTS)download_ffmpeg.py"

deps:
	@echo === Syncing dependencies ===
	uv sync --group dev

setup: ffmpeg deps
	@echo === Setup complete ===

# ── run & test ─────────────────────────────────────────────────────────
run:
	uv run python -m micecam

check:
	uv run python -m micecam --check

test:
	uv run pytest tests/ -v

# ── build ──────────────────────────────────────────────────────────────
build-only:
	@echo === Building MiceCam.exe ===
	uv run pyinstaller --clean "$(ROOT)micecam.spec"
	@echo === Build complete ===

build: ffmpeg build-only

rebuild:
	$(MAKE) clean
	$(MAKE) build

smoke-exe:
	"$(ROOT)dist/MiceCam.exe" --check

# ── ci ─────────────────────────────────────────────────────────────────
ci: test check build smoke-exe
	@echo === CI pipeline PASSED ===

# ── diagnostics ────────────────────────────────────────────────────────
LATEST := $(shell uv run python "$(SCRIPTS)_find_latest.py")

analyze:
	@uv run python "$(SCRIPTS)analyze_frame_intervals.py" "$(LATEST)" --last 1

health:
	@uv run python -X utf8 "$(SCRIPTS)check_recording.py" "$(LATEST)"

health-all:
	@uv run python -X utf8 "$(SCRIPTS)check_recording.py" "$(OUTPUT)"

# ── cleanup ────────────────────────────────────────────────────────────
clean:
	@echo === Cleaning build artifacts ===
	uv run python -c "import shutil,pathlib; r=pathlib.Path('$(ROOT)'); [shutil.rmtree(r/d,ignore_errors=1) for d in ['dist','build','.pytest_cache']]; [shutil.rmtree(p,ignore_errors=1) for p in r.rglob('__pycache__')]; [p.unlink(missing_ok=1) for p in r.rglob('*.pyc')]; print('=== Clean ===')"

nuke: clean
	@echo === Nuking venv and ffmpeg ===
	uv run python -c "import shutil,pathlib; r=pathlib.Path('$(ROOT)'); [shutil.rmtree(r/d,ignore_errors=1) for d in ['.venv','ffmpeg','ffmpeg_bundled']]; print('=== Nuked. Run: make setup ===')"
