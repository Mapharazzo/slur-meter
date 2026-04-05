"""Audio providers — pluggable backends that produce audio files.

Each provider takes a context dict and writes a single audio file.
New backends (ElevenLabs, OpenAI TTS, etc.) are added by subclassing
``AudioProvider`` and registering via ``PROVIDER_REGISTRY``.
"""

from __future__ import annotations

import abc
import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

# Default cache directory — lives alongside other output artefacts.
_DEFAULT_CACHE_DIR = Path(".cache/audio")


def _cache_key(provider_name: str, text: str, **params: Any) -> str:
    """Deterministic hash of (provider, text, voice/model params).

    Ignores params whose value is None so that defaults don't break
    the key when they're omitted.
    """
    blob = json.dumps(
        {"p": provider_name, "t": text, **{k: v for k, v in sorted(params.items()) if v is not None}},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


class AudioProvider(abc.ABC):
    """Base class for all audio providers."""

    name: str = "base"
    # Providers that hit a remote API should set this to True so the
    # generate() wrapper checks the cache before calling _generate().
    cacheable: bool = False

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        cache_dir = self.config.get("cache_dir", str(_DEFAULT_CACHE_DIR))
        self.cache_dir = Path(cache_dir)

    def generate(self, text: str, output_path: Path, **kwargs: Any) -> Path:
        """Public entry point — checks cache, then delegates to _generate()."""
        if self.cacheable and text:
            return self._cached_generate(text, output_path, **kwargs)
        return self._generate(text, output_path, **kwargs)

    @abc.abstractmethod
    def _generate(self, text: str, output_path: Path, **kwargs: Any) -> Path:
        """Actual generation logic — subclasses implement this."""

    # ── Caching plumbing ────────────────────────

    def _cache_params(self, **kwargs: Any) -> dict[str, Any]:
        """Return the params that should be part of the cache key.

        Override in subclasses to include voice, model, speed, etc.
        """
        return {}

    def _cached_generate(self, text: str, output_path: Path, **kwargs: Any) -> Path:
        params = self._cache_params(**kwargs)
        key = _cache_key(self.name, text, **params)
        ext = output_path.suffix or ".mp3"
        cached = self.cache_dir / f"{key}{ext}"

        if cached.exists():
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cached, output_path)
            return output_path

        # Generate, then store in cache
        self._generate(text, output_path, **kwargs)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(output_path, cached)
        return output_path

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}>"


# ── Concrete providers ──────────────────────────


class EdgeTTSProvider(AudioProvider):
    """Free Microsoft Edge TTS — good quality, zero cost."""

    name = "edge"
    cacheable = True

    def _cache_params(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "voice": kwargs.get("voice") or self.config.get("voice", "en-US-GuyNeural"),
            "speed": kwargs.get("speed") or self.config.get("speed", 1.0),
        }

    def _generate(self, text: str, output_path: Path, **kwargs: Any) -> Path:
        import edge_tts

        voice = kwargs.get("voice") or self.config.get("voice", "en-US-GuyNeural")
        speed = kwargs.get("speed") or self.config.get("speed", 1.0)
        rate = f"{int((speed - 1.0) * 100):+d}%"

        async def _gen() -> None:
            comm = edge_tts.Communicate(text, voice, rate=rate)
            await comm.save(str(output_path))

        asyncio.run(_gen())
        return output_path


class ElevenLabsProvider(AudioProvider):
    """ElevenLabs TTS — high quality, paid API.

    Config keys:
        voice_id    — ElevenLabs voice ID (required)
        model_id    — model to use (default: eleven_multilingual_v2)
        api_key_env — env var name holding the API key (default: ELEVENLABS_API_KEY)
    """

    name = "elevenlabs"
    cacheable = True

    def _cache_params(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "voice_id": kwargs.get("voice_id") or self.config.get("voice_id"),
            "model_id": kwargs.get("model_id") or self.config.get("model_id", "eleven_multilingual_v2"),
        }

    def _generate(self, text: str, output_path: Path, **kwargs: Any) -> Path:
        import os
        import requests

        api_key_env = kwargs.get("api_key_env") or self.config.get("api_key_env", "ELEVENLABS_API_KEY")
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise RuntimeError(
                f"ElevenLabsProvider: set ${api_key_env} in your environment"
            )

        voice_id = kwargs.get("voice_id") or self.config.get("voice_id")
        if not voice_id:
            raise ValueError("ElevenLabsProvider: voice_id is required")

        model_id = kwargs.get("model_id") or self.config.get("model_id", "eleven_multilingual_v2")

        resp = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
            },
            json={
                "text": text,
                "model_id": model_id,
                "voice_settings": {
                    "stability": self.config.get("stability", 0.5),
                    "similarity_boost": self.config.get("similarity_boost", 0.75),
                },
            },
            timeout=30,
        )
        resp.raise_for_status()

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(resp.content)
        return output_path


class FileProvider(AudioProvider):
    """Serves a pre-existing audio file (music loops, SFX, etc.)."""

    name = "file"

    def _generate(self, text: str, output_path: Path, **kwargs: Any) -> Path:
        src = Path(kwargs.get("path") or self.config.get("path", ""))
        if not src.exists():
            raise FileNotFoundError(f"FileProvider: source not found: {src}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, output_path)
        return output_path


class SilenceProvider(AudioProvider):
    """Generate a silent audio file of a given duration (seconds).

    Useful as a fallback or placeholder when no real audio is configured.
    Requires ffmpeg.
    """

    name = "silence"

    def _generate(self, text: str, output_path: Path, **kwargs: Any) -> Path:
        import subprocess

        duration = kwargs.get("duration") or self.config.get("duration", 1.0)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
                "-t", str(duration),
                "-c:a", "aac", "-b:a", "128k",
                str(output_path),
            ],
            check=True,
            capture_output=True,
        )
        return output_path


class LyriaProvider(AudioProvider):
    """Google Lyria 3 via OpenRouter — 30-second instrumental clips.

    Uses streaming (required by OpenRouter for audio output).

    Config keys:
        api_key_env — env var holding the OpenRouter key (default: OPENROUTER_API_KEY)
        model       — OpenRouter model ID (default: google/lyria-3-pro-preview)
    """

    name = "lyria"
    cacheable = True

    def _cache_params(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "model": kwargs.get("model") or self.config.get("model", "google/lyria-3-pro-preview"),
        }

    def _cached_generate(self, text: str, output_path: Path, **kwargs: Any) -> Path:
        """Cache keyed on first 64 chars of the prompt hash."""
        params = self._cache_params(**kwargs)
        key = _cache_key(self.name, text, **params)[:64]
        cached = self.cache_dir / f"{key}.wav"

        if cached.exists():
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cached, output_path)
            return output_path

        self._generate(text, output_path, **kwargs)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(output_path, cached)
        return output_path

    def _generate(self, text: str, output_path: Path, **kwargs: Any) -> Path:
        import base64
        import os

        import requests

        api_key_env = kwargs.get("api_key_env") or self.config.get("api_key_env", "OPENROUTER_API_KEY")
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise RuntimeError(f"LyriaProvider: set ${api_key_env} in your environment")

        model = kwargs.get("model") or self.config.get("model", "google/lyria-3-pro-preview")

        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "modalities": ["audio", "text"],
                "stream": True,
                "messages": [
                    {"role": "user", "content": text},
                ],
            },
            timeout=180,
            stream=True,
        )
        resp.raise_for_status()

        audio_chunks: list[str] = []

        for line in resp.iter_lines():
            if not line:
                continue
            decoded = line.decode("utf-8")
            if not decoded.startswith("data: "):
                continue
            data_str = decoded[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                delta = chunk.get("choices", [{}])[0].get("delta", {})

                audio = delta.get("audio")
                if audio and "data" in audio:
                    audio_chunks.append(audio["data"])

                content = delta.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "audio" and "data" in part:
                            audio_chunks.append(part["data"])
            except json.JSONDecodeError:
                continue

        if not audio_chunks:
            raise RuntimeError("LyriaProvider: no audio data in streamed response")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(base64.b64decode("".join(audio_chunks)))
        return output_path


# ── Registry ────────────────────────────────────

PROVIDER_REGISTRY: dict[str, type[AudioProvider]] = {
    "edge": EdgeTTSProvider,
    "elevenlabs": ElevenLabsProvider,
    "lyria": LyriaProvider,
    "file": FileProvider,
    "silence": SilenceProvider,
}


def get_provider(name: str, config: dict[str, Any] | None = None) -> AudioProvider:
    """Instantiate a provider by name.  Raises KeyError for unknown names."""
    cls = PROVIDER_REGISTRY.get(name)
    if cls is None:
        raise KeyError(
            f"Unknown audio provider '{name}'. "
            f"Available: {', '.join(PROVIDER_REGISTRY)}"
        )
    return cls(config)
