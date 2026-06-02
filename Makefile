# ── MiceCam Makefile ─────────────────────────────────────────────────────
#
# Usage:  make [target]
#
# Quick reference:
#   make setup      First-time: download ffmpeg + sync dependencies
#   make run         Launch the GUI app
#   make test        Run unit tests
#   make check       Smoke test (no GUI)
#   make build       Package standalone .exe
#   make rebuild     Clean + build from scratch
#   make analyze     Frame-interval HTML report (latest recording)
#   make health      Terminal health check on latest recording
#   make ci          Full CI pipeline locally
#   make clean       Remove build artifacts
#   make nuke        Clean everything (build + deps + venv)
#   make help        Show this help
# ─────────────────────────────────────────────────────────────────────────

SHELL   := bash
ROOT    := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
SCRIPTS := $(ROOT)scripts/
OUTPUT  := $(ROOT)output/

# ── Phony targets (none of these produce files) ────────────────────────
.PHONY: help setup run test check build rebuild analyze health ci clean nuke \
        ffmpeg deps build-only smoke-exe

# ── Default target ─────────────────────────────────────────────────────

help:
	@grep -E '^#.*|^[a-zA-Z_-]+:.*## .*' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-16s\033[0m %s\n", $$1, $$2}' | \
		sort || { grep -E '^[a-zA-Z_-]+:' $(MAKEFILE_LIST) | sed 's/:.*//' | sort; }

# ── One-time setup ─────────────────────────────────────────────────────

ffmpeg: ## Download ffmpeg.exe (Windows) into ffmpeg/
	@echo "=== Downloading ffmpeg ==="
	uv run python "$(SCRIPTS)download_ffmpeg.py"

deps: ## Install/update Python dependencies
	@echo "=== Syncing dependencies ==="
	uv sync --group dev

setup: ffmpeg deps ## Full first-time setup: ffmpeg + dependencies
	@echo "=== Setup complete ==="

# ── Run & test ─────────────────────────────────────────────────────────

run: ## Launch the MiceCam GUI
	uv run python -m micecam

check: ## Smoke test (no GUI, prints results)
	uv run python -m micecam --check

test: ## Run pytest suite
	uv run pytest tests/ -v

test-cov: ## Run tests with coverage (requires pytest-cov)
	uv run pytest tests/ -v --cov=micecam --cov-report=term-missing

# ── Build ──────────────────────────────────────────────────────────────

build-only: ## PyInstaller build (assumes ffmpeg/ exists)
	@echo "=== Building MiceCam.exe ==="
	uv run pyinstaller --clean "$(ROOT)micecam.spec"
	@echo ""
	@echo "=== Build complete ==="
	@ls -lh "$(ROOT)dist/MiceCam.exe" 2>/dev/null || echo "ERROR: dist/MiceCam.exe not found"

build: ffmpeg build-only ## Download ffmpeg + build .exe

rebuild: ## Clean + full build from scratch
	@echo "=== Clean rebuild ==="
	$(MAKE) clean
	$(MAKE) build

smoke-exe: ## Smoke-test the packaged .exe
	@[ -f "$(ROOT)dist/MiceCam.exe" ] || { echo "ERROR: Build first (make build)"; exit 1; }
	@"$(ROOT)dist/MiceCam.exe" --check

# ── Full CI pipeline (same as GitHub Actions) ──────────────────────────

ci: test check build smoke-exe ## Run full CI pipeline locally
	@echo ""
	@echo "=== CI pipeline PASSED ==="

# ── Diagnostics ────────────────────────────────────────────────────────

LATEST_DIR = $(shell ls -dt "$(OUTPUT)"/*/*/ 2>/dev/null | head -1)

analyze: ## Frame-interval HTML report for latest recording
	@[ -n "$(LATEST_DIR)" ] || { echo "No recordings found in $(OUTPUT)"; exit 1; }
	@echo "Analyzing: $(LATEST_DIR)"
	uv run python "$(SCRIPTS)analyze_frame_intervals.py" "$(LATEST_DIR)" --last 1

health: ## Terminal health check for latest recording
	@[ -n "$(LATEST_DIR)" ] || { echo "No recordings found in $(OUTPUT)"; exit 1; }
	@echo "Checking: $(LATEST_DIR)"
	uv run python -X utf8 "$(SCRIPTS)check_recording.py" "$(LATEST_DIR)"

health-all: ## Health check for all recordings
	@[ -d "$(OUTPUT)" ] || { echo "No output/ directory"; exit 1; }
	uv run python -X utf8 "$(SCRIPTS)check_recording.py" "$(OUTPUT)"

# ── Cleanup ────────────────────────────────────────────────────────────

clean: ## Remove build artifacts (dist/, build/, .pytest_cache/)
	@echo "=== Cleaning build artifacts ==="
	rm -rf "$(ROOT)dist" "$(ROOT)build" "$(ROOT).pytest_cache" "$(ROOT)__pycache__"
	find "$(ROOT)" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
	find "$(ROOT)" -name '*.pyc' -delete 2>/dev/null || true
	@echo "=== Clean ==="

nuke: clean ## Nuke everything including venv + ffmpeg (use with care)
	@echo "=== Nuking venv and ffmpeg ==="
	rm -rf "$(ROOT).venv" "$(ROOT)ffmpeg" "$(ROOT)ffmpeg_bundled"
	@echo "=== Nuked. Run 'make setup' to rebuild. ==="
