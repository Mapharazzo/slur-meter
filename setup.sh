#!/bin/sh
# Quickstart: install uv and run the slur-meter API
set -e

# Install uv if missing
if ! command -v uv &> /dev/null; then
    echo "📦 Installing uv…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Re-source PATH for this script
    export PATH="$HOME/.local/bin:$PATH"
fi

cd "$(dirname "$0")"

# Create venv + install deps
echo "🔧 Creating venv and installing deps…"
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e .

# Build React UI
echo "🎨 Building React UI…"
cd webui
npm install
npm run build
cd ..

# Install Playwright browsers (for TikTok upload)
echo "🌐 Installing Playwright browser…"
uv run playwright install chromium || true

echo ""
echo "✅ Setup complete!"
echo ""
echo "To start the server:"
echo "  source .venv/bin/activate"
echo "  cd api && python main.py"
echo ""
echo "Then open http://localhost:8000"
