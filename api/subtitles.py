"""Durable discovery and selection of safe subtitle candidates."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from api.database import OperationStore
from api.domain import AttemptTrigger, JobState, StageState
from api.errors import AttentionRequired
from api.settings import Settings, confined_path
from src.data.opensubtitles import (
    OpenSubtitlesClient,
    SubtitleCache,
    UnsafeArchiveError,
)
from src.data.subtitle_quality import (
    SubtitleParseError,
    SubtitleRequest,
    evaluate_quality,
    inspect_subtitle,
    rank_candidates,
)

_EXHAUSTION_ACTIONS = (
    "select_subtitle",
    "rediscover_subtitles",
    "upload_subtitle",
    "cancel",
)
_SAFE_PARSE_ERROR = "Subtitle candidate could not be parsed."
_SAFE_ARCHIVE_ERROR = "Subtitle archive could not be safely read."


class SubtitleService:
    """Coordinates provider work while preserving candidate history in the store."""

    def __init__(
        self,
        store: OperationStore,
        client: OpenSubtitlesClient,
        cache: SubtitleCache,
        settings: Settings,
    ) -> None:
        self.store = store
        self.client = client
        self.cache = cache
        self.settings = settings
        self._root = settings.results_dir / "subtitle-candidates"

    def discover(self, job_id: str) -> list[dict[str, Any]]:
        """Persist ranked provider metadata; no provider filename becomes a path."""
        job = self._job(job_id)
        cycle = max((item["discovery_cycle"] for item in self.store.list_candidates(job_id)), default=0) + 1
        results = self.client.search(
            query=job["query"] or None,
            imdb_id=job["source_imdb_id"] or None,
            language="en",
            limit=20,
        )
        request = SubtitleRequest(
            imdb_id=job["source_imdb_id"], language="en", title=job["label"], year=None
        )
        created: list[dict[str, Any]] = []
        for rank, ranked in enumerate(rank_candidates(results, request), start=1):
            candidate = ranked.candidate
            row, _ = self.store.record_candidate(
                job_id,
                "opensubtitles",
                candidate.file_id,
                provider_filename=candidate.file_name,
                source_type="provider",
                language=candidate.language,
                fps=candidate.fps,
                title=candidate.movie_title,
                year=_year(candidate.movie_year),
                imdb_match=bool(
                    job["source_imdb_id"]
                    and candidate.imdb_id
                    and candidate.imdb_id.casefold() == job["source_imdb_id"].casefold()
                ),
                provider_rating=candidate.provider_rating,
                provider_download_count=candidate.download_count,
                discovery_cycle=cycle,
                rank=rank,
                rank_reasons=list(ranked.reasons),
                expected_runtime_seconds=candidate.runtime_seconds,
                status="discovered",
            )
            created.append(row)
        self.store.record_event(
            job_id,
            event_type="subtitle_discovered",
            message=f"Discovered {len(created)} subtitle candidates.",
            data={"discovery_cycle": cycle, "candidate_count": len(created)},
        )
        return created

    def select(
        self, job_id: str, manual_candidate_id: str | None = None
    ) -> dict[str, Any]:
        """Select one validated candidate, or leave a durable attention state."""
        selected = next(
            (row for row in self.store.list_candidates(job_id) if row["status"] == "selected"),
            None,
        )
        if selected is not None and self._selected_contract_valid(job_id, selected):
            return selected
        if selected is not None:
            self.store.update_candidate(selected["id"], status="validated")
        self._start_selection(job_id)
        validated = next(
            (row for row in self.store.list_candidates(job_id) if row["status"] == "validated"),
            None,
        )
        if validated is not None:
            return self._resume_validated(job_id, validated)
        if manual_candidate_id is not None:
            candidate = self.store.get_candidate(manual_candidate_id, include_internal=True)
            if candidate is None or candidate["job_id"] != job_id:
                raise ValueError("Subtitle candidate does not belong to this run")
            return self._evaluate(job_id, candidate, manual=True)

        limit = min(3, self.settings.subtitle_candidates_per_cycle)
        candidates = [
            row
            for row in self.store.list_candidates(job_id)
            if row["status"] == "discovered"
        ][:limit]
        for candidate in candidates:
            selected = self._evaluate(job_id, candidate, manual=False)
            if selected["status"] == "selected":
                return selected
        self._exhaust(job_id, attempted=len(candidates), limit=limit)
        raise AttentionRequired(
            _exhaustion_message(len(candidates)),
            code="subtitle_candidates_exhausted",
            actions=_EXHAUSTION_ACTIONS,
        )

    def upload(self, job_id: str, filename: str, content: bytes) -> dict[str, Any]:
        """Store uploads below a generated candidate directory, never their filename."""
        if not isinstance(content, bytes) or len(content) > OpenSubtitlesClient.MAX_DOWNLOAD_BYTES:
            raise ValueError("Uploaded subtitle exceeds the size limit")
        digest = sha256(content).hexdigest()
        row, _ = self.store.record_candidate(
            job_id,
            "upload",
            digest,
            provider_filename=Path(filename).name,
            source_type="upload",
            discovery_cycle=max((item["discovery_cycle"] for item in self.store.list_candidates(job_id)), default=0) + 1,
            rank=0,
            rank_reasons=["operator_upload"],
            status="uploaded",
            content_hash=digest,
        )
        destination = confined_path(self._root, job_id, row["id"], "subtitle.srt")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        try:
            inspection = inspect_subtitle(destination)
        except SubtitleParseError:
            return self.store.update_candidate(
                row["id"],
                status="rejected",
                parse_error=_SAFE_PARSE_ERROR,
                rejection_reasons=["invalid_srt"],
            ) or row
        _write_normalized(destination, inspection.normalized_utf8)
        return self.store.update_candidate(
            row["id"],
            detected_encoding=inspection.detected_encoding,
            cue_count=inspection.cue_count,
            first_cue_seconds=inspection.first_cue_seconds,
            final_cue_seconds=inspection.final_cue_seconds,
            parsed_duration_seconds=inspection.parsed_duration_seconds,
            content_hash=_hash(destination),
            artifact_path=str(destination),
        ) or row

    def _evaluate(
        self, job_id: str, candidate: dict[str, Any], *, manual: bool) -> dict[str, Any]:
        attempt = self.store.start_attempt(
            job_id,
            "subtitle_selection",
            max_attempts=3,
            candidate_id=candidate["id"],
        )
        try:
            path = self._candidate_path(job_id, candidate)
            inspection = inspect_subtitle(path)
            _write_normalized(path, inspection.normalized_utf8)
            quality = evaluate_quality(
                inspection,
                candidate["expected_runtime_seconds"],
                self.settings.subtitle_coverage_threshold,
            )
            reasons = list(quality.reasons)
            accepted = quality.accepted
            if manual and not accepted:
                accepted = True
                reasons.append("manual_threshold_override")
            if accepted:
                validated = self.store.update_candidate(
                    candidate["id"],
                    detected_encoding=inspection.detected_encoding,
                    cue_count=inspection.cue_count,
                    first_cue_seconds=inspection.first_cue_seconds,
                    final_cue_seconds=inspection.final_cue_seconds,
                    parsed_duration_seconds=inspection.parsed_duration_seconds,
                    coverage_percent=quality.coverage_percent,
                    quality_reasons=reasons,
                    rejection_reasons=[],
                    status="validated",
                    content_hash=_hash(path),
                    artifact_path=str(path),
                    selection_method="manual" if manual else "automatic",
                )
                return self._complete_validated(job_id, validated, path, attempt)
            updated = self.store.update_candidate(
                candidate["id"],
                detected_encoding=inspection.detected_encoding,
                cue_count=inspection.cue_count,
                first_cue_seconds=inspection.first_cue_seconds,
                final_cue_seconds=inspection.final_cue_seconds,
                parsed_duration_seconds=inspection.parsed_duration_seconds,
                coverage_percent=quality.coverage_percent,
                quality_reasons=reasons,
                rejection_reasons=reasons,
                status="rejected",
                content_hash=_hash(path),
                artifact_path=str(path),
            )
            self.store.finish_attempt(attempt["id"], "rejected", diagnostics={"reasons": reasons})
            return updated
        except UnsafeArchiveError:
            updated = self.store.update_candidate(
                candidate["id"],
                status="rejected",
                download_error=_SAFE_ARCHIVE_ERROR,
                rejection_reasons=["unsafe_download"],
            )
            self.store.finish_attempt(attempt["id"], "rejected", diagnostics={"reason": "unsafe_download"})
            return updated or candidate
        except (OSError, ValueError, SubtitleParseError):
            updated = self.store.update_candidate(
                candidate["id"],
                status="rejected",
                parse_error=_SAFE_PARSE_ERROR,
                rejection_reasons=["invalid_srt"],
            )
            self.store.finish_attempt(attempt["id"], "rejected", diagnostics={"reason": "invalid_srt"})
            return updated or candidate

    def _candidate_path(self, job_id: str, candidate: dict[str, Any]) -> Path:
        if candidate["source_type"] in {"upload", "cache"}:
            stored = self.store.get_candidate(candidate["id"], include_internal=True)
            if stored and stored.get("artifact_path"):
                return Path(stored["artifact_path"])
            raise SubtitleParseError("Candidate file is unavailable")
        destination = confined_path(self._root, job_id, candidate["id"], "subtitle.srt")
        destination.parent.mkdir(parents=True, exist_ok=True)
        downloaded = self.client.download(candidate["provider_id"], destination).resolve()
        try:
            downloaded.relative_to(destination.parent.resolve())
        except ValueError as exc:
            raise UnsafeArchiveError("Provider download escaped its generated destination") from exc
        return downloaded

    def _resume_validated(self, job_id: str, candidate: dict[str, Any]) -> dict[str, Any]:
        stored = self.store.get_candidate(candidate["id"], include_internal=True)
        if stored is None or not stored.get("artifact_path"):
            raise SubtitleParseError("Candidate file is unavailable")
        path = Path(stored["artifact_path"])
        inspection = inspect_subtitle(path)
        _write_normalized(path, inspection.normalized_utf8)
        if _hash(path) != candidate["content_hash"]:
            candidate = self.store.update_candidate(candidate["id"], content_hash=_hash(path)) or candidate
        stage = self._selection_stage(job_id)
        attempt = (
            self.store.start_attempt(
                job_id, "subtitle_selection", max_attempts=3, candidate_id=candidate["id"]
            )
            if StageState(stage["state"]) is StageState.RUNNING
            else None
        )
        return self._complete_validated(job_id, candidate, path, attempt)

    def _complete_validated(
        self,
        job_id: str,
        candidate: dict[str, Any],
        path: Path,
        attempt: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Complete durable promotion before marking a candidate selected."""
        job = self._job(job_id)
        if job["source_imdb_id"]:
            self.cache.store(job["source_imdb_id"], path, replace=True)
        if not any(
            event["type"] == "subtitle_selected" and event["data"].get("candidate_id") == candidate["id"]
            for event in self.store.list_events(job_id)
        ):
            self.store.record_event(
                job_id,
                event_type="subtitle_selected",
                message="A subtitle candidate was selected.",
                data={"candidate_id": candidate["id"], "selection_method": candidate["selection_method"]},
            )
        if candidate["selection_method"] == "manual":
            self.store.record_decision(
                job_id,
                "select_subtitle",
                candidate_id=candidate["id"],
                accepted=True,
                reason="Manual subtitle selection accepted a parsed threshold override.",
                idempotency_key=f"subtitle-selected:{candidate['id']}",
            )
        stage = self._selection_stage(job_id)
        if StageState(stage["state"]) is StageState.RUNNING:
            self.store.transition_stage(
                job_id, "subtitle_selection", StageState.COMPLETED, expected_state=StageState.RUNNING
            )
        elif StageState(stage["state"]) is not StageState.COMPLETED:
            raise RuntimeError("Subtitle selection stage cannot be completed")
        if attempt is not None:
            self.store.finish_attempt(
                attempt["id"], "completed", output={"candidate_id": candidate["id"]}
            )
        return self.store.update_candidate(
            candidate["id"], status="selected", selected_at=datetime.now(UTC).isoformat()
        ) or candidate

    def _selected_contract_valid(self, job_id: str, candidate: dict[str, Any]) -> bool:
        stored = self.store.get_candidate(candidate["id"], include_internal=True)
        if stored is None or not stored.get("artifact_path"):
            return False
        path = Path(stored["artifact_path"])
        cache_path = self.cache.has(self._job(job_id)["source_imdb_id"] or "")
        return (
            path.exists()
            and candidate["content_hash"] == _hash(path)
            and cache_path is not None
            and cache_path.read_bytes() == path.read_bytes()
            and StageState(self._selection_stage(job_id)["state"]) is StageState.COMPLETED
        )

    def _selection_stage(self, job_id: str) -> dict[str, Any]:
        return next(
            stage
            for stage in self.store.get_job_detail(job_id)["stages"]
            if stage["name"] == "subtitle_selection"
        )

    def _start_selection(self, job_id: str) -> None:
        job = self._job(job_id)
        state = JobState(job["state"])
        if state in {JobState.NEEDS_ATTENTION, JobState.FAILED, JobState.CANCELLED}:
            self.store.transition_job(job_id, JobState.QUEUED, trigger=AttemptTrigger.RESUME)
            state = JobState.QUEUED
        if state is JobState.QUEUED:
            self.store.transition_job(job_id, JobState.RUNNING, expected_state=JobState.QUEUED)
        stage = self.store.ensure_stage(
            job_id, "subtitle_selection", ordinal=4, state=StageState.PENDING, max_auto_attempts=3
        )
        stage_state = StageState(stage["state"])
        if stage_state in {StageState.NEEDS_ATTENTION, StageState.FAILED}:
            stage = self.store.transition_stage(
                job_id, "subtitle_selection", StageState.QUEUED, trigger=AttemptTrigger.RESUME
            )
            stage_state = StageState(stage["state"])
        if stage_state is StageState.PENDING:
            stage = self.store.transition_stage(job_id, "subtitle_selection", StageState.QUEUED)
            stage_state = StageState(stage["state"])
        if stage_state is StageState.QUEUED:
            self.store.transition_stage(
                job_id, "subtitle_selection", StageState.RUNNING, expected_state=StageState.QUEUED
            )

    def _exhaust(self, job_id: str, *, attempted: int, limit: int) -> None:
        self.store.transition_stage(
            job_id,
            "subtitle_selection",
            StageState.NEEDS_ATTENTION,
            expected_state=StageState.RUNNING,
            progress_numerator=attempted,
            progress_denominator=limit,
            progress_unit="candidates",
            safe_error_code="subtitle_candidates_exhausted",
            safe_error_message="No acceptable subtitle candidate was found.",
            next_action="select_subtitle",
        )
        self.store.transition_job(
            job_id,
            JobState.NEEDS_ATTENTION,
            expected_state=JobState.RUNNING,
            safe_error_code="subtitle_candidates_exhausted",
            safe_error_message=_exhaustion_message(attempted),
            next_action="select_subtitle",
        )

    def _job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job is None:
            raise KeyError("Run was not found")
        return job


def _year(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _write_normalized(path: Path, content: bytes) -> None:
    partial = path.with_suffix(".normalized")
    partial.write_bytes(content)
    partial.replace(path)


def _exhaustion_message(attempted: int) -> str:
    noun = "candidate was" if attempted == 1 else "candidates were"
    word = {0: "No", 1: "One", 2: "Two", 3: "Three"}.get(attempted, str(attempted))
    return f"{word} subtitle {noun} rejected."
