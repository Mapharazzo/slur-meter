"""Pure subtitle inspection, quality, and deterministic ranking."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from src.data.opensubtitles import SubtitleResult

_TIMING = re.compile(
    r"^(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(?P<end>\d{2}:\d{2}:\d{2},\d{3})\s*$"
)


class SubtitleParseError(ValueError):
    """The candidate does not contain structurally valid SRT cues."""


@dataclass(frozen=True)
class SubtitleInspection:
    detected_encoding: str
    cue_count: int
    first_cue_seconds: float
    final_cue_seconds: float
    parsed_duration_seconds: float
    normalized_utf8: bytes


@dataclass(frozen=True)
class SubtitleQuality:
    accepted: bool
    coverage_percent: float | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SubtitleRequest:
    imdb_id: str | None = None
    language: str = "en"
    title: str | None = None
    year: int | str | None = None


@dataclass(frozen=True)
class RankedCandidate:
    candidate: SubtitleResult
    score: int
    reasons: tuple[str, ...]


def inspect_subtitle(path: str | Path) -> SubtitleInspection:
    raw = Path(path).read_bytes()
    text, encoding = _decode(raw)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise SubtitleParseError("Malformed SRT: no cues were found")
    starts: list[float] = []
    ends: list[float] = []
    canonical_blocks: list[str] = []
    for expected_number, block in enumerate(re.split(r"\n{2,}", normalized), start=1):
        lines = block.split("\n")
        if len(lines) < 3 or not lines[0].strip().isdigit():
            raise SubtitleParseError("Malformed SRT: each cue needs a number, timing, and dialogue")
        if int(lines[0].strip()) != expected_number:
            raise SubtitleParseError("Malformed SRT: cue numbering must be sequential")
        match = _TIMING.match(lines[1].strip())
        if match is None or not any(line.strip() for line in lines[2:]):
            raise SubtitleParseError("Malformed SRT: each cue needs valid timing and dialogue")
        start = _seconds(match.group("start"))
        end = _seconds(match.group("end"))
        if end <= start:
            raise SubtitleParseError("Malformed SRT: cue end must follow its start")
        starts.append(start)
        ends.append(end)
        canonical_blocks.append("\n".join([str(expected_number), lines[1].strip(), *lines[2:]]))
    return SubtitleInspection(
        detected_encoding=encoding,
        cue_count=len(starts),
        first_cue_seconds=min(starts),
        final_cue_seconds=max(ends),
        parsed_duration_seconds=max(ends) - min(starts),
        normalized_utf8=("\n\n".join(canonical_blocks) + "\n").encode("utf-8"),
    )


def evaluate_quality(
    inspection: SubtitleInspection,
    runtime_seconds: float | None,
    threshold: float = 0.70,
) -> SubtitleQuality:
    if runtime_seconds is None or runtime_seconds <= 0:
        return SubtitleQuality(False, None, ("expected_runtime_unavailable",))
    coverage = inspection.final_cue_seconds / float(runtime_seconds) * 100
    reasons: list[str] = []
    if coverage < threshold * 100:
        reasons.append("coverage_below_threshold")
    if coverage > 120:
        reasons.append("coverage_exceeds_runtime")
    return SubtitleQuality(not reasons or reasons == ["coverage_exceeds_runtime"], coverage, tuple(reasons))


def rank_candidates(
    candidates: Iterable[SubtitleResult], request: SubtitleRequest
) -> list[RankedCandidate]:
    ranked: list[RankedCandidate] = []
    for candidate in candidates:
        score = 0
        reasons: list[str] = []
        if request.imdb_id and candidate.imdb_id and candidate.imdb_id.casefold() == request.imdb_id.casefold():
            score += 1_000
            reasons.append("exact_imdb_match")
        if candidate.language.casefold() == request.language.casefold():
            score += 100
            reasons.append("language_match")
        if request.title and _normal(candidate.movie_title) == _normal(request.title):
            score += 50
            reasons.append("title_match")
        if request.year is not None and candidate.movie_year and str(candidate.movie_year) == str(request.year):
            score += 25
            reasons.append("year_match")
        if candidate.provider_rating is not None:
            score += round(candidate.provider_rating)
            reasons.append("provider_rating")
        if candidate.download_count is not None:
            score += min(candidate.download_count, 10_000) // 1_000
            reasons.append("provider_download_count")
        ranked.append(RankedCandidate(candidate, score, tuple(reasons)))
    return sorted(
        ranked,
        key=lambda item: (-item.score, item.candidate.file_name.casefold(), item.candidate.file_id),
    )


def _decode(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(encoding), "utf-8" if encoding == "utf-8-sig" else encoding
        except UnicodeDecodeError:
            continue
    raise SubtitleParseError("Subtitle encoding is not supported")


def _seconds(value: str) -> float:
    hours, minutes, seconds = value.replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _normal(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())
