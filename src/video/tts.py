"""TTS helpers — generates voiceover audio for intro/outro."""

import asyncio
from pathlib import Path

import edge_tts


async def generate_tts_async(
    text: str,
    output_path: str | Path,
    voice: str = "en-US-GuyNeural",
    rate: str = "+0%",
):
    """Generate speech using free Microsoft Edge TTS."""
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    await communicate.save(str(output_path))


def generate_tts(
    text: str,
    output_path: str | Path,
    voice: str = "en-US-GuyNeural",
    speed: float = 1.0,
):
    """Synchronous wrapper — only safe to call outside a running event loop (CLI)."""
    rate = f"{int((speed - 1.0) * 100):+d}%"
    asyncio.run(generate_tts_async(text, output_path, voice, rate))


async def build_intro_audio_async(movie_title: str, movie_year: str,
                                  output_path: str | Path, config: dict) -> Path:
    tts_cfg = config.get("tts", {})
    voice = tts_cfg.get("voice", "en-US-GuyNeural")
    speed = tts_cfg.get("speed", 1.0)
    rate = f"{int((speed - 1.0) * 100):+d}%"
    text = (
        f"How toxic is {movie_title} from {movie_year}? "
        "Let's check the Daily Slur Meter!"
    )
    await generate_tts_async(text, output_path, voice, rate)
    return Path(output_path)


def build_intro_audio(movie_title: str, movie_year: str,
                      output_path: str | Path, config: dict) -> Path:
    """Synchronous entry point for CLI use."""
    tts_cfg = config.get("tts", {})
    voice = tts_cfg.get("voice", "en-US-GuyNeural")
    speed = tts_cfg.get("speed", 1.0)
    rate = f"{int((speed - 1.0) * 100):+d}%"
    text = (
        f"How toxic is {movie_title} from {movie_year}? "
        "Let's check the Daily Slur Meter!"
    )
    generate_tts(text, output_path, voice, speed)
    return Path(output_path)


def build_outro_audio(output_path: str | Path, config: dict) -> Path:
    """Synchronous entry point for CLI use."""
    tts_cfg = config.get("tts", {})
    voice = tts_cfg.get("voice", "en-US-GuyNeural")
    speed = tts_cfg.get("speed", 1.0)
    text = "What movie should we rate next? Let us know in the comments!"
    generate_tts(text, output_path, voice, speed)
    return Path(output_path)
