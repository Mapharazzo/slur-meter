"""Integration test: Full pipeline — fetch → analyse → render → metadata."""


import pytest

from src.analysis.engine import ProfanityEngine
from src.publishing.metadata import generate_metadata


class TestAnalysisEngineIntegration:
    """Tests the engine against real-world-style SRT data."""

    @pytest.fixture
    def config(self, test_config):
        return test_config

    def test_django_srt_pipeline(self, config, tmp_path):
        """Simulate a Django Unchained subtitle file."""
        srt = tmp_path / "django.srt"
        lines = []
        for i in range(100):
            lines.append(
                f"{i+1}\n"
                f"00:{i//60:02d}:{i%60:02d},000 --> "
                f"00:{i//60:02d}:{i%60+2:02d},000\n"
                "you fucking nigger bastard"
            )
        srt.write_text("\n\n".join(lines))

        engine = ProfanityEngine(config)
        result = engine.analyse_srt(srt)
        s = result["summary"]

        # Every line has: 1 hard, 1 soft(bastard), and f_bomb(fucking)
        assert s["total_hard"] == 100
        assert s["total_f_bombs"] == 100
        assert "TOXIC" in s["rating"] or "HAZMAT" in s["rating"]

    def test_pulp_fiction_fuel(self, config, tmp_path):
        """Simulate a cleaner movie."""
        srt = tmp_path / "clean.srt"
        lines = []
        for i in range(50):
            lines.append(
                f"{i+1}\n"
                f"00:{i//60:02d}:{i%60:02d},000 --> "
                f"00:{i//60:02d}:{i%60+2:02d},000\n"
                "royale with cheese"
            )
        srt.write_text("\n\n".join(lines))

        engine = ProfanityEngine(config)
        result = engine.analyse_srt(srt)
        s = result["summary"]

        assert s["total_hard"] == 0
        assert s["total_soft"] == 0
        assert s["total_f_bombs"] == 0

    def test_metadata_generation(self, config, tmp_path):
        srt = tmp_path / "d.srt"
        srt.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nfuck\n"
        )
        engine = ProfanityEngine(config)
        analysis = engine.analyse_srt(srt)
        summary = analysis["summary"]
        meta = generate_metadata("Pulp Fiction", summary)
        assert meta["video_title"]
        assert meta["description"]
        assert len(meta["tags"]) > 0
        assert len(meta["hashtags"]) > 0
