"""Audio layers — typed descriptors for each audio element in the mix.

A layer describes *what* audio to place, *when* it starts, and *how* it
should be mixed (volume, fade, loop).  The ``AudioMixer`` consumes a list
of layers and produces the final mixed track.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AudioLayer:
    """One audio element in the final mix."""

    # Identity
    name: str                          # e.g. "intro_tts", "bg_music", "slam_sfx_0"
    role: str                          # semantic role: "tts", "music", "sfx"

    # Source
    file: Path | None = None           # set after the provider generates it

    # Timing (seconds from video start)
    start: float = 0.0
    end: float | None = None           # None = play to natural end of file

    # Volume & mixing
    volume: float = 1.0                # linear gain (1.0 = 0 dB)
    fade_in: float = 0.0              # seconds
    fade_out: float = 0.0             # seconds

    # Looping
    loop: bool = False                 # loop the source until ``end``

    # Duck other layers while this one plays (e.g. TTS ducks music)
    duck_others: bool = False
    duck_amount: float = 0.3           # target volume for ducked layers

    # Provider settings — passed to the provider's generate()
    provider_name: str = "silence"
    provider_kwargs: dict[str, Any] = field(default_factory=dict)

    # Text for TTS providers (ignored by file/silence providers)
    text: str = ""


@dataclass
class AudioTimeline:
    """Ordered collection of layers that make up the full audio track."""

    layers: list[AudioLayer] = field(default_factory=list)
    total_duration: float = 0.0        # total video duration in seconds

    def add(self, layer: AudioLayer) -> None:
        self.layers.append(layer)

    def get_by_role(self, role: str) -> list[AudioLayer]:
        return [l for l in self.layers if l.role == role]

    def get_by_name(self, name: str) -> AudioLayer | None:
        for l in self.layers:
            if l.name == name:
                return l
        return None
