"""Unit tests for analysis engine — isolated profanity detection."""

from pathlib import Path

import pytest

from src.analysis.engine import ProfanityEngine


@pytest.fixture
def config():
    return {
        "categories": {
            "hard": ["nigger", "cunt", "faggot"],
            "soft": [
                "shit", "ass", "bastard", "bitch", "damn",
                "dick", "piss", "crap", "bloody", "whore", "slut",
                "goddamn", "motherfucker", "motherfucking",
            ],
            "f_bombs": [
                "fuck", "fucker", "fucking", "fucked",
                "motherfucker", "motherfucking",
            ],
        }
    }


@pytest.fixture
def engine(config):
    return ProfanityEngine(config)


def _analyse(engine, tmp_path: Path, text: str) -> dict:
    srt = tmp_path / "t.srt"
    srt.write_text(text)
    return engine.analyse_srt(srt)


# ─── Counting correctness ────────────────


class TestFBombs:
    def test_single(self, engine, tmp_path):
        r = _analyse(engine, tmp_path,
                     "1\n00:00:01,000 --> 00:00:02,000\nwhat the fuck\n")
        assert r["summary"]["total_f_bombs"] == 1

    def test_three_on_same_line(self, engine, tmp_path):
        r = _analyse(engine, tmp_path,
                     "1\n00:00:01,000 --> 00:00:02,000\n"
                     "fuck you fucking fucker\n")
        assert r["summary"]["total_f_bombs"] == 3

    def test_variants(self, engine, tmp_path):
        """fuck, fucked, and fucker all counted."""
        r = _analyse(engine, tmp_path,
                     "1\n00:00:01,000 --> 00:00:02,000\nfuck\n"
                     "2\n00:00:03,000 --> 00:00:04,000\nfucked\n"
                     "3\n00:00:05,000 --> 00:00:06,000\nfucker\n")
        assert r["summary"]["total_f_bombs"] == 3


class TestSoftSlurs:
    def test_bloody(self, engine, tmp_path):
        r = _analyse(engine, tmp_path,
                     "1\n00:00:01,000 --> 00:00:02,000\nbloody hell\n")
        assert r["summary"]["total_soft"] == 1

    def test_shit(self, engine, tmp_path):
        r = _analyse(engine, tmp_path,
                     "1\n00:00:01,000 --> 00:00:02,000\nwhat a shit show\n")
        assert r["summary"]["total_soft"] == 1


class TestHardSlurs:
    def test_detected(self, engine, tmp_path):
        r = _analyse(engine, tmp_path,
                     "1\n00:00:01,000 --> 00:00:02,000\nnigger\n")
        assert r["summary"]["total_hard"] == 1

    def test_word_boundary(self, engine, tmp_path):
        """'cuntface' should NOT match 'cunt' — word boundary regex."""
        r = _analyse(engine, tmp_path,
                     "1\n00:00:01,000 --> 00:00:02,000\ncuntface\n")
        assert r["summary"]["total_hard"] == 0


# ─── Edge cases ──────────────────────────

class TestEdgeCases:
    def test_clean(self, engine, tmp_path):
        r = _analyse(engine, tmp_path,
                     "1\n00:00:01,000 --> 00:00:02,000\nhello\n")
        s = r["summary"]
        assert s["total_hard"] == 0
        assert s["total_soft"] == 0
        assert s["total_f_bombs"] == 0
        assert "QUIET" in s["rating"] or "ANGEL" in s["rating"]

    def test_case_insensitive(self, engine, tmp_path):
        r = _analyse(engine, tmp_path,
                     "1\n00:00:01,000 --> 00:00:02,000\nWHAT THE FUCK\n")
        assert r["summary"]["total_f_bombs"] == 1

    def test_no_false_positive(self, engine, tmp_path):
        """'ship' != 'shit'. Word boundary."""
        r = _analyse(engine, tmp_path,
                     "1\n00:00:01,000 --> 00:00:02,000\nthe ship sails\n")
        assert r["summary"]["total_soft"] == 0

    def test_newlines_in_sub(self, engine, tmp_path):
        r = _analyse(engine, tmp_path,
                     "1\n00:00:01,000 --> 00:00:02,000\nhello\nfuck you\n")
        assert r["summary"]["total_f_bombs"] == 1


# ─── Time binning ────────────────────────

class TestBinning:
    def test_minute_buckets(self, engine, tmp_path):
        r = _analyse(engine, tmp_path,
                     "1\n00:00:01,000 --> 00:00:02,000\nfuck\n"
                     "2\n00:00:30,000 --> 00:00:31,000\nfuck\n"
                     "3\n00:00:55,000 --> 00:00:56,000\nfuck\n")
        binned = {b["minute"]: b["f_bombs"] for b in r["binned"]}
        assert binned[0] == 3


# ─── Rating system ───────────────────────

class TestRating:
    def test_wholesome(self, engine, tmp_path):
        r = _analyse(engine, tmp_path,
                     "1\n00:00:01,000 --> 00:00:02,000\nhello\n")
        s = r["summary"]
        assert "QUIET" in s["rating"] or "ANGEL" in s["rating"]

    def test_edgy(self, engine, tmp_path):
        r = _analyse(engine, tmp_path,
                     "1\n00:00:01,000 --> 00:00:02,000\nfuck\n")
        assert "Wholesome" in r["summary"]["rating"]

    def test_toxic(self, engine, tmp_path):
        """200+ hard slurs = HAZMAT."""
        lines = []
        for i in range(1, 210):
            m = (i * 2) // 60
            s = (i * 2) % 60
            lines.append(f"{i}\n00:{m:02d}:{s:02d},000 --> 00:{m:02d}:{s+1:02d},000\nnigger")
        
        r = _analyse(engine, tmp_path, "\n\n".join(lines))
        assert "HAZMAT" in r["summary"]["rating"]
