"""AudioPipeline — orchestrates providers, layers, and mixing.

Usage in the render pipeline:
    pipeline = AudioPipeline(config, audio_dir, segment_timing)
    pipeline.build_layers(movie_title, movie_year, summary)
    pipeline.generate_all()          # runs providers → audio files
    mixed = pipeline.mix(output)     # combines into one track
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .layers import AudioLayer, AudioTimeline
from .mixer import AudioMixer
from .providers import get_provider


class AudioPipeline:
    """High-level orchestrator for the audio track."""

    def __init__(
        self,
        config: dict[str, Any],
        audio_dir: Path,
        segment_timing: dict[str, dict],
    ):
        self.config = config
        self.audio_dir = Path(audio_dir)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.timing = segment_timing
        self.audio_cfg = config.get("audio", {})
        self.fps = config.get("video", {}).get("fps", 30)

        total_frames = max(
            (seg["end_frame"] for seg in segment_timing.values()), default=0
        ) + 1
        self.total_duration = total_frames / self.fps

        self.timeline = AudioTimeline(total_duration=self.total_duration)

    # ─────────────────────────────────────────────
    #  Build layers from config + video timing
    # ─────────────────────────────────────────────

    def build_layers(
        self,
        movie_title: str,
        movie_year: str,
        summary: dict,
    ) -> AudioTimeline:
        """Populate the timeline with layers defined in config."""
        self._movie_title = movie_title
        self._movie_year = movie_year
        self._summary = summary

        self._build_intro_tts(movie_title, movie_year)
        self._build_outro_tts()
        self._build_background_music()
        self._build_verdict_sfx()

        return self.timeline

    def _music_prompt(self) -> str:
        """Generate a music prompt from movie context for AI music providers."""
        rating = self._summary.get("rating", "")
        hard = self._summary.get("total_hard", 0)
        soft = self._summary.get("total_soft", 0)
        f_bombs = self._summary.get("total_f_bombs", 0)
        total = hard + soft + f_bombs

        # Pick vibe based on profanity intensity
        if hard > 10 or total > 150:
            intensity = "aggressive, dark, intense"
        elif total > 60:
            intensity = "edgy, dramatic, tense"
        elif total > 20:
            intensity = "moody, cinematic, brooding"
        else:
            intensity = "light, upbeat, playful"

        return (
            f"30 second instrumental background beat for a short-form video "
            f"about the movie {self._movie_title} ({self._movie_year}). "
            f"Style: {intensity}. "
            f"Loopable, no vocals, modern trap-cinematic hybrid, "
            f"clean mix suitable for background under voiceover."
        )

    # ── Intro TTS ───────────────────────────────

    def _build_intro_tts(self, title: str, year: str) -> None:
        cfg = self.audio_cfg.get("intro_tts", {})
        if not cfg.get("enabled", True):
            return

        provider_name = cfg.get("provider", "edge")
        provider_cfg = cfg.get("provider_config", {})

        text = cfg.get("text", "").strip()
        if not text:
            text = (
                f"How toxic is {title} from {year}? "
                "Let's check the Daily Slur Meter!"
            )

        intro_start = self.timing.get("intro_hold", {}).get("start_time", 0.0)

        layer = AudioLayer(
            name="intro_tts",
            role="tts",
            start=intro_start,
            volume=cfg.get("volume", 1.0),
            fade_in=cfg.get("fade_in", 0.0),
            fade_out=cfg.get("fade_out", 0.3),
            duck_others=True,
            duck_amount=cfg.get("duck_amount", 0.25),
            provider_name=provider_name,
            provider_kwargs=provider_cfg,
            text=text,
        )
        self.timeline.add(layer)

    # ── Outro TTS ───────────────────────────────

    def _build_outro_tts(self) -> None:
        cfg = self.audio_cfg.get("outro_tts", {})
        if not cfg.get("enabled", True):
            return

        provider_name = cfg.get("provider", "edge")
        provider_cfg = cfg.get("provider_config", {})

        text = cfg.get("text", "").strip()
        if not text:
            text = "What movie should we rate next? Let us know in the comments!"

        # Start outro TTS partway through the verdict segment so it plays
        # after the last stat slams in.
        verdict_timing = self.timing.get("verdict", {})
        verdict_start = verdict_timing.get("start_time", self.total_duration - 9.0)
        # Offset into verdict (after all slams land, ~4s in)
        outro_offset = cfg.get("start_offset", 4.0)

        layer = AudioLayer(
            name="outro_tts",
            role="tts",
            start=verdict_start + outro_offset,
            volume=cfg.get("volume", 1.0),
            fade_in=cfg.get("fade_in", 0.0),
            fade_out=cfg.get("fade_out", 0.5),
            duck_others=True,
            duck_amount=cfg.get("duck_amount", 0.25),
            provider_name=provider_name,
            provider_kwargs=provider_cfg,
            text=text,
        )
        self.timeline.add(layer)

    # ── Background music ────────────────────────

    def _build_background_music(self) -> None:
        cfg = self.audio_cfg.get("background_music", {})
        if not cfg.get("enabled", False):
            return

        provider_name = cfg.get("provider", "file")
        provider_cfg = cfg.get("provider_config", {})

        # For generative providers (lyria), build a prompt from movie context.
        # For file provider this is ignored.
        text = cfg.get("prompt", "").strip()
        if not text and provider_name in ("lyria",):
            text = self._music_prompt()

        layer = AudioLayer(
            name="bg_music",
            role="music",
            start=0.0,
            end=self.total_duration,
            volume=cfg.get("volume", 0.18),
            fade_in=cfg.get("fade_in", 1.0),
            fade_out=cfg.get("fade_out", 2.0),
            loop=True,
            provider_name=provider_name,
            provider_kwargs=provider_cfg,
            text=text,
        )
        self.timeline.add(layer)

    # ── Verdict SFX ─────────────────────────────

    def _build_verdict_sfx(self) -> None:
        cfg = self.audio_cfg.get("verdict_sfx", {})
        if not cfg.get("enabled", False):
            return

        from src.video.compositor import VideoCompositor

        verdict_start = self.timing.get("verdict", {}).get("start_time", 0.0)
        impacts = VideoCompositor.verdict_impact_times(
            fps=self.fps, verdict_start_offset=verdict_start
        )

        provider_name = cfg.get("provider", "file")
        provider_cfg = cfg.get("provider_config", {})
        slam_volume = cfg.get("slam_volume", 0.7)

        # Rating reveal can have a different SFX
        rating_cfg = cfg.get("rating", {})
        rating_provider = rating_cfg.get("provider", provider_name)
        rating_provider_cfg = rating_cfg.get("provider_config", provider_cfg)
        rating_volume = rating_cfg.get("volume", 1.0)

        for impact in impacts:
            is_rating = impact["label"] == "rating"
            pname = rating_provider if is_rating else provider_name
            pcfg = rating_provider_cfg if is_rating else provider_cfg
            vol = rating_volume if is_rating else slam_volume

            layer = AudioLayer(
                name=f"sfx_{impact['label']}",
                role="sfx",
                start=impact["impact_time"],
                volume=vol,
                provider_name=pname,
                provider_kwargs=pcfg,
                text="",
            )
            self.timeline.add(layer)

    # ─────────────────────────────────────────────
    #  Generate audio files via providers
    # ─────────────────────────────────────────────

    def generate_all(self) -> None:
        """Run each layer's provider to produce its audio file."""
        for layer in self.timeline.layers:
            output_file = self.audio_dir / f"{layer.name}.mp3"
            provider = get_provider(layer.provider_name, layer.provider_kwargs)
            provider.generate(
                text=layer.text,
                output_path=output_file,
                **layer.provider_kwargs,
            )
            layer.file = output_file

    # ─────────────────────────────────────────────
    #  Mix down to final audio track
    # ─────────────────────────────────────────────

    def mix(self, output_path: Path) -> Path:
        """Combine all generated layers into one audio file."""
        mixer = AudioMixer()
        return mixer.mix(self.timeline, output_path)
