"""Durable discovery and selection of safe subtitle candidates."""

from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
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
    promote_subtitle_file,
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


def generated_upload_path(
    candidate_root: str | Path, job_id: str, candidate_id: str
) -> Path:
    """Resolve the sole generated artifact location for an upload candidate."""
    return confined_path(candidate_root, job_id, candidate_id, "subtitle.srt")


def recover_interrupted_uploads(
    store: OperationStore, candidate_root: str | Path
) -> list[str]:
    """Reconcile pending upload artifacts without requiring provider credentials."""
    recovered: list[str] = []
    for candidate in store.list_pending_uploads():
        try:
            expected = generated_upload_path(
                candidate_root, candidate["job_id"], candidate["id"]
            )
        except ValueError:
            expected = None
        if expected is not None:
            expected.unlink(missing_ok=True)
            shutil.rmtree(expected.parent, ignore_errors=True)
        if store.reject_pending_upload(candidate["id"]):
            recovered.append(candidate["id"])
    return recovered


@dataclass(frozen=True)
class _ExecutionContext:
    lease_owner: str | None
    cancel_requested: Callable[[], bool] | None

    def check(self) -> None:
        if self.cancel_requested is not None and self.cancel_requested():
            raise asyncio.CancelledError("Subtitle work no longer owns the job lease")

    def publication_allowed(self) -> bool:
        """Revalidate ownership at the filesystem publication boundary."""
        self.check()
        return True

    @staticmethod
    def require(value: Any) -> Any:
        if value is None:
            raise asyncio.CancelledError("Subtitle work no longer owns the job lease")
        return value


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

    def discover(
        self,
        job_id: str,
        *,
        lease_owner: str | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """Persist ranked provider metadata; no provider filename becomes a path."""
        context = _ExecutionContext(lease_owner, cancel_requested)
        context.check()
        job = self._job(job_id)
        cycle = (
            max(
                (
                    item["discovery_cycle"]
                    for item in self.store.list_candidates(job_id)
                ),
                default=0,
            )
            + 1
        )
        results = self.client.search(
            query=job["query"] or None,
            imdb_id=job["source_imdb_id"] or None,
            language="en",
            limit=20,
        )
        context.check()
        request = SubtitleRequest(
            imdb_id=job["source_imdb_id"], language="en", title=job["label"], year=None
        )
        created: list[dict[str, Any]] = []
        for rank, ranked in enumerate(rank_candidates(results, request), start=1):
            candidate = ranked.candidate
            context.check()
            row, _ = context.require(
                self.store.record_candidate(
                    job_id,
                    "opensubtitles",
                    candidate.file_id,
                    lease_owner=context.lease_owner,
                    provider_filename=candidate.file_name,
                    source_type="provider",
                    language=candidate.language,
                    fps=candidate.fps,
                    title=candidate.movie_title,
                    year=_year(candidate.movie_year),
                    imdb_match=bool(
                        job["source_imdb_id"]
                        and candidate.imdb_id
                        and candidate.imdb_id.casefold()
                        == job["source_imdb_id"].casefold()
                    ),
                    provider_rating=candidate.provider_rating,
                    provider_download_count=candidate.download_count,
                    discovery_cycle=cycle,
                    rank=rank,
                    rank_reasons=list(ranked.reasons),
                    expected_runtime_seconds=candidate.runtime_seconds,
                    status="discovered",
                )
            )
            created.append(row)
        context.check()
        context.require(
            self.store.record_event(
                job_id,
                event_type="subtitle_discovered",
                message=f"Discovered {len(created)} subtitle candidates.",
                data={"discovery_cycle": cycle, "candidate_count": len(created)},
                lease_owner=context.lease_owner,
            )
        )
        return created

    def select(
        self,
        job_id: str,
        manual_candidate_id: str | None = None,
        *,
        lease_owner: str | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Select one validated candidate, or leave a durable attention state."""
        context = _ExecutionContext(lease_owner, cancel_requested)
        context.check()
        selected = next(
            (
                row
                for row in self.store.list_candidates(job_id)
                if row["status"] == "selected"
            ),
            None,
        )
        if selected is not None and self._selected_contract_valid(
            job_id, selected, context
        ):
            return selected
        if selected is not None:
            context.check()
            context.require(
                self.store.update_candidate(
                    selected["id"],
                    lease_owner=context.lease_owner,
                    status="validated",
                )
            )
        self._start_selection(job_id, context)
        validated = next(
            (
                row
                for row in self.store.list_candidates(job_id)
                if row["status"] == "validated"
            ),
            None,
        )
        if validated is not None:
            return self._resume_validated(job_id, validated, context)
        if manual_candidate_id is not None:
            candidate = self.store.get_candidate(
                manual_candidate_id, include_internal=True
            )
            if candidate is None or candidate["job_id"] != job_id:
                raise ValueError("Subtitle candidate does not belong to this run")
            return self._evaluate(job_id, candidate, manual=True, context=context)

        limit = min(3, self.settings.subtitle_candidates_per_cycle)
        candidates = [
            row
            for row in self.store.list_candidates(job_id)
            if row["status"] == "discovered"
        ][:limit]
        for candidate in candidates:
            context.check()
            selected = self._evaluate(job_id, candidate, manual=False, context=context)
            if selected["status"] == "selected":
                return selected
        self._exhaust(job_id, attempted=len(candidates), limit=limit, context=context)
        raise AttentionRequired(
            _exhaustion_message(len(candidates)),
            code="subtitle_candidates_exhausted",
            actions=_EXHAUSTION_ACTIONS,
        )

    def upload(
        self,
        job_id: str,
        filename: str,
        content: bytes,
        *,
        lease_owner: str | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Store uploads below a generated candidate directory, never their filename."""
        context = _ExecutionContext(lease_owner, cancel_requested)
        context.check()
        if (
            not isinstance(content, bytes)
            or len(content) > OpenSubtitlesClient.MAX_DOWNLOAD_BYTES
        ):
            raise ValueError("Uploaded subtitle exceeds the size limit")
        digest = sha256(content).hexdigest()
        row, created = context.require(
            self.store.record_candidate(
                job_id,
                "upload",
                digest,
                lease_owner=context.lease_owner,
                provider_filename=Path(filename).name,
                source_type="upload",
                discovery_cycle=0,
                rank=0,
                rank_reasons=["operator_upload"],
                status="upload_pending",
                content_hash=digest,
            )
        )
        if not created and row["status"] in {"uploaded", "selected"}:
            return row
        if not created:
            generated_upload_path(self._root, job_id, row["id"]).unlink(
                missing_ok=True
            )
            row = context.require(
                self.store.update_candidate(
                    row["id"],
                    lease_owner=context.lease_owner,
                    status="upload_pending",
                    parse_error=None,
                    rejection_reasons=[],
                    artifact_path=None,
                    content_hash=digest,
                )
            )
        destination, staging_directory, staged_path = self._candidate_workspace(
            job_id, row["id"]
        )
        try:
            context.check()
            staged_path.write_bytes(content)
            context.check()
            inspection = inspect_subtitle(staged_path)
            context.check()
            _write_normalized(staged_path, inspection.normalized_utf8)
            context.check()
            path = promote_subtitle_file(
                staged_path,
                destination,
                publish_allowed=context.publication_allowed,
            )
        except SubtitleParseError:
            return context.require(
                self.store.update_candidate(
                    row["id"],
                    lease_owner=context.lease_owner,
                    status="rejected",
                    parse_error=_SAFE_PARSE_ERROR,
                    rejection_reasons=["invalid_srt"],
                )
            )
        finally:
            shutil.rmtree(staging_directory, ignore_errors=True)
        context.check()
        return context.require(
            self.store.update_candidate(
                row["id"],
                lease_owner=context.lease_owner,
                detected_encoding=inspection.detected_encoding,
                cue_count=inspection.cue_count,
                first_cue_seconds=inspection.first_cue_seconds,
                final_cue_seconds=inspection.final_cue_seconds,
                parsed_duration_seconds=inspection.parsed_duration_seconds,
                content_hash=_hash(path),
                artifact_path=str(path),
            )
        )

    def recover_pending_uploads(self) -> list[str]:
        """Reject and clean interrupted uploads before workers can claim runs."""
        return recover_interrupted_uploads(self.store, self._root)

    def _evaluate(
        self,
        job_id: str,
        candidate: dict[str, Any],
        *,
        manual: bool,
        context: _ExecutionContext,
    ) -> dict[str, Any]:
        context.check()
        attempt = context.require(
            self.store.start_attempt(
                job_id,
                "subtitle_selection",
                max_attempts=3,
                candidate_id=candidate["id"],
                lease_owner=context.lease_owner,
            )
        )
        destination, staging_directory, _ = self._candidate_workspace(
            job_id, candidate["id"]
        )
        try:
            path = self._candidate_path(candidate, staging_directory, context)
            context.check()
            inspection = inspect_subtitle(path)
            context.check()
            _write_normalized(path, inspection.normalized_utf8)
            context.check()
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
            path = promote_subtitle_file(
                path,
                destination,
                publish_allowed=context.publication_allowed,
            )
            candidate_fields = {
                "detected_encoding": inspection.detected_encoding,
                "cue_count": inspection.cue_count,
                "first_cue_seconds": inspection.first_cue_seconds,
                "final_cue_seconds": inspection.final_cue_seconds,
                "parsed_duration_seconds": inspection.parsed_duration_seconds,
                "coverage_percent": quality.coverage_percent,
                "quality_reasons": reasons,
                "content_hash": _hash(path),
                "artifact_path": str(path),
            }
            context.check()
            if accepted:
                validated = context.require(
                    self.store.update_candidate(
                        candidate["id"],
                        lease_owner=context.lease_owner,
                        rejection_reasons=[],
                        status="validated",
                        selection_method="manual" if manual else "automatic",
                        **candidate_fields,
                    )
                )
                return self._complete_validated(
                    job_id, validated, path, attempt, context
                )
            updated = context.require(
                self.store.update_candidate(
                    candidate["id"],
                    lease_owner=context.lease_owner,
                    rejection_reasons=reasons,
                    status="rejected",
                    **candidate_fields,
                )
            )
            context.check()
            context.require(
                self.store.finish_attempt(
                    attempt["id"],
                    "rejected",
                    diagnostics={"reasons": reasons},
                    lease_owner=context.lease_owner,
                )
            )
            return updated
        except UnsafeArchiveError:
            context.check()
            updated = context.require(
                self.store.update_candidate(
                    candidate["id"],
                    lease_owner=context.lease_owner,
                    status="rejected",
                    download_error=_SAFE_ARCHIVE_ERROR,
                    rejection_reasons=["unsafe_download"],
                )
            )
            context.check()
            context.require(
                self.store.finish_attempt(
                    attempt["id"],
                    "rejected",
                    diagnostics={"reason": "unsafe_download"},
                    lease_owner=context.lease_owner,
                )
            )
            return updated
        except (OSError, ValueError, SubtitleParseError):
            context.check()
            updated = context.require(
                self.store.update_candidate(
                    candidate["id"],
                    lease_owner=context.lease_owner,
                    status="rejected",
                    parse_error=_SAFE_PARSE_ERROR,
                    rejection_reasons=["invalid_srt"],
                )
            )
            context.check()
            context.require(
                self.store.finish_attempt(
                    attempt["id"],
                    "rejected",
                    diagnostics={"reason": "invalid_srt"},
                    lease_owner=context.lease_owner,
                )
            )
            return updated
        finally:
            shutil.rmtree(staging_directory, ignore_errors=True)

    def _candidate_path(
        self,
        candidate: dict[str, Any],
        staging_directory: Path,
        context: _ExecutionContext,
    ) -> Path:
        context.check()
        destination = staging_directory / "subtitle.srt"
        if candidate["source_type"] in {"upload", "cache"}:
            stored = self.store.get_candidate(candidate["id"], include_internal=True)
            if stored and stored.get("artifact_path"):
                shutil.copy2(Path(stored["artifact_path"]), destination)
                return destination
            raise SubtitleParseError("Candidate file is unavailable")
        downloaded = self.client.download(
            candidate["provider_id"], destination
        ).resolve()
        context.check()
        try:
            downloaded.relative_to(staging_directory.resolve())
        except ValueError as exc:
            raise UnsafeArchiveError(
                "Provider download escaped its generated destination"
            ) from exc
        return downloaded

    def _resume_validated(
        self,
        job_id: str,
        candidate: dict[str, Any],
        context: _ExecutionContext,
    ) -> dict[str, Any]:
        context.check()
        stored = self.store.get_candidate(candidate["id"], include_internal=True)
        if stored is None or not stored.get("artifact_path"):
            return self._reject_resumed_artifact(job_id, candidate, context)
        destination, staging_directory, staged_path = self._candidate_workspace(
            job_id, candidate["id"]
        )
        try:
            context.check()
            shutil.copy2(Path(stored["artifact_path"]), staged_path)
            inspection = inspect_subtitle(staged_path)
            context.check()
            _write_normalized(staged_path, inspection.normalized_utf8)
            context.check()
            path = promote_subtitle_file(
                staged_path,
                destination,
                publish_allowed=context.publication_allowed,
            )
        except (OSError, ValueError, SubtitleParseError):
            return self._reject_resumed_artifact(job_id, candidate, context)
        finally:
            shutil.rmtree(staging_directory, ignore_errors=True)
        attempt = self._active_candidate_attempt(job_id, candidate["id"])
        if _hash(path) == candidate["content_hash"]:
            return self._complete_validated(job_id, candidate, path, attempt, context)
        return self._reapply_quality(
            job_id, candidate, path, inspection, attempt, context
        )

    def _reapply_quality(
        self,
        job_id: str,
        candidate: dict[str, Any],
        path: Path,
        inspection,
        attempt: dict[str, Any] | None,
        context: _ExecutionContext,
    ) -> dict[str, Any]:
        context.check()
        quality = evaluate_quality(
            inspection,
            candidate["expected_runtime_seconds"],
            self.settings.subtitle_coverage_threshold,
        )
        reasons = list(quality.reasons)
        accepted = quality.accepted
        if candidate["selection_method"] == "manual" and not accepted:
            accepted = True
            reasons.append("manual_threshold_override")
        fields = {
            "detected_encoding": inspection.detected_encoding,
            "cue_count": inspection.cue_count,
            "first_cue_seconds": inspection.first_cue_seconds,
            "final_cue_seconds": inspection.final_cue_seconds,
            "parsed_duration_seconds": inspection.parsed_duration_seconds,
            "coverage_percent": quality.coverage_percent,
            "quality_reasons": reasons,
            "content_hash": _hash(path),
            "artifact_path": str(path),
        }
        if accepted:
            updated = context.require(
                self.store.update_candidate(
                    candidate["id"],
                    lease_owner=context.lease_owner,
                    status="validated",
                    rejection_reasons=[],
                    **fields,
                )
            )
            return self._complete_validated(job_id, updated, path, attempt, context)
        updated = context.require(
            self.store.update_candidate(
                candidate["id"],
                lease_owner=context.lease_owner,
                status="rejected",
                rejection_reasons=reasons,
                **fields,
            )
        )
        if attempt is not None:
            context.check()
            context.require(
                self.store.finish_attempt(
                    attempt["id"],
                    "rejected",
                    diagnostics={"reasons": reasons},
                    lease_owner=context.lease_owner,
                )
            )
        return self._raise_resume_attention(job_id, updated, context)

    def _reject_resumed_artifact(
        self,
        job_id: str,
        candidate: dict[str, Any],
        context: _ExecutionContext,
    ) -> dict[str, Any]:
        context.check()
        updated = context.require(
            self.store.update_candidate(
                candidate["id"],
                lease_owner=context.lease_owner,
                status="rejected",
                parse_error=_SAFE_PARSE_ERROR,
                rejection_reasons=["invalid_srt"],
            )
        )
        attempt = self._active_candidate_attempt(job_id, candidate["id"])
        if attempt is not None:
            context.check()
            context.require(
                self.store.finish_attempt(
                    attempt["id"],
                    "rejected",
                    diagnostics={"reason": "invalid_srt"},
                    lease_owner=context.lease_owner,
                )
            )
        return self._raise_resume_attention(job_id, updated, context)

    def _raise_resume_attention(
        self,
        job_id: str,
        candidate: dict[str, Any],
        context: _ExecutionContext,
    ) -> dict[str, Any]:
        context.check()
        stage = self._selection_stage(job_id)
        if StageState(stage["state"]) is StageState.COMPLETED:
            job = self._job(job_id)
            if JobState(job["state"]) is JobState.COMPLETED:
                context.require(
                    self.store.transition_job(
                        job_id,
                        JobState.QUEUED,
                        expected_state=JobState.COMPLETED,
                        lease_owner=context.lease_owner,
                    )
                )
                context.require(
                    self.store.transition_job(
                        job_id,
                        JobState.RUNNING,
                        expected_state=JobState.QUEUED,
                        lease_owner=context.lease_owner,
                    )
                )
            context.require(
                self.store.transition_stage(
                    job_id,
                    "subtitle_selection",
                    StageState.QUEUED,
                    trigger=AttemptTrigger.ARTIFACT_INVALIDATION,
                    expected_state=StageState.COMPLETED,
                    lease_owner=context.lease_owner,
                )
            )
            context.require(
                self.store.transition_stage(
                    job_id,
                    "subtitle_selection",
                    StageState.RUNNING,
                    expected_state=StageState.QUEUED,
                    lease_owner=context.lease_owner,
                )
            )
            stage = self._selection_stage(job_id)
        context.check()
        context.require(
            self.store.record_event(
                job_id,
                event_type="subtitle_candidate_rejected",
                message="A validated subtitle candidate could not be resumed.",
                severity="warning",
                data={"candidate_id": candidate["id"]},
                lease_owner=context.lease_owner,
            )
        )
        if StageState(stage["state"]) is StageState.RUNNING:
            self._exhaust(
                job_id,
                attempted=1,
                limit=min(3, self.settings.subtitle_candidates_per_cycle),
                context=context,
            )
        raise AttentionRequired(
            "A subtitle candidate needs operator attention.",
            code="subtitle_candidate_invalid",
            actions=_EXHAUSTION_ACTIONS,
        )

    def _complete_validated(
        self,
        job_id: str,
        candidate: dict[str, Any],
        path: Path,
        attempt: dict[str, Any] | None,
        context: _ExecutionContext,
    ) -> dict[str, Any]:
        """Complete durable promotion before marking a candidate selected."""
        context.check()
        attempt = attempt or self._active_candidate_attempt(job_id, candidate["id"])
        job = self._job(job_id)
        if job["source_imdb_id"]:
            self.cache.store(
                job["source_imdb_id"],
                path,
                replace=True,
                publish_allowed=context.publication_allowed,
            )
            context.check()
        if not any(
            event["type"] == "subtitle_selected"
            and event["data"].get("candidate_id") == candidate["id"]
            for event in self.store.list_events(job_id)
        ):
            context.require(
                self.store.record_event(
                    job_id,
                    event_type="subtitle_selected",
                    message="A subtitle candidate was selected.",
                    data={
                        "candidate_id": candidate["id"],
                        "selection_method": candidate["selection_method"],
                    },
                    lease_owner=context.lease_owner,
                )
            )
        if candidate["selection_method"] == "manual":
            context.require(
                self.store.record_decision(
                    job_id,
                    "select_subtitle",
                    candidate_id=candidate["id"],
                    accepted=True,
                    reason=(
                        "Manual subtitle selection accepted a parsed threshold override."
                    ),
                    idempotency_key=f"subtitle-selected:{candidate['id']}",
                    lease_owner=context.lease_owner,
                )
            )
        stage = self._selection_stage(job_id)
        if StageState(stage["state"]) is StageState.RUNNING:
            context.require(
                self.store.transition_stage(
                    job_id,
                    "subtitle_selection",
                    StageState.COMPLETED,
                    expected_state=StageState.RUNNING,
                    lease_owner=context.lease_owner,
                )
            )
        elif StageState(stage["state"]) is not StageState.COMPLETED:
            raise RuntimeError("Subtitle selection stage cannot be completed")
        if attempt is not None:
            context.require(
                self.store.finish_attempt(
                    attempt["id"],
                    "completed",
                    output={"candidate_id": candidate["id"]},
                    lease_owner=context.lease_owner,
                )
            )
        return context.require(
            self.store.update_candidate(
                candidate["id"],
                lease_owner=context.lease_owner,
                status="selected",
                selected_at=datetime.now(UTC).isoformat(),
            )
        )

    def _selected_contract_valid(
        self,
        job_id: str,
        candidate: dict[str, Any],
        context: _ExecutionContext,
    ) -> bool:
        context.check()
        stored = self.store.get_candidate(candidate["id"], include_internal=True)
        if stored is None or not stored.get("artifact_path"):
            return False
        path = Path(stored["artifact_path"])
        job = self._job(job_id)
        cache_path = (
            self.cache.has(job["source_imdb_id"]) if job["source_imdb_id"] else None
        )
        artifact_valid = (
            path.exists()
            and candidate["content_hash"] == _hash(path)
            and StageState(self._selection_stage(job_id)["state"])
            is StageState.COMPLETED
        )
        cache_valid = (
            cache_path is not None and cache_path.read_bytes() == path.read_bytes()
            if job["source_imdb_id"]
            else True
        )
        context.check()
        return artifact_valid and cache_valid

    def _active_candidate_attempt(
        self, job_id: str, candidate_id: str
    ) -> dict[str, Any] | None:
        return next(
            (
                attempt
                for attempt in reversed(self.store.get_job_detail(job_id)["attempts"])
                if attempt["candidate_id"] == candidate_id
                and attempt["finished_at"] is None
            ),
            None,
        )

    def _candidate_workspace(
        self, job_id: str, candidate_id: str
    ) -> tuple[Path, Path, Path]:
        destination = confined_path(self._root, job_id, candidate_id, "subtitle.srt")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging_directory = destination.parent / (
            f".execution.{uuid.uuid4().hex}.partial"
        )
        staging_directory.mkdir()
        return destination, staging_directory, staging_directory / "subtitle.srt"

    def _selection_stage(self, job_id: str) -> dict[str, Any]:
        return next(
            stage
            for stage in self.store.get_job_detail(job_id)["stages"]
            if stage["name"] == "subtitle_selection"
        )

    def _start_selection(self, job_id: str, context: _ExecutionContext) -> None:
        context.check()
        job = self._job(job_id)
        state = JobState(job["state"])
        if state in {JobState.NEEDS_ATTENTION, JobState.FAILED, JobState.CANCELLED}:
            context.require(
                self.store.transition_job(
                    job_id,
                    JobState.QUEUED,
                    trigger=AttemptTrigger.RESUME,
                    lease_owner=context.lease_owner,
                )
            )
            state = JobState.QUEUED
        if state is JobState.QUEUED:
            context.require(
                self.store.transition_job(
                    job_id,
                    JobState.RUNNING,
                    expected_state=JobState.QUEUED,
                    lease_owner=context.lease_owner,
                )
            )
        stage = context.require(
            self.store.ensure_stage(
                job_id,
                "subtitle_selection",
                ordinal=4,
                state=StageState.PENDING,
                max_auto_attempts=3,
                lease_owner=context.lease_owner,
            )
        )
        stage_state = StageState(stage["state"])
        if stage_state in {StageState.NEEDS_ATTENTION, StageState.FAILED}:
            stage = context.require(
                self.store.transition_stage(
                    job_id,
                    "subtitle_selection",
                    StageState.QUEUED,
                    trigger=AttemptTrigger.RESUME,
                    lease_owner=context.lease_owner,
                )
            )
            stage_state = StageState(stage["state"])
        if stage_state is StageState.PENDING:
            stage = context.require(
                self.store.transition_stage(
                    job_id,
                    "subtitle_selection",
                    StageState.QUEUED,
                    lease_owner=context.lease_owner,
                )
            )
            stage_state = StageState(stage["state"])
        if stage_state is StageState.QUEUED:
            context.require(
                self.store.transition_stage(
                    job_id,
                    "subtitle_selection",
                    StageState.RUNNING,
                    expected_state=StageState.QUEUED,
                    lease_owner=context.lease_owner,
                )
            )

    def _exhaust(
        self,
        job_id: str,
        *,
        attempted: int,
        limit: int,
        context: _ExecutionContext,
    ) -> None:
        context.check()
        context.require(
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
                lease_owner=context.lease_owner,
            )
        )
        context.require(
            self.store.transition_job(
                job_id,
                JobState.NEEDS_ATTENTION,
                expected_state=JobState.RUNNING,
                safe_error_code="subtitle_candidates_exhausted",
                safe_error_message=_exhaustion_message(attempted),
                next_action="select_subtitle",
                lease_owner=context.lease_owner,
            )
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
