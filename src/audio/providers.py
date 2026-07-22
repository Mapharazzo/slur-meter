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
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
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
        {
            "p": provider_name,
            "t": text,
            **{k: v for k, v in sorted(params.items()) if v is not None},
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


class AudioProvider(abc.ABC):
    """Base class for all audio providers."""

    name: str = "base"
    # Providers that hit a remote API should set this to True so the
    # generate() wrapper checks the cache before calling _generate().
    cacheable: bool = False

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        popen: Callable[..., Any] | None = None,
        process_timeout: float = 30.0,
    ):
        if process_timeout <= 0:
            raise ValueError("Audio provider process timeout must be positive")
        self.config = config or {}
        cache_dir = self.config.get("cache_dir", str(_DEFAULT_CACHE_DIR))
        self.cache_dir = Path(cache_dir)
        self._popen = popen or subprocess.Popen
        self._process_timeout = float(process_timeout)

    def generate(
        self,
        text: str,
        output_path: Path,
        *,
        cancel_requested: Any = None,
        **kwargs: Any,
    ) -> Path:
        """Public entry point — checks cache, then delegates to _generate()."""
        _raise_if_cancelled(cancel_requested)
        if self.cacheable and text:
            result = self._cached_generate(
                text,
                output_path,
                cancel_requested=cancel_requested,
                **kwargs,
            )
        else:
            result = self._generate(
                text,
                output_path,
                cancel_requested=cancel_requested,
                **kwargs,
            )
        _raise_if_cancelled(cancel_requested)
        return result

    @abc.abstractmethod
    def _generate(self, text: str, output_path: Path, **kwargs: Any) -> Path:
        """Actual generation logic — subclasses implement this."""

    # ── Caching plumbing ────────────────────────

    def _cache_params(self, **kwargs: Any) -> dict[str, Any]:
        """Return the params that should be part of the cache key.

        Override in subclasses to include voice, model, speed, etc.
        """
        return {
            key: value
            for key, value in self.config.items()
            if key not in {"cache_dir", "api_key_env"}
        }

    def _cached_generate(
        self,
        text: str,
        output_path: Path,
        *,
        cancel_requested: Any = None,
        **kwargs: Any,
    ) -> Path:
        params = self._cache_params(**kwargs)
        key = _cache_key(self.name, text, **params)
        ext = output_path.suffix or ".mp3"
        cached = self.cache_dir / f"{key}{ext}"
        metadata = cached.with_suffix(f"{cached.suffix}.json")

        if self._valid_cache_entry(
            cached,
            metadata,
            cancel_requested=cancel_requested,
        ):
            _raise_if_cancelled(cancel_requested)
            _atomic_copy(cached, output_path)
            _raise_if_cancelled(cancel_requested)
            return output_path
        cached.unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        staged = _partial_path(output_path)
        try:
            self._generate(
                text,
                staged,
                cancel_requested=cancel_requested,
                **kwargs,
            )
            _raise_if_cancelled(cancel_requested)
            if not staged.is_file() or staged.stat().st_size <= 0:
                raise RuntimeError(f"{self.__class__.__name__} produced empty audio")
            if not self._validate_audio(
                staged,
                cancel_requested=cancel_requested,
            ):
                raise RuntimeError(f"{self.__class__.__name__} produced invalid audio")
            _raise_if_cancelled(cancel_requested)
            os.replace(staged, output_path)
            _atomic_copy(output_path, cached)
            _atomic_json(
                metadata,
                {
                    "sha256": _hash_file(cached),
                    "size": cached.stat().st_size,
                },
            )
        finally:
            staged.unlink(missing_ok=True)
        return output_path

    def _valid_cache_entry(
        self,
        cached: Path,
        metadata: Path,
        *,
        cancel_requested: Any = None,
    ) -> bool:
        try:
            if cached.is_symlink() or metadata.is_symlink():
                return False
            facts = json.loads(metadata.read_text(encoding="utf-8"))
            return bool(
                cached.is_file()
                and cached.stat().st_size > 0
                and facts.get("size") == cached.stat().st_size
                and facts.get("sha256") == _hash_file(cached)
                and self._validate_audio(
                    cached,
                    cancel_requested=cancel_requested,
                )
            )
        except asyncio.CancelledError:
            raise
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def _validate_audio(self, path: Path, *, cancel_requested: Any = None) -> bool:
        """Fail closed when an available ffprobe cannot validate cached audio."""
        executable = shutil.which("ffprobe")
        if executable is None:
            return path.is_file() and path.stat().st_size > 0
        try:
            stdout = self._run_process(
                [
                    executable,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                cancel_requested=cancel_requested,
                timeout=15.0,
                capture_stdout=True,
            )
            return float(stdout.strip()) > 0
        except asyncio.CancelledError:
            raise
        except (OSError, TypeError, ValueError, subprocess.SubprocessError):
            return False

    def _run_process(
        self,
        command: list[str],
        *,
        cancel_requested: Any = None,
        timeout: float | None = None,
        capture_stdout: bool = False,
    ) -> str:
        process = self._popen(
            command,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        limit = self._process_timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + limit
        while process.poll() is None:
            if cancel_requested is not None and cancel_requested():
                self._stop_process(process)
                raise asyncio.CancelledError("Audio provider process was cancelled")
            if time.monotonic() >= deadline:
                self._stop_process(process)
                raise subprocess.TimeoutExpired(command, limit)
            time.sleep(0.02)
        returncode = process.wait()
        if returncode != 0:
            raise subprocess.CalledProcessError(returncode, command)
        if capture_stdout and process.stdout is not None:
            return str(process.stdout.read())
        return ""

    @staticmethod
    def _stop_process(process: Any) -> None:
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=2)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}>"


# ── Concrete providers ──────────────────────────


class EdgeTTSProvider(AudioProvider):
    """Free Microsoft Edge TTS — good quality, zero cost."""

    name = "edge"
    cacheable = True

    def _cache_params(self, **kwargs: Any) -> dict[str, Any]:
        return {
            **super()._cache_params(**kwargs),
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
            **super()._cache_params(**kwargs),
            "voice_id": kwargs.get("voice_id") or self.config.get("voice_id"),
            "model_id": kwargs.get("model_id")
            or self.config.get("model_id", "eleven_multilingual_v2"),
            "stability": kwargs.get("stability", self.config.get("stability", 0.5)),
            "similarity_boost": kwargs.get(
                "similarity_boost", self.config.get("similarity_boost", 0.75)
            ),
        }

    def _generate(self, text: str, output_path: Path, **kwargs: Any) -> Path:
        import requests
        from dotenv import load_dotenv

        load_dotenv(override=False)

        api_key_env = kwargs.get("api_key_env") or self.config.get(
            "api_key_env", "ELEVENLABS_API_KEY"
        )
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise RuntimeError(
                f"ElevenLabsProvider: set ${api_key_env} in your environment"
            )

        voice_id = kwargs.get("voice_id") or self.config.get("voice_id")
        if not voice_id:
            raise ValueError("ElevenLabsProvider: voice_id is required")

        model_id = kwargs.get("model_id") or self.config.get(
            "model_id", "eleven_multilingual_v2"
        )

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
                    "stability": kwargs.get(
                        "stability", self.config.get("stability", 0.5)
                    ),
                    "similarity_boost": kwargs.get(
                        "similarity_boost", self.config.get("similarity_boost", 0.75)
                    ),
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
        duration = kwargs.get("duration") or self.config.get("duration", 1.0)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        partial = _partial_path(output_path)
        try:
            self._run_process(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=r=44100:cl=stereo",
                    "-t",
                    str(duration),
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    str(partial),
                ],
                cancel_requested=kwargs.get("cancel_requested"),
            )
            _raise_if_cancelled(kwargs.get("cancel_requested"))
            if not partial.is_file() or partial.stat().st_size <= 0:
                raise RuntimeError("SilenceProvider produced empty audio")
            os.replace(partial, output_path)
            return output_path
        finally:
            partial.unlink(missing_ok=True)


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
            **super()._cache_params(**kwargs),
            "model": kwargs.get("model")
            or self.config.get("model", "google/lyria-3-pro-preview"),
        }

    def _cached_generate(self, text: str, output_path: Path, **kwargs: Any) -> Path:
        """Use the same atomic content cache as other remote providers."""
        return super()._cached_generate(text, output_path, **kwargs)

    def _generate(self, text: str, output_path: Path, **kwargs: Any) -> Path:
        import base64

        import requests

        api_key_env = kwargs.get("api_key_env") or self.config.get(
            "api_key_env", "OPENROUTER_API_KEY"
        )
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise RuntimeError(f"LyriaProvider: set ${api_key_env} in your environment")

        model = kwargs.get("model") or self.config.get(
            "model", "google/lyria-3-pro-preview"
        )

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
            _raise_if_cancelled(kwargs.get("cancel_requested"))
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
                        if (
                            isinstance(part, dict)
                            and part.get("type") == "audio"
                            and "data" in part
                        ):
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


def _partial_path(path: Path) -> Path:
    suffix = path.suffix
    return path.with_name(f".{path.stem}.{uuid.uuid4().hex}.partial{suffix}")


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = _partial_path(destination)
    try:
        shutil.copy2(source, partial)
        if partial.stat().st_size <= 0:
            raise RuntimeError("Audio cache entry is empty")
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


def _atomic_json(destination: Path, value: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = _partial_path(destination)
    try:
        with partial.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _raise_if_cancelled(cancel_requested: Any) -> None:
    if cancel_requested is not None and cancel_requested():
        raise asyncio.CancelledError("Audio generation was cancelled")
