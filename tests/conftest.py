# Shared test fixtures

import sys
from pathlib import Path

import pytest

# Ensure project root is on sys.path so `from src.*` and `from api.*` work
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def test_config():
    return {
        "categories": {
            "hard": ["nigger", "cunt", "faggot"],
            "soft": [
                "shit", "ass", "bastard", "bitch", "damn",
                "dick", "piss", "crap", "bloody", "whore",
                "slut", "goddamn", "motherfucker",
                "motherfucking",
            ],
            "f_bombs": [
                "fuck", "fucker", "fucking", "fucked",
                "bastard", "motherfucker", "motherfucking",
            ],
        },
        "video": {
            "resolution": [1080, 1920],
            "fps": 30,
            "duration_seconds": 60,
            "colors": {
                "background": "#0d0d0d",
                "hard_slur": "#ff1744",
                "soft_slur": "#ffea00",
                "f_bomb": "#d500f9",
                "line": "#00e5ff",
                "text": "#ffffff",
                "accent": "#76ff03",
            },
        },
        "tts": {
            "engine": "edge",
            "voice": "en-US-GuyNeural",
            "speed": 1.0,
        },
    }


@pytest.fixture
def mini_srt(tmp_path: Path) -> Path:
    srt = tmp_path / "test.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:04,000\n"
        "Hey, what the fuck is going on?\n\n"
        "2\n00:00:05,000 --> 00:00:08,000\n"
        "This is bloody brilliant, mate!\n\n"
        "3\n00:00:10,000 --> 00:00:13,000\n"
        "You fucking bastard, get the fuck out!\n\n"
        "4\n00:01:05,000 --> 00:01:08,000\n"
        "Nice damn weather we're having.\n\n"
        "5\n00:02:00,000 --> 00:02:03,000\n"
        "What a shit show.\n"
    )
    return srt


@pytest.fixture
def empty_srt(tmp_path: Path) -> Path:
    srt = tmp_path / "empty.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nHello world\n"
    )
    return srt


@pytest.fixture
def api_app():
    """FastAPI app for integration testing."""
    from api.main import app
    return app
