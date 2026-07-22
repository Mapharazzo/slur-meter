from pathlib import Path

import pytest

from src.analysis.engine import ProfanityEngine
from src.data.opensubtitles import SubtitleResult
from src.data.subtitle_quality import (
    SubtitleRequest,
    evaluate_quality,
    inspect_subtitle,
    rank_candidates,
)


def write_srt(tmp_path: Path, *, final_end="00:01:00,000", text="clean dialogue", encoding="utf-8"):
    path = tmp_path / "candidate.srt"
    path.write_text(
        f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n\n"
        f"2\n00:00:10,000 --> {final_end}\nfinal cue\n",
        encoding=encoding,
    )
    return path


def test_coverage_uses_final_cue_not_last_profanity(tmp_path):
    path = write_srt(tmp_path, final_end="01:35:00,000", text="clean dialogue")
    inspection = inspect_subtitle(path)
    result = evaluate_quality(inspection, runtime_seconds=100 * 60)
    assert result.coverage_percent == pytest.approx(95.0)
    assert result.accepted is True


def test_inspection_falls_back_to_cp1252_and_normalizes_text(tmp_path):
    path = write_srt(tmp_path, text="caf\u00e9", encoding="cp1252")
    inspection = inspect_subtitle(path)
    assert inspection.detected_encoding == "cp1252"
    assert inspection.cue_count == 2
    assert inspection.final_cue_seconds == pytest.approx(60.0)


def test_quality_warns_for_extended_edition_without_rejecting(tmp_path):
    inspection = inspect_subtitle(write_srt(tmp_path, final_end="02:10:00,000"))
    result = evaluate_quality(inspection, runtime_seconds=100 * 60)
    assert result.accepted is True
    assert result.coverage_percent == pytest.approx(130.0)
    assert "coverage_exceeds_runtime" in result.reasons


def test_analysis_runtime_uses_final_subtitle_cue_when_dialogue_is_clean(tmp_path):
    path = write_srt(tmp_path, final_end="01:36:00,000")
    engine = ProfanityEngine({"categories": {}})
    assert engine.analyse_srt(path)["summary"]["runtime_minutes"] == 96


def test_ranking_is_deterministic_and_exposes_match_reasons():
    requested = SubtitleRequest(imdb_id="tt0110912", language="en", title="Pulp Fiction", year=1994)
    candidates = [
        SubtitleResult("b", "z.srt", "Pulp Fiction", "1994", "en", None, "tt0110912"),
        SubtitleResult("a", "a.srt", "Other", "1993", "fr", None, "tt0000001"),
        SubtitleResult("c", "a.srt", "Pulp Fiction", "1994", "en", None, "tt0110912"),
    ]
    ranked = rank_candidates(candidates, requested)
    assert [item.candidate.file_id for item in ranked] == ["c", "b", "a"]
    assert "exact_imdb_match" in ranked[0].reasons
