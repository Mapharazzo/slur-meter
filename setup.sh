#!/bin/sh
# Quickstart: install deps, build the UI, and print how to run the API.
set -e

cd "$(dirname "$0")"

# Install uv if missing
if ! command -v uv >/dev/null 2>&1; then
    echo "📦 Installing uv…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Re-source PATH for this script
    export PATH="$HOME/.local/bin:$PATH"
fi

# Create venv + install deps (locked)
echo "🔧 Creating venv and installing deps…"
uv venv --python 3.11
uv pip install -e ".[test]"

# Build React UI (served by the API from webui/dist)
echo "🎨 Installing and building the React UI…"
npm --prefix webui ci
npm --prefix webui run build

# Install Playwright browsers (only needed for TikTok/browser publishing)
echo "🌐 Installing Playwright browser…"
uv run playwright install chromium || true

echo ""
echo "✅ Setup complete!"
echo ""
echo "Configure credentials by copying .env.example to .env, then start the API:"
echo "  .venv/bin/python -m uvicorn api.main:app --host 0.0.0.0 --port 8001"
echo ""
echo "Then open http://localhost:8001"
