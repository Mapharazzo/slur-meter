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
        lint fix clean kill restart dev

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
	PYTHONPATH=. uv run pytest tests/ \
	  -v --tb=short \
	  --ignore=tests/integration 2>/dev/null || \
	PYTHONPATH=. uv run pytest tests/ \
	  -v --tb=short \
	  -m "not slow"

test-fast: ## Fast subset only (unit, no I/O, no API)
	PYTHONPATH=. uv run pytest tests/unit \
	  -v --tb=short

test-ci: ## CI mode — junit xml + coverage
	PYTHONPATH=. uv run pytest tests/ \
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
	PYTHONPATH=. $(UV) run uvicorn api.main:app --host $(HOST) --port $(PORT) --reload

kill: ## Kill process on port $(PORT)
	fuser -k -9 $(PORT)/tcp 2>/dev/null || echo "Nothing on $(PORT)"

restart: kill ## Kill + start server on port $(PORT)
	@sleep 1 && $(MAKE) server

dev: server  ## Alias for 'make server'

# ── Lint / Fix ─────────────────────────────
lint: ## Ruff lint check
	$(ACTIVATE) && ruff check src/ api/ tests/

lint-fix: ## Ruff lint auto-fix
	$(ACTIVATE) && ruff check --fix src/ api/ tests/

format: ## Ruff format
	$(ACTIVATE) && ruff format src/ api/ tests/

typecheck: ## mypy type check (optional)
	$(ACTIVATE) && mypy src/ api/

# ── Clean ──────────────────────────────────
clean: ## Remove build artefacts + caches
	rm -rf __pycache__ */__pycache__ */*/__pycache__
	rm -rf .pytest_cache reports/ htmlcov/
	rm -rf output/ tmp/
	rm -rf webui/dist/ webui/node_modules/
	find . -name "*.pyc" -delete
	@echo "🧹 Cleaned"