# 📉 Daily Slur Meter — Operations Control Panel

[![CI](https://github.com/Mapharazzo/slur-meter/actions/workflows/ci.yml/badge.svg)](https://github.com/Mapharazzo/slur-meter/actions/workflows/ci.yml)

Automated pipeline that fetches movie subtitles, performs profanity/sentiment
analysis over runtime, and generates 9:16 vertical "Shorts" videos with animated
rage charts — now driven by a durable, crash-recoverable **operations control
panel** (FastAPI backend + React UI) with a thin CLI adapter for one-off renders.

## 🎬 Demo (Pulp Fiction, 1994)

| Intro | Rage Chart | Verdict |
| :---: | :---: | :---: |
| ![Intro](fixtures/demo/intro.png) | ![Plotting](fixtures/demo/plotting.png) | ![Conclusion](fixtures/demo/conclusion.png) |

## 🚀 Setup

Requires **Python 3.11+** and **Node 20+** (see `.node-version`). The project uses
[uv](https://astral.sh/uv) for Python deps and npm (locked) for the UI.

```bash
git clone https://github.com/Mapharazzo/slur-meter.git
cd slur-meter

# One-shot: venv + locked deps + build the UI
./setup.sh
#   ...or the equivalent:
make install
```

Then copy `.env.example` to `.env` and fill in credentials and the operations
API token (see [Configuration](#️-configuration)).

## 🔌 Running the control panel

```bash
# Serve the API (also serves the built UI from webui/dist) on :8001
make server
# → open http://localhost:8001
```

For UI development with hot-reload, run the API and Vite side by side:

```bash
make server   # API  on :8001
make ui       # Vite on :5173  (proxies /api → :8001)
```

### CLI (one-off renders)

The CLI is a thin adapter over the same durable pipeline:

```bash
uv run main.py --imdb tt0110912            # fetch + analyze
uv run main.py --imdb tt0110912 --render   # fetch + analyze + render
uv run main.py --render-only fixtures/pulp_fiction/analysis.json
```

## ⚙️ Configuration

- **`config.yaml`** — slur dictionary, colors, fonts, timing, provider toggles.
- **`.env`** — credentials and runtime settings (copy from `.env.example`). Key
  operations settings:
  - `ADMIN_API_TOKEN` — bearer token required by every `/api` route. If unset,
    the API is closed unless `ALLOW_LOCAL_DEVELOPMENT_AUTH=true`.
  - `ALLOWED_ORIGINS` — comma-separated CORS allow-list for the browser UI.
  - `RETRY_DELAYS`, `SUBTITLE_COVERAGE_THRESHOLD`, `SUBTITLE_CANDIDATES_PER_CYCLE`
    — retry/subtitle-selection tuning.
  - `DATA_DIR`, `RESULTS_DIR`, `OUTPUT_DIR` — durable roots (persisted as Docker
    volumes).

Full operator reference: **[docs/operations-control-panel.md](docs/operations-control-panel.md)**.

## 🐳 Docker

A multi-stage build compiles the UI, installs locked Python deps, and runs the
API as a **non-root** user on port 8001:

```bash
docker compose up --build
# → http://localhost:8001 (health: /api/health)
```

Runtime state persists in the `data`, `results`, and `output` volumes. Credentials
are never copied into the image — they are injected at runtime via `.env`.

## 🧪 Testing & verification

```bash
make verify          # ruff + full Python tests + UI tests + UI build + diff check
make test            # full Python test suite
make test-fast       # unit tests only
npm --prefix webui test -- --run
```

CI (`.github/workflows/ci.yml`) runs the full Python suite and the UI
tests + build on every push/PR.

## 🏗️ Structure

```
├── config.yaml           # Master config (slur dict, colors, fonts)
├── main.py               # Thin CLI adapter over the durable pipeline
├── api/                  # FastAPI operations backend (auth, store, dispatcher, routes)
│   ├── main.py           #   app factory + routes (serves the UI + /api)
│   ├── database.py       #   versioned SQLite operational store
│   ├── dispatcher.py     #   leased, crash-recoverable job dispatcher
│   └── pipeline.py       #   durable resumable stage runner
├── src/
│   ├── data/             # OpenSubtitles API + download
│   ├── analysis/         # SRT parsing + profanity engine
│   ├── video/            # Plotting + compositing
│   └── publishing/       # Social upload handlers
├── webui/                # React operations UI (Vite + Tailwind)
├── docs/                 # Operator guide + codebase review ledger
├── fixtures/             # Demo assets (Pulp Fiction)
├── tests/                # Unit + integration + scenario tests
├── data/                 # (Gitignored) operational database
├── results/ · output/    # (Gitignored) artifacts / rendered videos
├── Dockerfile · docker-compose.yml · Makefile · setup.sh
```
