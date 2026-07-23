# syntax=docker/dockerfile:1

# ─── Stage 1: build the React UI ────────────────────────────────────────────
FROM node:20-slim AS ui-builder
WORKDIR /ui

# Install UI deps from the lockfile for reproducible builds.
COPY webui/package.json webui/package-lock.json ./
RUN npm ci

# Build the production bundle into /ui/dist.
COPY webui/ ./
RUN npm run build

# ─── Stage 2: Python runtime that serves the operations control panel ───────
FROM python:3.11-slim AS runtime

# System deps: FFmpeg for encoding, a base font family for Matplotlib rendering.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg fonts-liberation unzip wget \
    && rm -rf /var/lib/apt/lists/*

# Montserrat font used by the rage-chart renderer.
RUN wget -q -O /tmp/montserrat.zip \
        https://github.com/JulietaUla/Montserrat/releases/download/v7.222/Montserrat-v7.222.zip \
    && mkdir -p /usr/share/fonts/truetype/montserrat \
    && unzip -q /tmp/montserrat.zip -d /usr/share/fonts/truetype/montserrat/ \
    && fc-cache -f \
    && rm /tmp/montserrat.zip

# Install uv (used to install locked Python dependencies).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install locked dependencies into /app/.venv. The application runs from the
# source tree via PYTHONPATH, so the project itself is not installed as a
# package — only its resolved dependencies come from the lock.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

# Copy application source.
COPY api ./api
COPY src ./src
COPY main.py config.yaml ./
RUN uv sync --frozen --no-dev

# Bring in the pre-built UI; the API serves it from /app/webui/dist.
COPY --from=ui-builder /ui/dist ./webui/dist

# Create runtime data directories and run as an unprivileged user.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data /app/results /app/output /app/tmp \
    && chown -R app:app /app
USER app

EXPOSE 8001

# Liveness against the public health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/api/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8001"]
