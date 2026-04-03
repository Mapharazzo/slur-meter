FROM python:3.11-slim

# Install system deps for FFmpeg, OpenCV, MoviePy, Matplotlib
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg fonts-liberation unzip wget curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Download Montserrat font (free Google Font)
RUN wget -q -O /tmp/montserrat.zip \
    https://github.com/JulietaUla/Montserrat/releases/download/v7.222/Montserrat-v7.222.zip \
    && mkdir -p /usr/share/fonts/truetype/montserrat \
    && unzip -q /tmp/montserrat.zip -d /usr/share/fonts/truetype/montserrat/ \
    && fc-cache -f \
    && rm /tmp/montserrat.zip

WORKDIR /app

# Pre-install deps separately to cache layer
COPY pyproject.toml .
RUN uv pip install -e ".[test]" --system

# Copy project source
COPY . .

ENTRYPOINT ["python", "main.py"]
