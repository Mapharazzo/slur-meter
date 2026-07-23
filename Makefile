# Daily Slur Meter — Operations Control Panel Makefile
# ─────────────────────────────────────────────

SHELL   := /bin/bash
VENV    := .venv
PYTHON  := $(VENV)/bin/python
UV      := uv
PORT    ?= 8001
HOST    ?= 0.0.0.0

.PHONY: help install install-deps ui-install verify test test-fast lint fix format \
        typecheck build server ui dev kill restart render preview clean

# ── Help ────────────────────────────────────
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ── Install ─────────────────────────────────
install: ## Create venv + install all deps (uv + npm) and build the UI
	$(UV) venv --python 3.11
	$(VENV)/bin/uv pip install -e ".[test]"
	npm --prefix webui ci
	npm --prefix webui run build
	@echo "✅ Ready — run: make verify"

install-deps: ## (Re)install only the Python deps
	$(VENV)/bin/uv pip install -e ".[test]"

ui-install: ## (Re)install only the UI deps from the lockfile
	npm --prefix webui ci

# ── Verify (full gate) ──────────────────────
verify: ## Full gate: ruff + all Python tests + UI tests + UI build + diff check
	$(PYTHON) -m ruff check src api tests
	$(PYTHON) -m pytest tests
	npm --prefix webui test -- --run
	npm --prefix webui run build
	git diff --check
	@echo "✅ verify passed"

# ── Tests ──────────────────────────────────
test: ## Run the full Python test suite
	$(PYTHON) -m pytest tests -v --tb=short

test-fast: ## Fast subset only (unit tests)
	$(PYTHON) -m pytest tests/unit -v --tb=short

# ── Lint / Format ──────────────────────────
lint: ## Ruff lint check
	$(PYTHON) -m ruff check src api tests

fix: ## Ruff lint auto-fix
	$(PYTHON) -m ruff check --fix src api tests

format: ## Ruff format
	$(PYTHON) -m ruff format src api tests

typecheck: ## mypy type check (optional)
	$(PYTHON) -m mypy src api

# ── Build ──────────────────────────────────
build: ## Build the production UI bundle
	npm --prefix webui run build

# ── Dev Server ──────────────────────────────
server: ## Start FastAPI API server (hot-reload, port $(PORT))
	$(PYTHON) -m uvicorn api.main:app --host $(HOST) --port $(PORT) --reload

ui: ## Start Vite dev server (hot-reload, port 5173)
	npm --prefix webui run dev

dev: ## How to run API + UI side-by-side
	@echo "Run in two separate terminals:"
	@echo "  make server   → API on :$(PORT)"
	@echo "  make ui       → Vite on :5173 (proxies /api → :$(PORT))"

kill: ## Kill process on port $(PORT)
	fuser -k -9 $(PORT)/tcp 2>/dev/null || echo "Nothing on $(PORT)"

restart: kill ## Kill + start server on port $(PORT)
	@sleep 1 && $(MAKE) server

# ── Video ───────────────────────────────────
JOB    ?= pulp_fiction
SEG    ?= all
FRAMES ?=

render: ## Render full video  JOB=pulp_fiction  (uses fixtures/<JOB>/analysis.json or results/<JOB>.json)
	@ANALYSIS=$$(test -f results/$(JOB).json && echo results/$(JOB).json || echo fixtures/$(JOB)/analysis.json); \
	  test -f $$ANALYSIS || (echo "No analysis found for JOB=$(JOB)"; exit 1); \
	  PYTHONPATH=. uv run python main.py --render-only $$ANALYSIS

preview: ## Render preview frames  JOB=<id>  SEG=all|intro_hold|intro_transition|graph|verdict  FRAMES=0,30,59
	PYTHONPATH=. uv run python scripts/dev_frames.py \
	  --job $(JOB) --segment $(SEG) --frames "$(FRAMES)"

# ── Clean ──────────────────────────────────
clean: ## Remove build artefacts + caches
	rm -rf __pycache__ */__pycache__ */*/__pycache__
	rm -rf .pytest_cache reports/ htmlcov/
	rm -rf output/ tmp/
	rm -rf webui/dist/ webui/node_modules/
	find . -name "*.pyc" -delete
	@echo "🧹 Cleaned"
