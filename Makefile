# Daily Slur Meter — Test Suite Makefile
# ─────────────────────────────────────────────

SHELL   := /bin/bash
VENV    := .venv
ACT     := source $(VENV)/bin/activate
PYTHON  := $(VENV)/bin/python
UV      := uv
PORT    ?= 8001
HOST    ?= 0.0.0.0

.PHONY: help install test test-fast test-ci test-serve \
        lint fix clean kill restart dev render preview

# ── Help ────────────────────────────────────
help:
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'

# ── Install ─────────────────────────────────
install: ## Create venv + install all deps (uv + npm)
	$(UV) venv --python 3.11
	$(VENV)/bin/uv pip install -e ".[test]"
	npm --prefix webui --silent install
	npm --prefix webui --silent run build
	@echo "✅ Ready — run: make test"

install-deps: ## Just install deps (don't re-run setup)
	$(VENV)/bin/uv pip install -e ".[test]"

# ── Tests ──────────────────────────────────
test: ## Run ALL tests (unit + integration)
	$(ACT) && PYTHONPATH=. $(PYTHON) -m pytest tests/ \
	  -v --tb=short \
	  --ignore=tests/integration 2>/dev/null || \
	PYTHONPATH=. $(PYTHON) -m pytest tests/ \
	  -v --tb=short \
	  -m "not slow"

test-fast: ## Fast subset only (unit, no I/O, no API)
	$(ACT) && PYTHONPATH=. $(PYTHON) -m pytest tests/unit \
	  -v --tb=short

test-ci: ## CI mode — junit xml + coverage
	$(ACT) && PYTHONPATH=. $(PYTHON) -m pytest tests/ \
	  -v --tb=short \
	  --ignore=tests/integration \
	  --junitxml=reports/junit.xml \
	  --cov=src --cov=api \
	  --cov-report=html:reports/coverage \
	  --cov-report=term-missing

test-serve: ## Serve test coverage report
	cd reports/coverage && python3 -m http.server 8080

# ── Dev Server ──────────────────────────────
server: ## Start FastAPI API server (hot-reload, port 8001)
	$(ACT) && PYTHONPATH=. $(PYTHON) -m uvicorn api.main:app --host $(HOST) --port $(PORT) --reload

kill: ## Kill process on port $(PORT)
	fuser -k -9 $(PORT)/tcp 2>/dev/null || echo "Nothing on $(PORT)"

restart: kill ## Kill + start server on port $(PORT)
	@sleep 1 && $(MAKE) server

ui: ## Start Vite dev server (hot-reload, port 5173)
	npm --prefix webui run dev

dev: ## Start API + UI dev servers side-by-side (needs two terminals, or use tmux)
	@echo "Run in two separate terminals:"
	@echo "  make server   → API on :8001"
	@echo "  make ui       → Vite on :5173 (proxies /api → :8001)"

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

# ── Lint / Fix ─────────────────────────────
lint: ## Ruff lint check
	$(ACT) && ruff check src/ api/ tests/

lint-fix: ## Ruff lint auto-fix
	$(ACT) && ruff check --fix src/ api/ tests/

format: ## Ruff format
	$(ACT) && ruff format src/ api/ tests/

typecheck: ## mypy type check (optional)
	$(ACT) && mypy src/ api/

# ── Clean ──────────────────────────────────
clean: ## Remove build artefacts + caches
	rm -rf __pycache__ */__pycache__ */*/__pycache__
	rm -rf .pytest_cache reports/ htmlcov/
	rm -rf output/ tmp/
	rm -rf webui/dist/ webui/node_modules/
	find . -name "*.pyc" -delete
	@echo "🧹 Cleaned"