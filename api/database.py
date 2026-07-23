"""Versioned SQLite persistence and the transactional operations store."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from api.domain import (
    AttemptTrigger,
    JobState,
    StageState,
    assert_job_transition,
    assert_stage_transition,
)
from api.errors import sanitize_text
from api.migrations import apply_migrations
from src.publishing.errors import normalized_remote_id as _normalized_remote_id

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "slur_meter.db"
_ACTIVE_STATES = (JobState.QUEUED.value, JobState.RUNNING.value)
_ABSOLUTE_INTERNAL_PATH_RE = re.compile(
    r"(?<![:/A-Za-z0-9_])/(?:[^/\s]+/)*[^\s,;\"']+|"
    r"\b[A-Za-z]:\\(?:[^\\\s]+\\)*[^\s,;\"']+"
)


def utc_now() -> datetime:
    """Return an aware UTC timestamp; injectable clocks use this contract."""
    return datetime.now(UTC)


class OperationStore:
    """Transactional persistence boundary for runs, stages, attempts, and history."""

    def __init__(
        self,
        path: str | Path,
        clock: Callable[[], datetime | str] = utc_now,
    ) -> None:
        self.path = Path(path)
        self.clock = clock

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            apply_migrations(connection, self._now_text())

    def schema_versions(self) -> list[int]:
        with self._connection() as connection:
            return [
                int(row[0])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]

    def foreign_key_violations(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute("PRAGMA foreign_key_check").fetchall()
            return [
                {
                    "table": row[0],
                    "rowid": row[1],
                    "parent": row[2],
                    "foreign_key": row[3],
                }
                for row in rows
            ]

    def create_or_get_active_job(
        self,
        source_imdb_id: str,
        normalized_query: str,
        label: str,
    ) -> tuple[dict[str, Any], bool]:
        source = str(source_imdb_id or "").strip()
        query = _normalize_query(normalized_query)
        if not source and not query:
            raise ValueError("A source IMDb ID or normalized query is required")
        submission_key = f"imdb:{source.lower()}" if source else f"query:{query}"
        now = self._now_text()
        with self._mutation() as connection:
            row = connection.execute(
                """SELECT * FROM job_runs
                   WHERE submission_key = ? AND state IN (?, ?)
                   ORDER BY created_at DESC LIMIT 1""",
                (submission_key, *_ACTIVE_STATES),
            ).fetchone()
            if row is not None:
                return self._job_dto(row), False

            job_id = self._new_id("job")
            connection.execute(
                """INSERT INTO job_runs (
                       id, source_imdb_id, normalized_query, submission_key, label,
                       state, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (
                    job_id,
                    source or None,
                    query,
                    submission_key,
                    str(label).strip() or source or query,
                    now,
                    now,
                ),
            )
            self._insert_event(
                connection,
                job_id,
                event_type="job_queued",
                message="Run was queued.",
                created_at=now,
            )
            row = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (job_id,)
            ).fetchone()
            return self._job_dto(row), True

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            resolved = self._resolve_job_id(connection, job_id)
            if resolved is None:
                return None
            row = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            return self._job_dto(row)

    def list_jobs(
        self,
        *,
        state: JobState | str | None = None,
        limit: int = 100,
        offset: int = 0,
        query: str | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        conditions: list[str] = []
        parameters: list[Any] = []
        if state is not None:
            conditions.append("state = ?")
            parameters.append(_enum_value(JobState, state))
        if query:
            conditions.append(
                "(lower(label) LIKE ? OR lower(normalized_query) LIKE ? OR lower(COALESCE(source_imdb_id, '')) LIKE ?)"
            )
            needle = f"%{str(query).strip().lower()}%"
            parameters.extend((needle, needle, needle))
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM job_runs{where}", parameters
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""SELECT * FROM job_runs{where}
                    ORDER BY updated_at DESC, created_at DESC, id DESC LIMIT ? OFFSET ?""",
                (*parameters, limit, offset),
            ).fetchall()
            return {
                "items": [self._job_dto(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def job_state_counts(self) -> dict[str, int]:
        """Return truthful queue counts without materializing a bounded page."""
        with self._connection() as connection:
            return {
                str(row["state"]): int(row["count"])
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM job_runs GROUP BY state"
                )
            }

    def list_completed_jobs(self) -> list[dict[str, Any]]:
        """Return all completed public job DTOs for compatibility ranking."""
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM job_runs WHERE state = 'completed'
                   ORDER BY updated_at DESC, id DESC"""
            ).fetchall()
            return [self._job_dto(row) for row in rows]

    def list_attention_jobs(self, *, limit: int) -> dict[str, Any]:
        """Return a bounded attention page with an unbounded truthful total."""
        bounded = max(1, min(int(limit), 200))
        with self._connection() as connection:
            total = int(
                connection.execute(
                    """SELECT COUNT(*) FROM job_runs
                       WHERE state IN ('failed', 'needs_attention')"""
                ).fetchone()[0]
            )
            rows = connection.execute(
                """SELECT * FROM job_runs
                   WHERE state IN ('failed', 'needs_attention')
                   ORDER BY updated_at DESC, id DESC LIMIT ?""",
                (bounded,),
            ).fetchall()
            return {"items": [self._job_dto(row) for row in rows], "total": total}

    def get_job_detail(self, job_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            resolved = self._resolve_job_id(connection, job_id)
            if resolved is None:
                return None
            row = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            return {
                "run": self._job_dto(row),
                "stages": [
                    self._stage_dto(item)
                    for item in connection.execute(
                        "SELECT * FROM pipeline_stages WHERE job_id = ? ORDER BY ordinal, id",
                        (resolved,),
                    )
                ],
                "attempts": [
                    self._attempt_dto(item)
                    for item in connection.execute(
                        "SELECT * FROM pipeline_attempts WHERE job_id = ? ORDER BY id",
                        (resolved,),
                    )
                ],
                "candidates": self._candidate_rows(connection, resolved),
                "events": self._event_rows(connection, resolved),
                "decisions": self._decision_rows(connection, resolved),
                "publishing_attempts": self._publishing_rows(connection, resolved),
                "costs": self._cost_rows(connection, resolved),
                "releases": self._release_rows(connection, resolved),
                "revenue": self._revenue_rows(connection, resolved),
                "server_time": self._now_text(),
            }

    def transition_job(
        self,
        job_id: str,
        new_state: JobState | str,
        *,
        expected_state: JobState | str | None = None,
        trigger: AttemptTrigger | str | None = None,
        next_action: str | None = None,
        safe_error_code: str | None = None,
        safe_error_message: object | None = None,
        retryable: bool = False,
        lease_owner: str | None = None,
        additional_event_type: str | None = None,
        additional_event_message: object = "",
        additional_event_data: Mapping[str, Any] | None = None,
        additional_event_stage_name: str | None = None,
    ) -> dict[str, Any] | None:
        target = JobState(_enum_value(JobState, new_state))
        transition_trigger = _optional_trigger(trigger)
        now = self._now_text()
        with self._mutation() as connection:
            resolved = self._resolve_job_id(connection, job_id)
            if resolved is None:
                return None
            row = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            if not self._lease_allows(row, lease_owner, now):
                return None
            old = JobState(row["state"])
            if expected_state is not None and old is not JobState(
                _enum_value(JobState, expected_state)
            ):
                return None
            if old is target:
                return self._job_dto(row)
            assert_job_transition(old, target, transition_trigger)
            started_at = row["started_at"] or (
                now if target is JobState.RUNNING else None
            )
            finished_at = (
                now
                if target in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
                else None
            )
            clear_lease = target is not JobState.RUNNING
            cursor = connection.execute(
                """UPDATE job_runs SET
                       state = ?, updated_at = ?, started_at = ?, finished_at = ?,
                       next_action = ?, safe_error_code = ?, safe_error_message = ?,
                       error_retryable = ?,
                       lease_owner = CASE WHEN ? THEN NULL ELSE lease_owner END,
                       lease_expires_at = CASE WHEN ? THEN NULL ELSE lease_expires_at END
                   WHERE id = ? AND state = ?""",
                (
                    target.value,
                    now,
                    started_at,
                    finished_at,
                    _safe_text(next_action),
                    _safe_text(safe_error_code),
                    _safe_text(safe_error_message),
                    int(bool(retryable)),
                    clear_lease,
                    clear_lease,
                    resolved,
                    old.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
            if target is JobState.CANCELLED:
                self._cancel_job_work(connection, resolved, now)
            self._insert_event(
                connection,
                resolved,
                event_type="job_state_changed",
                message=f"Run moved from {old.value} to {target.value}.",
                data={
                    "from": old.value,
                    "to": target.value,
                    "trigger": transition_trigger.value if transition_trigger else None,
                },
                created_at=now,
            )
            if additional_event_type is not None:
                additional_stage_id = (
                    self._stage_id(connection, resolved, additional_event_stage_name)
                    if additional_event_stage_name
                    else None
                )
                self._insert_event(
                    connection,
                    resolved,
                    stage_id=additional_stage_id,
                    event_type=additional_event_type,
                    message=additional_event_message,
                    data=additional_event_data,
                    created_at=now,
                )
            updated = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            return self._job_dto(updated)

    def claim_next_job(
        self, owner: str, *, lease_seconds: float
    ) -> dict[str, Any] | None:
        if not str(owner).strip():
            raise ValueError("A lease owner is required")
        if lease_seconds <= 0:
            raise ValueError("Lease duration must be positive")
        now_dt = self._now_datetime()
        now = now_dt.isoformat()
        expires = (now_dt + timedelta(seconds=float(lease_seconds))).isoformat()
        with self._mutation() as connection:
            row = connection.execute(
                """SELECT * FROM job_runs
                   WHERE state = 'queued' AND cancel_requested_at IS NULL
                   ORDER BY created_at, id LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            assert_job_transition(JobState.QUEUED, JobState.RUNNING)
            cursor = connection.execute(
                """UPDATE job_runs SET state = 'running', lease_owner = ?,
                       lease_expires_at = ?, started_at = COALESCE(started_at, ?),
                       updated_at = ?
                   WHERE id = ? AND state = 'queued' AND cancel_requested_at IS NULL""",
                (str(owner), expires, now, now, row["id"]),
            )
            if cursor.rowcount != 1:
                return None
            self._insert_event(
                connection,
                row["id"],
                event_type="job_claimed",
                message="Run was claimed by a worker.",
                created_at=now,
            )
            claimed = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (row["id"],)
            ).fetchone()
            return self._job_dto(claimed)

    def transition_stage_and_job(
        self,
        job_id: str,
        stage_name: str,
        stage_state: StageState | str,
        job_state: JobState | str,
        *,
        expected_stage_state: StageState | str = StageState.RUNNING,
        expected_job_state: JobState | str = JobState.RUNNING,
        warnings: list[str] | None = None,
        output_manifest: Mapping[str, Any] | None = None,
        progress_unit: str | None = None,
        safe_error_code: str | None = None,
        safe_error_message: object | None = None,
        retryable: bool = False,
        next_action: str | None = None,
        reset_descendants: bool = False,
        lease_owner: str | None = None,
    ) -> dict[str, Any] | None:
        """Commit a stage and its owning run outcome as one fenced transaction."""
        target_stage = StageState(_enum_value(StageState, stage_state))
        target_job = JobState(_enum_value(JobState, job_state))
        expected_stage = StageState(_enum_value(StageState, expected_stage_state))
        expected_job = JobState(_enum_value(JobState, expected_job_state))
        now = self._now_text()
        with self._mutation() as connection:
            resolved = self._resolve_job_id(connection, job_id)
            if resolved is None:
                return None
            job = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            stage = connection.execute(
                "SELECT * FROM pipeline_stages WHERE job_id = ? AND name = ?",
                (resolved, stage_name),
            ).fetchone()
            if stage is None:
                raise KeyError("Stage was not found")
            if not self._lease_allows(job, lease_owner, now):
                return None
            old_stage = StageState(stage["state"])
            old_job = JobState(job["state"])
            if old_stage is not expected_stage or old_job is not expected_job:
                return None

            # Validate both moves before either row changes.
            assert_stage_transition(old_stage, target_stage, stage_name=stage_name)
            assert_job_transition(old_job, target_job)
            if reset_descendants:
                descendants = connection.execute(
                    "SELECT * FROM pipeline_stages WHERE parent_stage_id = ? ORDER BY ordinal, id",
                    (stage["id"],),
                ).fetchall()
                for child in descendants:
                    child_state = StageState(child["state"])
                    connection.execute(
                        """UPDATE pipeline_stages SET state = 'pending', updated_at = ?,
                               started_at = NULL, finished_at = NULL,
                               progress_numerator = NULL, progress_denominator = NULL,
                               progress_unit = NULL, warnings_json = '[]',
                               output_manifest_json = '{}', safe_error_code = NULL,
                               safe_error_message = NULL, retryable = 0, next_action = NULL
                           WHERE id = ?""",
                        (now, child["id"]),
                    )
                    if child_state is not StageState.PENDING:
                        self._insert_event(
                            connection,
                            resolved,
                            stage_id=int(child["id"]),
                            event_type="stage_state_changed",
                            message=(
                                f"Stage {child['name']} moved from "
                                f"{child_state.value} to pending."
                            ),
                            data={
                                "from": child_state.value,
                                "to": StageState.PENDING.value,
                                "trigger": None,
                            },
                            created_at=now,
                        )
            stage_finished = (
                now
                if target_stage
                in {
                    StageState.COMPLETED,
                    StageState.FAILED,
                    StageState.CANCELLED,
                    StageState.SKIPPED,
                    StageState.NEEDS_ATTENTION,
                }
                else None
            )
            job_finished = (
                now
                if target_job
                in {
                    JobState.COMPLETED,
                    JobState.FAILED,
                    JobState.CANCELLED,
                }
                else None
            )
            connection.execute(
                """UPDATE pipeline_stages SET state = ?, updated_at = ?, finished_at = ?,
                       progress_unit = COALESCE(?, progress_unit), warnings_json = ?,
                       output_manifest_json = ?, safe_error_code = ?, safe_error_message = ?,
                       retryable = ?, next_action = ? WHERE id = ? AND state = ?""",
                (
                    target_stage.value,
                    now,
                    stage_finished,
                    _safe_text(progress_unit) if progress_unit is not None else None,
                    _safe_json_dump(warnings)
                    if warnings is not None
                    else stage["warnings_json"],
                    _safe_json_dump(output_manifest)
                    if output_manifest is not None
                    else stage["output_manifest_json"],
                    _safe_text(safe_error_code),
                    _safe_text(safe_error_message),
                    int(bool(retryable)),
                    _safe_text(next_action),
                    stage["id"],
                    old_stage.value,
                ),
            )
            connection.execute(
                """UPDATE job_runs SET state = ?, current_stage = ?, updated_at = ?,
                       finished_at = ?, next_action = ?, safe_error_code = ?,
                       safe_error_message = ?, error_retryable = ?, lease_owner = NULL,
                       lease_expires_at = NULL WHERE id = ? AND state = ?""",
                (
                    target_job.value,
                    stage_name,
                    now,
                    job_finished,
                    _safe_text(next_action),
                    _safe_text(safe_error_code),
                    _safe_text(safe_error_message),
                    int(bool(retryable)),
                    resolved,
                    old_job.value,
                ),
            )
            self._insert_event(
                connection,
                resolved,
                stage_id=int(stage["id"]),
                event_type="stage_state_changed",
                message=f"Stage {stage_name} moved from {old_stage.value} to {target_stage.value}.",
                data={
                    "from": old_stage.value,
                    "to": target_stage.value,
                    "trigger": None,
                },
                created_at=now,
            )
            self._insert_event(
                connection,
                resolved,
                event_type="job_state_changed",
                message=f"Run moved from {old_job.value} to {target_job.value}.",
                data={"from": old_job.value, "to": target_job.value, "trigger": None},
                created_at=now,
            )
            updated_stage = connection.execute(
                "SELECT * FROM pipeline_stages WHERE id = ?", (stage["id"],)
            ).fetchone()
            updated_job = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            return {
                "stage": self._stage_dto(updated_stage),
                "run": self._job_dto(updated_job),
            }

    def complete_stage_and_children(
        self,
        job_id: str,
        stage_name: str,
        *,
        warnings: list[str] | None = None,
        output_manifest: Mapping[str, Any] | None = None,
        progress_unit: str | None = None,
        lease_owner: str | None = None,
    ) -> dict[str, Any] | None:
        """Atomically complete a running parent and all of its running children."""
        now = self._now_text()
        with self._mutation() as connection:
            resolved = self._resolve_job_id(connection, job_id)
            if resolved is None:
                return None
            job = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            parent = connection.execute(
                "SELECT * FROM pipeline_stages WHERE job_id = ? AND name = ?",
                (resolved, stage_name),
            ).fetchone()
            if parent is None:
                raise KeyError("Stage was not found")
            if not self._lease_allows(job, lease_owner, now):
                return None
            if (
                JobState(job["state"]) is not JobState.RUNNING
                or StageState(parent["state"]) is not StageState.RUNNING
            ):
                return None
            children = connection.execute(
                "SELECT * FROM pipeline_stages WHERE parent_stage_id = ? ORDER BY ordinal, id",
                (parent["id"],),
            ).fetchall()
            if not children or any(
                StageState(child["state"]) is not StageState.RUNNING
                or not _json_load(child["output_manifest_json"], {})
                for child in children
            ):
                return None

            for child in children:
                assert_stage_transition(
                    StageState.RUNNING,
                    StageState.COMPLETED,
                    stage_name=child["name"],
                )
                connection.execute(
                    """UPDATE pipeline_stages SET state = 'completed', updated_at = ?,
                           finished_at = ?, safe_error_code = NULL,
                           safe_error_message = NULL, retryable = 0, next_action = NULL
                       WHERE id = ? AND state = 'running'""",
                    (now, now, child["id"]),
                )
                self._insert_event(
                    connection,
                    resolved,
                    stage_id=int(child["id"]),
                    event_type="stage_state_changed",
                    message=f"Stage {child['name']} moved from running to completed.",
                    data={"from": "running", "to": "completed", "trigger": None},
                    created_at=now,
                )

            assert_stage_transition(
                StageState.RUNNING,
                StageState.COMPLETED,
                stage_name=stage_name,
            )
            connection.execute(
                """UPDATE pipeline_stages SET state = 'completed', updated_at = ?,
                       finished_at = ?, progress_unit = COALESCE(?, progress_unit),
                       warnings_json = ?, output_manifest_json = ?,
                       safe_error_code = NULL, safe_error_message = NULL,
                       retryable = 0, next_action = NULL
                   WHERE id = ? AND state = 'running'""",
                (
                    now,
                    now,
                    _safe_text(progress_unit) if progress_unit is not None else None,
                    _safe_json_dump(warnings)
                    if warnings is not None
                    else parent["warnings_json"],
                    _safe_json_dump(output_manifest)
                    if output_manifest is not None
                    else parent["output_manifest_json"],
                    parent["id"],
                ),
            )
            connection.execute(
                "UPDATE job_runs SET current_stage = ?, updated_at = ? WHERE id = ?",
                (stage_name, now, resolved),
            )
            self._insert_event(
                connection,
                resolved,
                stage_id=int(parent["id"]),
                event_type="stage_state_changed",
                message=f"Stage {stage_name} moved from running to completed.",
                data={"from": "running", "to": "completed", "trigger": None},
                created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM pipeline_stages WHERE id = ?", (parent["id"],)
            ).fetchone()
            return self._stage_dto(updated)

    def release_job_lease(self, job_id: str, owner: str) -> bool:
        """Release owned interrupted work for immediate restart recovery."""
        now = self._now_text()
        with self._mutation() as connection:
            resolved = self._resolve_job_id(connection, job_id)
            if resolved is None:
                return False
            job = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            if job["state"] != JobState.RUNNING.value or job["lease_owner"] != str(
                owner
            ):
                return False
            if job["cancel_requested_at"] is not None:
                self._cancel_job_work(connection, resolved, now)
                connection.execute(
                    """UPDATE job_runs SET state = 'cancelled', lease_owner = NULL,
                           lease_expires_at = NULL, updated_at = ?, finished_at = ?,
                           next_action = NULL WHERE id = ?""",
                    (now, now, resolved),
                )
                return True

            running_stages = connection.execute(
                "SELECT * FROM pipeline_stages WHERE job_id = ? AND state = 'running'",
                (resolved,),
            ).fetchall()
            for stage in running_stages:
                assert_stage_transition(
                    StageState.RUNNING,
                    StageState.QUEUED,
                    AttemptTrigger.RESTART_RECOVERY,
                )
                connection.execute(
                    """UPDATE pipeline_attempts SET finished_at = ?, outcome = 'interrupted',
                           retryable = 1, diagnostics_json = ?
                       WHERE stage_id = ? AND finished_at IS NULL""",
                    (
                        now,
                        _safe_json_dump(
                            {"reason": "Worker shutdown interrupted the attempt."}
                        ),
                        stage["id"],
                    ),
                )
                connection.execute(
                    """UPDATE pipeline_stages SET state = 'queued', retry_cycle = retry_cycle + 1,
                           updated_at = ?, finished_at = NULL, retryable = 1,
                           safe_error_code = 'restart_recovery',
                           safe_error_message = 'Interrupted work was safely queued for restart.',
                           next_action = 'resume' WHERE id = ?""",
                    (now, stage["id"]),
                )
            assert_job_transition(
                JobState.RUNNING, JobState.QUEUED, AttemptTrigger.RESTART_RECOVERY
            )
            connection.execute(
                """UPDATE job_runs SET state = 'queued', lease_owner = NULL,
                       lease_expires_at = NULL, updated_at = ?, next_action = 'resume',
                       safe_error_code = 'restart_recovery',
                       safe_error_message = 'Interrupted work was safely queued for restart.',
                       error_retryable = 1 WHERE id = ?""",
                (now, resolved),
            )
            self._insert_event(
                connection,
                resolved,
                event_type="shutdown_recovery",
                severity="warning",
                message="Owned work was interrupted during shutdown and queued for restart.",
                data={"trigger": AttemptTrigger.RESTART_RECOVERY.value},
                created_at=now,
            )
            return True

    def renew_lease(self, job_id: str, owner: str, *, lease_seconds: float) -> bool:
        if lease_seconds <= 0:
            raise ValueError("Lease duration must be positive")
        now_dt = self._now_datetime()
        with self._mutation() as connection:
            resolved = self._resolve_job_id(connection, job_id)
            if resolved is None:
                return False
            cursor = connection.execute(
                """UPDATE job_runs SET lease_expires_at = ?, updated_at = ?
                   WHERE id = ? AND state = 'running' AND lease_owner = ?
                     AND lease_expires_at > ?""",
                (
                    (now_dt + timedelta(seconds=float(lease_seconds))).isoformat(),
                    now_dt.isoformat(),
                    resolved,
                    str(owner),
                    now_dt.isoformat(),
                ),
            )
            return cursor.rowcount == 1

    def recover_expired_leases(self) -> list[str]:
        now = self._now_text()
        recovered: list[str] = []
        with self._mutation() as connection:
            rows = connection.execute(
                """SELECT * FROM job_runs
                   WHERE state = 'running' AND lease_expires_at IS NOT NULL
                     AND lease_expires_at <= ? ORDER BY id""",
                (now,),
            ).fetchall()
            for row in rows:
                cancellation_pending = row["cancel_requested_at"] is not None
                target_job_state = (
                    JobState.CANCELLED if cancellation_pending else JobState.QUEUED
                )
                assert_job_transition(
                    JobState.RUNNING,
                    target_job_state,
                    None if cancellation_pending else AttemptTrigger.RESTART_RECOVERY,
                )
                running_stages = connection.execute(
                    "SELECT * FROM pipeline_stages WHERE job_id = ? AND state = 'running' ORDER BY id",
                    (row["id"],),
                ).fetchall()
                for stage in running_stages:
                    target_stage_state = (
                        StageState.CANCELLED
                        if cancellation_pending
                        else StageState.QUEUED
                    )
                    assert_stage_transition(
                        StageState.RUNNING,
                        target_stage_state,
                        None
                        if cancellation_pending
                        else AttemptTrigger.RESTART_RECOVERY,
                    )
                    connection.execute(
                        """UPDATE pipeline_attempts SET finished_at = ?, outcome = ?,
                               retryable = ?, diagnostics_json = ?
                           WHERE stage_id = ? AND finished_at IS NULL""",
                        (
                            now,
                            "cancelled" if cancellation_pending else "interrupted",
                            0 if cancellation_pending else 1,
                            _safe_json_dump(
                                {
                                    "reason": (
                                        "Pending cancellation was applied after the worker lease expired."
                                        if cancellation_pending
                                        else "Worker lease expired; attempt recovered after restart."
                                    )
                                }
                            ),
                            stage["id"],
                        ),
                    )
                    if cancellation_pending:
                        connection.execute(
                            """UPDATE pipeline_stages SET state = 'cancelled',
                                   updated_at = ?, finished_at = ?, retryable = 0,
                                   safe_error_code = NULL, safe_error_message = NULL,
                                   next_action = NULL
                               WHERE id = ? AND state = 'running'""",
                            (now, now, stage["id"]),
                        )
                    else:
                        connection.execute(
                            """UPDATE pipeline_stages SET state = 'queued', retry_cycle = retry_cycle + 1,
                               updated_at = ?, finished_at = NULL, retryable = 1,
                               safe_error_code = 'restart_recovery',
                               safe_error_message = 'Interrupted work was safely queued for restart.',
                               next_action = 'resume'
                           WHERE id = ? AND state = 'running'""",
                            (now, stage["id"]),
                        )
                if cancellation_pending:
                    cursor = connection.execute(
                        """UPDATE job_runs SET state = 'cancelled', lease_owner = NULL,
                               lease_expires_at = NULL, updated_at = ?, finished_at = ?,
                               next_action = NULL, safe_error_code = NULL,
                               safe_error_message = NULL, error_retryable = 0
                           WHERE id = ? AND state = 'running' AND lease_expires_at <= ?""",
                        (now, now, row["id"], now),
                    )
                else:
                    cursor = connection.execute(
                        """UPDATE job_runs SET state = 'queued', lease_owner = NULL,
                           lease_expires_at = NULL, updated_at = ?, next_action = 'resume',
                           safe_error_code = 'restart_recovery',
                           safe_error_message = 'Interrupted work was safely queued for restart.',
                           error_retryable = 1
                       WHERE id = ? AND state = 'running' AND lease_expires_at <= ?""",
                        (now, row["id"], now),
                    )
                if cursor.rowcount != 1:
                    continue
                if cancellation_pending:
                    self._cancel_job_work(connection, row["id"], now)
                self._insert_event(
                    connection,
                    row["id"],
                    event_type=(
                        "cancellation_applied"
                        if cancellation_pending
                        else "restart_recovery"
                    ),
                    severity="warning",
                    message=(
                        "Pending cancellation was applied after the worker lease expired."
                        if cancellation_pending
                        else "An expired worker lease was recovered and the run was requeued."
                    ),
                    data={
                        "trigger": (
                            "cancel"
                            if cancellation_pending
                            else AttemptTrigger.RESTART_RECOVERY.value
                        )
                    },
                    created_at=now,
                )
                recovered.append(str(row["id"]))
        return recovered

    def request_cancel(self, job_id: str) -> tuple[dict[str, Any], bool]:
        now = self._now_text()
        with self._mutation() as connection:
            resolved = self._require_job_id(connection, job_id)
            row = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            state = JobState(row["state"])
            if state is JobState.CANCELLED or row["cancel_requested_at"] is not None:
                return self._job_dto(row), False
            if state is JobState.COMPLETED:
                return self._job_dto(row), False
            if state is JobState.RUNNING:
                connection.execute(
                    "UPDATE job_runs SET cancel_requested_at = ?, updated_at = ?, next_action = 'cancel_pending' WHERE id = ?",
                    (now, now, resolved),
                )
            else:
                assert_job_transition(state, JobState.CANCELLED)
                connection.execute(
                    """UPDATE job_runs SET state = 'cancelled', cancel_requested_at = ?,
                           updated_at = ?, finished_at = ?, next_action = NULL,
                           lease_owner = NULL, lease_expires_at = NULL WHERE id = ?""",
                    (now, now, now, resolved),
                )
                self._cancel_job_work(connection, resolved, now)
            self._insert_event(
                connection,
                resolved,
                event_type="cancel_requested",
                severity="warning",
                message="Cancellation was requested.",
                created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            return self._job_dto(updated), True

    def ensure_stage(
        self,
        job_id: str,
        name: str,
        *,
        ordinal: int = 0,
        parent_name: str | None = None,
        state: StageState | str = StageState.PENDING,
        max_auto_attempts: int = 1,
        lease_owner: str | None = None,
    ) -> dict[str, Any] | None:
        stage_state = _enum_value(StageState, state)
        now = self._now_text()
        with self._mutation() as connection:
            resolved = self._require_job_id(connection, job_id)
            job = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            if not self._lease_allows(job, lease_owner, now):
                return None
            parent_id = None
            if parent_name is not None:
                parent = connection.execute(
                    "SELECT id FROM pipeline_stages WHERE job_id = ? AND name = ?",
                    (resolved, parent_name),
                ).fetchone()
                if parent is None:
                    raise KeyError("Parent stage was not found")
                parent_id = parent["id"]
            connection.execute(
                """INSERT INTO pipeline_stages
                   (job_id, name, parent_stage_id, ordinal, state, max_auto_attempts, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id, name) DO UPDATE SET
                     parent_stage_id = COALESCE(pipeline_stages.parent_stage_id, excluded.parent_stage_id),
                     ordinal = excluded.ordinal,
                     max_auto_attempts = excluded.max_auto_attempts""",
                (
                    resolved,
                    str(name),
                    parent_id,
                    int(ordinal),
                    stage_state,
                    int(max_auto_attempts),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM pipeline_stages WHERE job_id = ? AND name = ?",
                (resolved, str(name)),
            ).fetchone()
            return self._stage_dto(row)

    def transition_stage(
        self,
        job_id: str,
        stage_name: str,
        new_state: StageState | str,
        *,
        expected_state: StageState | str | None = None,
        trigger: AttemptTrigger | str | None = None,
        progress_numerator: int | None = None,
        progress_denominator: int | None = None,
        progress_unit: str | None = None,
        warnings: list[str] | None = None,
        output_manifest: Mapping[str, Any] | None = None,
        safe_error_code: str | None = None,
        safe_error_message: object | None = None,
        retryable: bool = False,
        next_action: str | None = None,
        lease_owner: str | None = None,
    ) -> dict[str, Any] | None:
        target = StageState(_enum_value(StageState, new_state))
        transition_trigger = _optional_trigger(trigger)
        now = self._now_text()
        with self._mutation() as connection:
            resolved = self._require_job_id(connection, job_id)
            row = connection.execute(
                "SELECT * FROM pipeline_stages WHERE job_id = ? AND name = ?",
                (resolved, stage_name),
            ).fetchone()
            if row is None:
                raise KeyError("Stage was not found")
            job = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            if not self._lease_allows(job, lease_owner, now):
                return None
            old = StageState(row["state"])
            if expected_state is not None and old is not StageState(
                _enum_value(StageState, expected_state)
            ):
                return None
            if old is target:
                has_detail_update = any(
                    value is not None
                    for value in (
                        progress_numerator,
                        progress_denominator,
                        progress_unit,
                        warnings,
                        output_manifest,
                    )
                )
                if not has_detail_update:
                    return self._stage_dto(row)
                connection.execute(
                    """UPDATE pipeline_stages SET updated_at = ?,
                           progress_numerator = COALESCE(?, progress_numerator),
                           progress_denominator = COALESCE(?, progress_denominator),
                           progress_unit = COALESCE(?, progress_unit),
                           warnings_json = ?, output_manifest_json = ?
                       WHERE id = ?""",
                    (
                        now,
                        progress_numerator,
                        progress_denominator,
                        _safe_text(progress_unit)
                        if progress_unit is not None
                        else None,
                        _safe_json_dump(warnings)
                        if warnings is not None
                        else row["warnings_json"],
                        _safe_json_dump(output_manifest)
                        if output_manifest is not None
                        else row["output_manifest_json"],
                        row["id"],
                    ),
                )
                self._insert_event(
                    connection,
                    resolved,
                    stage_id=int(row["id"]),
                    event_type="stage_progress",
                    message=f"Stage {stage_name} progress was updated.",
                    created_at=now,
                )
                updated = connection.execute(
                    "SELECT * FROM pipeline_stages WHERE id = ?", (row["id"],)
                ).fetchone()
                return self._stage_dto(updated)
            assert_stage_transition(
                old, target, transition_trigger, stage_name=stage_name
            )
            started_at = row["started_at"] or (
                now if target is StageState.RUNNING else None
            )
            finished_at = (
                now
                if target
                in {
                    StageState.COMPLETED,
                    StageState.FAILED,
                    StageState.CANCELLED,
                    StageState.SKIPPED,
                    StageState.NEEDS_ATTENTION,
                }
                else None
            )
            retry_cycle = int(row["retry_cycle"])
            if target is StageState.QUEUED and transition_trigger in {
                AttemptTrigger.MANUAL_RETRY,
                AttemptTrigger.RESTART_RECOVERY,
                AttemptTrigger.ARTIFACT_INVALIDATION,
            }:
                retry_cycle += 1
            connection.execute(
                """UPDATE pipeline_stages SET state = ?, retry_cycle = ?, updated_at = ?, started_at = ?,
                       finished_at = ?, progress_numerator = COALESCE(?, progress_numerator),
                       progress_denominator = COALESCE(?, progress_denominator),
                       progress_unit = COALESCE(?, progress_unit), warnings_json = ?,
                       output_manifest_json = ?, safe_error_code = ?, safe_error_message = ?,
                       retryable = ?, next_action = ?
                   WHERE id = ?""",
                (
                    target.value,
                    retry_cycle,
                    now,
                    started_at,
                    finished_at,
                    progress_numerator,
                    progress_denominator,
                    _safe_text(progress_unit),
                    _safe_json_dump(warnings)
                    if warnings is not None
                    else row["warnings_json"],
                    _safe_json_dump(output_manifest)
                    if output_manifest is not None
                    else row["output_manifest_json"],
                    _safe_text(safe_error_code),
                    _safe_text(safe_error_message),
                    int(bool(retryable)),
                    _safe_text(next_action),
                    row["id"],
                ),
            )
            connection.execute(
                "UPDATE job_runs SET current_stage = ?, updated_at = ? WHERE id = ?",
                (stage_name, now, resolved),
            )
            self._insert_event(
                connection,
                resolved,
                stage_id=int(row["id"]),
                event_type="stage_state_changed",
                message=f"Stage {stage_name} moved from {old.value} to {target.value}.",
                data={
                    "from": old.value,
                    "to": target.value,
                    "trigger": transition_trigger.value if transition_trigger else None,
                },
                created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM pipeline_stages WHERE id = ?", (row["id"],)
            ).fetchone()
            return self._stage_dto(updated)

    def start_attempt(
        self,
        job_id: str,
        stage_name: str,
        *,
        trigger: AttemptTrigger | str = AttemptTrigger.AUTOMATIC,
        max_attempts: int | None = None,
        candidate_id: str | None = None,
        lease_owner: str | None = None,
    ) -> dict[str, Any] | None:
        trigger_value = _enum_value(AttemptTrigger, trigger)
        now = self._now_text()
        with self._mutation() as connection:
            resolved = self._require_job_id(connection, job_id)
            stage = connection.execute(
                "SELECT * FROM pipeline_stages WHERE job_id = ? AND name = ?",
                (resolved, stage_name),
            ).fetchone()
            if stage is None:
                raise KeyError("Stage was not found")
            job = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            if not self._lease_allows(job, lease_owner, now):
                return None
            if stage["state"] != StageState.RUNNING.value:
                raise ValueError("Stage must be running before an attempt starts")
            if candidate_id is not None:
                candidate = connection.execute(
                    "SELECT job_id FROM subtitle_candidates WHERE id = ?",
                    (candidate_id,),
                ).fetchone()
                if candidate is None or candidate["job_id"] != resolved:
                    raise ValueError("Subtitle candidate does not belong to this run")
            active = connection.execute(
                """SELECT * FROM pipeline_attempts
                   WHERE stage_id = ? AND finished_at IS NULL
                   ORDER BY id DESC LIMIT 1""",
                (stage["id"],),
            ).fetchone()
            if active is not None:
                if candidate_id is not None and active["candidate_id"] is None:
                    connection.execute(
                        "UPDATE pipeline_attempts SET candidate_id = ? WHERE id = ?",
                        (candidate_id, active["id"]),
                    )
                    active = connection.execute(
                        "SELECT * FROM pipeline_attempts WHERE id = ?",
                        (active["id"],),
                    ).fetchone()
                elif (
                    candidate_id is not None and active["candidate_id"] != candidate_id
                ):
                    raise RuntimeError(
                        "Another subtitle candidate attempt is still active"
                    )
                return self._attempt_dto(active)
            cycle = int(stage["retry_cycle"])
            attempt_number = int(
                connection.execute(
                    """SELECT COALESCE(MAX(attempt_number), 0) + 1
                       FROM pipeline_attempts WHERE stage_id = ? AND retry_cycle = ?""",
                    (stage["id"], cycle),
                ).fetchone()[0]
            )
            cursor = connection.execute(
                """INSERT INTO pipeline_attempts
                   (job_id, stage_id, candidate_id, retry_cycle, attempt_number,
                    max_attempts, trigger, started_at, outcome)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running')""",
                (
                    resolved,
                    stage["id"],
                    candidate_id,
                    cycle,
                    attempt_number,
                    int(max_attempts or stage["max_auto_attempts"]),
                    trigger_value,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM pipeline_attempts WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return self._attempt_dto(row)

    def finish_attempt(
        self,
        attempt_id: int,
        outcome: str,
        *,
        retryable: bool = False,
        diagnostics: Mapping[str, Any] | None = None,
        output: Mapping[str, Any] | None = None,
        lease_owner: str | None = None,
    ) -> dict[str, Any] | None:
        now = self._now_text()
        with self._mutation() as connection:
            row = connection.execute(
                "SELECT * FROM pipeline_attempts WHERE id = ?", (int(attempt_id),)
            ).fetchone()
            if row is None:
                return None
            job = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (row["job_id"],)
            ).fetchone()
            if not self._lease_allows(job, lease_owner, now):
                return None
            if row["finished_at"] is not None:
                return self._attempt_dto(row)
            connection.execute(
                """UPDATE pipeline_attempts SET finished_at = ?, outcome = ?,
                       retryable = ?, diagnostics_json = ?, output_json = ? WHERE id = ?""",
                (
                    now,
                    _safe_text(outcome) or "failed",
                    int(bool(retryable)),
                    _safe_json_dump(diagnostics or {}),
                    _safe_json_dump(output or {}),
                    int(attempt_id),
                ),
            )
            updated = connection.execute(
                "SELECT * FROM pipeline_attempts WHERE id = ?", (int(attempt_id),)
            ).fetchone()
            return self._attempt_dto(updated)

    def record_event(
        self,
        job_id: str,
        *,
        event_type: str,
        message: object = "",
        severity: str = "info",
        stage_name: str | None = None,
        attempt_id: int | None = None,
        data: Mapping[str, Any] | None = None,
        lease_owner: str | None = None,
    ) -> dict[str, Any] | None:
        now = self._now_text()
        with self._mutation() as connection:
            resolved = self._require_job_id(connection, job_id)
            job = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            if not self._lease_allows(job, lease_owner, now):
                return None
            stage_id = (
                self._stage_id(connection, resolved, stage_name) if stage_name else None
            )
            event_id = self._insert_event(
                connection,
                resolved,
                stage_id=stage_id,
                attempt_id=attempt_id,
                event_type=event_type,
                message=message,
                severity=severity,
                data=data,
                created_at=now,
            )
            row = connection.execute(
                "SELECT * FROM pipeline_events WHERE id = ?", (event_id,)
            ).fetchone()
            return self._event_dto(row)

    def list_events(
        self,
        job_id: str,
        *,
        after: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            resolved = self._resolve_job_id(connection, job_id)
            if resolved is None:
                return []
            rows = connection.execute(
                """SELECT * FROM pipeline_events WHERE job_id = ? AND id > ?
                   ORDER BY id LIMIT ?""",
                (resolved, max(0, int(after)), max(1, min(int(limit), 1000))),
            ).fetchall()
            return [self._event_dto(row) for row in rows]

    def record_candidate(
        self,
        job_id: str,
        provider: str,
        provider_id: str,
        *,
        lease_owner: str | None = None,
        **fields: Any,
    ) -> tuple[dict[str, Any], bool] | None:
        now = self._now_text()
        allowed = {
            "provider_filename",
            "source_type",
            "language",
            "fps",
            "title",
            "year",
            "imdb_match",
            "provider_rating",
            "provider_download_count",
            "discovery_cycle",
            "rank",
            "detected_encoding",
            "cue_count",
            "first_cue_seconds",
            "final_cue_seconds",
            "parsed_duration_seconds",
            "expected_runtime_seconds",
            "coverage_percent",
            "download_error",
            "parse_error",
            "status",
            "content_hash",
            "artifact_path",
            "selected_at",
            "selection_method",
        }
        json_fields = {
            "rank_reasons": "rank_reasons_json",
            "quality_reasons": "quality_reasons_json",
            "rejection_reasons": "rejection_reasons_json",
        }
        unknown = set(fields) - allowed - set(json_fields)
        if unknown:
            raise ValueError(f"Unknown candidate fields: {', '.join(sorted(unknown))}")
        cycle = int(fields.get("discovery_cycle", 1))
        with self._mutation() as connection:
            resolved = self._require_job_id(connection, job_id)
            job = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            if not self._lease_allows(job, lease_owner, now):
                return None
            existing = connection.execute(
                """SELECT * FROM subtitle_candidates
                   WHERE job_id = ? AND provider = ? AND provider_id = ? AND discovery_cycle = ?""",
                (resolved, provider, provider_id, cycle),
            ).fetchone()
            if existing is not None:
                return self._candidate_dto(existing), False
            values: dict[str, Any] = {
                key: fields[key] for key in allowed if key in fields
            }
            for public_name, column_name in json_fields.items():
                if public_name in fields:
                    values[column_name] = _safe_json_dump(fields[public_name])
            for key in ("download_error", "parse_error"):
                if key in values:
                    values[key] = _safe_text(values[key])
            candidate_id = self._new_id("candidate")
            columns = [
                "id",
                "job_id",
                "provider",
                "provider_id",
                "created_at",
                "updated_at",
                *values,
            ]
            parameters = [
                candidate_id,
                resolved,
                str(provider),
                str(provider_id),
                now,
                now,
                *values.values(),
            ]
            placeholders = ", ".join("?" for _ in columns)
            connection.execute(
                f"INSERT INTO subtitle_candidates ({', '.join(columns)}) VALUES ({placeholders})",
                parameters,
            )
            row = connection.execute(
                "SELECT * FROM subtitle_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            return self._candidate_dto(row), True

    add_candidate = record_candidate

    def update_candidate(
        self,
        candidate_id: str,
        *,
        lease_owner: str | None = None,
        **fields: Any,
    ) -> dict[str, Any] | None:
        allowed = {
            "rank",
            "detected_encoding",
            "cue_count",
            "first_cue_seconds",
            "final_cue_seconds",
            "parsed_duration_seconds",
            "expected_runtime_seconds",
            "coverage_percent",
            "download_error",
            "parse_error",
            "status",
            "content_hash",
            "artifact_path",
            "selected_at",
            "selection_method",
        }
        json_fields = {
            "rank_reasons": "rank_reasons_json",
            "quality_reasons": "quality_reasons_json",
            "rejection_reasons": "rejection_reasons_json",
        }
        unknown = set(fields) - allowed - set(json_fields)
        if unknown:
            raise ValueError(f"Unknown candidate fields: {', '.join(sorted(unknown))}")
        updates = {key: fields[key] for key in allowed if key in fields}
        updates.update(
            {
                column: _safe_json_dump(fields[public])
                for public, column in json_fields.items()
                if public in fields
            }
        )
        for key in ("download_error", "parse_error"):
            if key in updates:
                updates[key] = _safe_text(updates[key])
        updates["updated_at"] = self._now_text()
        with self._mutation() as connection:
            row = connection.execute(
                "SELECT * FROM subtitle_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                return None
            job = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (row["job_id"],)
            ).fetchone()
            if not self._lease_allows(job, lease_owner, updates["updated_at"]):
                return None
            if updates:
                clause = ", ".join(f"{column} = ?" for column in updates)
                connection.execute(
                    f"UPDATE subtitle_candidates SET {clause} WHERE id = ?",
                    (*updates.values(), candidate_id),
                )
            updated = connection.execute(
                "SELECT * FROM subtitle_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            return self._candidate_dto(updated)

    def list_candidates(
        self, job_id: str, *, discovery_cycle: int | None = None
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            resolved = self._resolve_job_id(connection, job_id)
            if resolved is None:
                return []
            if discovery_cycle is None:
                return self._candidate_rows(connection, resolved)
            rows = connection.execute(
                """SELECT * FROM subtitle_candidates WHERE job_id = ? AND discovery_cycle = ?
                   ORDER BY CASE WHEN rank IS NULL THEN 1 ELSE 0 END, rank, id""",
                (resolved, int(discovery_cycle)),
            ).fetchall()
            return [self._candidate_dto(row) for row in rows]

    def list_pending_uploads(self) -> list[dict[str, Any]]:
        """Return internal paths only for startup reconciliation of pending uploads."""
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM subtitle_candidates
                   WHERE source_type = 'upload' AND status = 'upload_pending'
                   ORDER BY id"""
            ).fetchall()
            return [
                {**self._candidate_dto(row), "artifact_path": row["artifact_path"]}
                for row in rows
            ]

    def reject_pending_upload(self, candidate_id: str) -> bool:
        """Atomically reconcile one interrupted upload and its safe event."""
        now = self._now_text()
        with self._mutation() as connection:
            row = connection.execute(
                """SELECT * FROM subtitle_candidates
                   WHERE id = ? AND source_type = 'upload'
                     AND status = 'upload_pending'""",
                (candidate_id,),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                """UPDATE subtitle_candidates SET status = 'rejected', artifact_path = NULL,
                       parse_error = ?, rejection_reasons_json = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    "Interrupted subtitle upload was safely discarded.",
                    _safe_json_dump(["interrupted_upload"]),
                    now,
                    candidate_id,
                ),
            )
            self._insert_event(
                connection,
                row["job_id"],
                event_type="subtitle_upload_recovered",
                severity="warning",
                message="An interrupted subtitle upload was safely discarded.",
                data={"candidate_id": candidate_id},
                created_at=now,
            )
            return True

    def get_candidate(
        self,
        candidate_id: str,
        *,
        include_internal: bool = False,
    ) -> dict[str, Any] | None:
        """Read a candidate, requiring an explicit opt-in for its generated path."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM subtitle_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                return None
            result = self._candidate_dto(row)
            if include_internal:
                result["artifact_path"] = row["artifact_path"]
            return result

    def record_decision(
        self,
        job_id: str,
        action: str,
        *,
        idempotency_key: str | None = None,
        target_stage: str | None = None,
        candidate_id: str | None = None,
        platform: str | None = None,
        accepted: bool,
        reason: object = "",
        lease_owner: str | None = None,
    ) -> tuple[dict[str, Any], bool] | None:
        now = self._now_text()
        with self._mutation() as connection:
            resolved = self._require_job_id(connection, job_id)
            job = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            if not self._lease_allows(job, lease_owner, now):
                return None
            if candidate_id is not None:
                candidate = connection.execute(
                    "SELECT job_id FROM subtitle_candidates WHERE id = ?",
                    (candidate_id,),
                ).fetchone()
                if candidate is None or candidate["job_id"] != resolved:
                    raise ValueError("Subtitle candidate does not belong to this run")
            if idempotency_key is not None:
                existing = connection.execute(
                    "SELECT * FROM admin_decisions WHERE job_id = ? AND idempotency_key = ?",
                    (resolved, str(idempotency_key)),
                ).fetchone()
                if existing is not None:
                    return self._decision_dto(existing), False
            cursor = connection.execute(
                """INSERT INTO admin_decisions
                   (job_id, action, target_stage, candidate_id, platform,
                    idempotency_key, accepted, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    resolved,
                    str(action),
                    target_stage,
                    candidate_id,
                    platform,
                    idempotency_key,
                    int(bool(accepted)),
                    _safe_text(reason),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM admin_decisions WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return self._decision_dto(row), True

    def apply_admin_action(
        self,
        job_id: str,
        action: str,
        *,
        idempotency_key: str | None = None,
        target_stage: str | None = None,
        candidate_id: str | None = None,
        platform: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], bool, bool]:
        """Validate, decide, and apply one operator action in one transaction.

        Returns ``(decision, run, changed, accepted)``. An idempotency replay is
        stable and never reapplies work.
        """
        now = self._now_text()
        key = str(idempotency_key).strip() if idempotency_key else None
        with self._mutation() as connection:
            resolved = self._require_job_id(connection, job_id)
            job = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            if key:
                existing = connection.execute(
                    "SELECT * FROM admin_decisions WHERE job_id = ? AND idempotency_key = ?",
                    (resolved, key),
                ).fetchone()
                if existing is not None:
                    if not self._decision_scope_matches(
                        existing,
                        action,
                        target_stage=target_stage,
                        candidate_id=candidate_id,
                        platform=platform,
                    ):
                        rejected = self._insert_idempotency_rejection(
                            connection,
                            resolved,
                            action,
                            target_stage=target_stage,
                            candidate_id=candidate_id,
                            platform=platform,
                            created_at=now,
                        )
                        return rejected, self._job_dto(job), False, False
                    return (
                        self._decision_dto(existing),
                        self._job_dto(job),
                        False,
                        bool(existing["accepted"]),
                    )

            if not key and self._admin_action_equivalent(
                connection,
                job,
                action,
                target_stage=target_stage,
                candidate_id=candidate_id,
                platform=platform,
            ):
                existing = connection.execute(
                    """SELECT * FROM admin_decisions
                       WHERE job_id = ? AND action = ? AND target_stage IS ?
                         AND candidate_id IS ? AND platform IS ? AND accepted = 1
                       ORDER BY id DESC LIMIT 1""",
                    (resolved, action, target_stage, candidate_id, platform),
                ).fetchone()
                if existing is not None:
                    return (
                        self._decision_dto(existing),
                        self._job_dto(job),
                        False,
                        True,
                    )

            accepted, reason = self._admin_action_allowed(
                connection,
                job,
                action,
                target_stage=target_stage,
                candidate_id=candidate_id,
                platform=platform,
            )
            cursor = connection.execute(
                """INSERT INTO admin_decisions
                   (job_id, action, target_stage, candidate_id, platform,
                    idempotency_key, accepted, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    resolved,
                    action,
                    target_stage,
                    candidate_id,
                    platform,
                    key,
                    int(accepted),
                    reason,
                    now,
                ),
            )
            decision = connection.execute(
                "SELECT * FROM admin_decisions WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            if not accepted:
                return self._decision_dto(decision), self._job_dto(job), False, False

            if action == "cancel":
                state = JobState(job["state"])
                if state is JobState.RUNNING:
                    connection.execute(
                        """UPDATE job_runs SET cancel_requested_at = ?, updated_at = ?,
                           next_action = 'cancel_pending' WHERE id = ?""",
                        (now, now, resolved),
                    )
                else:
                    connection.execute(
                        """UPDATE job_runs SET state = 'cancelled', cancel_requested_at = ?,
                           updated_at = ?, finished_at = ?, next_action = NULL,
                           lease_owner = NULL, lease_expires_at = NULL WHERE id = ?""",
                        (now, now, now, resolved),
                    )
                    self._cancel_job_work(connection, resolved, now)
            elif action == "resume":
                connection.execute(
                    """UPDATE job_runs SET state = 'queued', updated_at = ?, finished_at = NULL,
                       cancel_requested_at = NULL, next_action = 'resume',
                       safe_error_code = NULL, safe_error_message = NULL,
                       error_retryable = 0, lease_owner = NULL, lease_expires_at = NULL
                       WHERE id = ?""",
                    (now, resolved),
                )
            elif action in {"retry_stage", "rediscover_subtitles"}:
                stage_name = target_stage or "subtitles"
                stage = connection.execute(
                    "SELECT * FROM pipeline_stages WHERE job_id = ? AND name = ?",
                    (resolved, stage_name),
                ).fetchone()
                if stage is not None:
                    connection.execute(
                        """UPDATE pipeline_stages SET state = 'queued', retry_cycle = retry_cycle + 1,
                           updated_at = ?, started_at = NULL, finished_at = NULL,
                           progress_numerator = 0, progress_denominator = 1,
                           warnings_json = '[]', output_manifest_json = '{}',
                           safe_error_code = NULL, safe_error_message = NULL,
                           retryable = 0, next_action = NULL WHERE id = ?""",
                        (now, stage["id"]),
                    )
                connection.execute(
                    """UPDATE job_runs SET state = 'queued', current_stage = ?, updated_at = ?,
                       finished_at = NULL, next_action = ?, safe_error_code = NULL,
                       safe_error_message = NULL, error_retryable = 0,
                       lease_owner = NULL, lease_expires_at = NULL WHERE id = ?""",
                    (stage_name, now, action, resolved),
                )
            elif action == "select_subtitle":
                connection.execute(
                    """UPDATE job_runs SET state = CASE WHEN state IN ('failed', 'needs_attention', 'cancelled')
                           THEN 'queued' ELSE state END, updated_at = ?, finished_at = NULL,
                       next_action = 'select_subtitle', safe_error_code = NULL,
                       safe_error_message = NULL, error_retryable = 0 WHERE id = ?""",
                    (now, resolved),
                )
            elif action == "publish":
                connection.execute(
                    """INSERT INTO releases (job_id, platform, status, metadata_json, updated_at)
                       VALUES (?, ?, 'pending', ?, ?)
                       ON CONFLICT(job_id, platform) DO UPDATE SET status = 'pending',
                         safe_error_code = NULL, safe_error_message = NULL,
                         updated_at = excluded.updated_at
                       WHERE releases.status NOT IN ('uploaded', 'uploading')""",
                    (resolved, platform, _safe_json_dump(metadata or {}), now),
                )
            elif action == "refresh_stats":
                connection.execute(
                    "UPDATE job_runs SET updated_at = ? WHERE id = ?", (now, resolved)
                )

            self._insert_event(
                connection,
                resolved,
                event_type=f"admin_{action}_accepted",
                message=f"Operator action {action} was accepted.",
                data={
                    "action": action,
                    "target_stage": target_stage,
                    "candidate_id": candidate_id,
                    "platform": platform,
                },
                created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            return self._decision_dto(decision), self._job_dto(updated), True, True

    @staticmethod
    def _decision_scope_matches(
        decision: sqlite3.Row,
        action: str,
        *,
        target_stage: str | None,
        candidate_id: str | None,
        platform: str | None,
    ) -> bool:
        return (
            decision["action"] == action
            and decision["target_stage"] == target_stage
            and decision["candidate_id"] == candidate_id
            and decision["platform"] == platform
        )

    def _insert_idempotency_rejection(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        action: str,
        *,
        target_stage: str | None = None,
        candidate_id: str | None = None,
        platform: str | None = None,
        created_at: str,
    ) -> dict[str, Any]:
        cursor = connection.execute(
            """INSERT INTO admin_decisions
               (job_id, action, target_stage, candidate_id, platform,
                idempotency_key, accepted, reason, created_at)
               VALUES (?, ?, ?, ?, ?, NULL, 0, ?, ?)""",
            (
                job_id,
                action,
                target_stage,
                candidate_id,
                platform,
                "Idempotency key belongs to another operation.",
                created_at,
            ),
        )
        row = connection.execute(
            "SELECT * FROM admin_decisions WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return self._decision_dto(row)

    @staticmethod
    def _admin_action_allowed(
        connection: sqlite3.Connection,
        job: sqlite3.Row,
        action: str,
        *,
        target_stage: str | None,
        candidate_id: str | None,
        platform: str | None,
    ) -> tuple[bool, str]:
        state = str(job["state"])
        if action == "cancel":
            allowed = state in {"queued", "running", "needs_attention", "failed"}
        elif action == "resume":
            allowed = state in {"cancelled", "failed", "needs_attention"}
        elif action == "retry_stage":
            stage = connection.execute(
                "SELECT state FROM pipeline_stages WHERE job_id = ? AND name = ?",
                (job["id"], target_stage),
            ).fetchone()
            allowed = stage is not None and stage["state"] in {
                "failed",
                "needs_attention",
                "cancelled",
            }
        elif action == "rediscover_subtitles":
            allowed = state not in {"running", "completed"}
        elif action == "select_subtitle":
            candidate = connection.execute(
                "SELECT job_id FROM subtitle_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            allowed = candidate is not None and candidate["job_id"] == job["id"]
        elif action == "publish":
            release = connection.execute(
                "SELECT status FROM releases WHERE job_id = ? AND platform = ?",
                (job["id"], platform),
            ).fetchone()
            allowed = state == "completed" and (
                release is None or release["status"] not in {"uploaded", "uploading"}
            )
        elif action == "refresh_stats":
            allowed = (
                connection.execute(
                    """SELECT 1 FROM releases WHERE job_id = ? AND status = 'uploaded'
                       AND remote_id IS NOT NULL LIMIT 1""",
                    (job["id"],),
                ).fetchone()
                is not None
            )
        else:
            allowed = False
        return allowed, "" if allowed else "Action conflicts with current run state."

    @staticmethod
    def _admin_action_equivalent(
        connection: sqlite3.Connection,
        job: sqlite3.Row,
        action: str,
        *,
        target_stage: str | None,
        candidate_id: str | None,
        platform: str | None,
    ) -> bool:
        state = str(job["state"])
        if action == "cancel":
            return state == "cancelled" or job["cancel_requested_at"] is not None
        if action == "resume":
            return state == "queued" and job["next_action"] == "resume"
        if action in {"retry_stage", "rediscover_subtitles"}:
            stage_name = target_stage or "subtitles"
            stage = connection.execute(
                "SELECT state FROM pipeline_stages WHERE job_id = ? AND name = ?",
                (job["id"], stage_name),
            ).fetchone()
            return (
                state == "queued"
                and job["next_action"] == action
                and (stage is None or stage["state"] == "queued")
            )
        if action == "select_subtitle":
            return job["next_action"] == "select_subtitle" and candidate_id is not None
        if action == "publish":
            release = connection.execute(
                "SELECT status FROM releases WHERE job_id = ? AND platform = ?",
                (job["id"], platform),
            ).fetchone()
            return release is not None and release["status"] in {
                "pending",
                "uploading",
                "uploaded",
            }
        return action == "refresh_stats"

    def discard_candidate(self, candidate_id: str) -> None:
        """Compensate an uncommitted upload finalization without exposing its path."""
        with self._mutation() as connection:
            connection.execute(
                "DELETE FROM subtitle_candidates WHERE id = ?", (candidate_id,)
            )

    def list_decisions(self, job_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            resolved = self._resolve_job_id(connection, job_id)
            return [] if resolved is None else self._decision_rows(connection, resolved)

    def reject_idempotency_reuse(
        self,
        job_id: str,
        action: str,
        *,
        target_stage: str | None = None,
        candidate_id: str | None = None,
        platform: str | None = None,
    ) -> dict[str, Any]:
        """Durably reject an idempotency key reused for another operation."""
        with self._mutation() as connection:
            resolved = self._require_job_id(connection, job_id)
            return self._insert_idempotency_rejection(
                connection,
                resolved,
                action,
                target_stage=target_stage,
                candidate_id=candidate_id,
                platform=platform,
                created_at=self._now_text(),
            )

    def finalize_uploaded_candidate(
        self,
        job_id: str,
        candidate_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], bool, bool]:
        """Atomically record an accepted upload, resumable state, and event."""
        now = self._now_text()
        key = str(idempotency_key).strip() if idempotency_key else None
        with self._mutation() as connection:
            resolved = self._require_job_id(connection, job_id)
            candidate = connection.execute(
                "SELECT * FROM subtitle_candidates WHERE id = ? AND job_id = ?",
                (candidate_id, resolved),
            ).fetchone()
            if candidate is None:
                raise ValueError("Subtitle candidate does not belong to this run")
            if candidate["status"] not in {"upload_pending", "uploaded"}:
                raise ValueError(
                    "Subtitle candidate is not ready for upload finalization"
                )
            if not candidate["artifact_path"]:
                raise ValueError("Subtitle candidate has no validated artifact")
            if key:
                existing = connection.execute(
                    "SELECT * FROM admin_decisions WHERE job_id = ? AND idempotency_key = ?",
                    (resolved, key),
                ).fetchone()
                if existing is not None:
                    if not self._decision_scope_matches(
                        existing,
                        "upload_subtitle",
                        target_stage=None,
                        candidate_id=candidate_id,
                        platform=None,
                    ):
                        rejected = self._insert_idempotency_rejection(
                            connection,
                            resolved,
                            "upload_subtitle",
                            candidate_id=candidate_id,
                            created_at=now,
                        )
                        run = connection.execute(
                            "SELECT * FROM job_runs WHERE id = ?", (resolved,)
                        ).fetchone()
                        return rejected, self._job_dto(run), False, False
                    run = connection.execute(
                        "SELECT * FROM job_runs WHERE id = ?", (resolved,)
                    ).fetchone()
                    return self._decision_dto(existing), self._job_dto(run), False, True
            elif candidate["status"] == "uploaded":
                existing = connection.execute(
                    """SELECT * FROM admin_decisions
                       WHERE job_id = ? AND action = 'upload_subtitle'
                         AND candidate_id = ? AND accepted = 1
                       ORDER BY id DESC LIMIT 1""",
                    (resolved, candidate_id),
                ).fetchone()
                if existing is not None:
                    run = connection.execute(
                        "SELECT * FROM job_runs WHERE id = ?", (resolved,)
                    ).fetchone()
                    return self._decision_dto(existing), self._job_dto(run), False, True
            cursor = connection.execute(
                """INSERT INTO admin_decisions
                   (job_id, action, candidate_id, idempotency_key, accepted, reason, created_at)
                   VALUES (?, 'upload_subtitle', ?, ?, 1, '', ?)""",
                (resolved, candidate_id, key, now),
            )
            connection.execute(
                """UPDATE subtitle_candidates SET status = 'uploaded', updated_at = ?
                   WHERE id = ?""",
                (now, candidate_id),
            )
            connection.execute(
                """UPDATE job_runs SET state = CASE
                         WHEN state IN ('failed', 'needs_attention', 'cancelled') THEN 'queued'
                         ELSE state END,
                       updated_at = ?, finished_at = CASE
                         WHEN state IN ('failed', 'needs_attention', 'cancelled') THEN NULL
                         ELSE finished_at END,
                       next_action = 'select_subtitle', safe_error_code = NULL,
                       safe_error_message = NULL, error_retryable = 0 WHERE id = ?""",
                (now, resolved),
            )
            self._insert_event(
                connection,
                resolved,
                event_type="subtitle_uploaded",
                message="An operator subtitle upload was accepted.",
                data={"candidate_id": candidate_id},
                created_at=now,
            )
            decision = connection.execute(
                "SELECT * FROM admin_decisions WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            run = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            return self._decision_dto(decision), self._job_dto(run), True, True

    def compatibility_analysis(self, job_id: str) -> dict[str, Any] | None:
        """Return only the sanitized legacy analysis payload for a resolved run."""
        with self._connection() as connection:
            resolved = self._resolve_job_id(connection, job_id)
            if resolved is None:
                return None
            row = connection.execute(
                "SELECT legacy_payload_json FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            payload = _json_load(row["legacy_payload_json"], {})
            analysis = _json_load(
                payload.get("analysis_json"), payload.get("analysis_json")
            )
            return _safe_json_value(analysis) if isinstance(analysis, Mapping) else None

    def request_publication(
        self,
        job_id: str,
        platform: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        metadata_factory: Callable[[], Mapping[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Persist one explicit platform request without replacing its metadata."""
        now = self._now_text()
        normalized_platform = str(platform).strip().lower()
        if not normalized_platform:
            raise ValueError("A publishing platform is required")
        if (metadata is None) == (metadata_factory is None):
            raise ValueError("Provide exactly one publishing metadata source")
        with self._mutation() as connection:
            resolved = self._require_job_id(connection, job_id)
            existing = connection.execute(
                "SELECT * FROM releases WHERE job_id = ? AND platform = ?",
                (resolved, normalized_platform),
            ).fetchone()
            if existing is not None:
                return self._release_dto(existing), False
            snapshot = metadata if metadata is not None else metadata_factory()
            connection.execute(
                """INSERT INTO releases
                   (job_id, platform, status, metadata_json, updated_at)
                   VALUES (?, ?, 'pending', ?, ?)""",
                (
                    resolved,
                    normalized_platform,
                    _safe_json_dump(snapshot),
                    now,
                ),
            )
            self._insert_event(
                connection,
                resolved,
                event_type="publishing_requested",
                message=f"Publishing to {normalized_platform} was requested.",
                data={"platform": normalized_platform},
                created_at=now,
            )
            row = connection.execute(
                "SELECT * FROM releases WHERE job_id = ? AND platform = ?",
                (resolved, normalized_platform),
            ).fetchone()
            return self._release_dto(row), True

    def recover_expired_publishing_attempt(
        self, job_id: str, platform: str
    ) -> tuple[dict[str, Any], bool]:
        """Mark an expired publishing lease ambiguous without touching live work."""
        now = self._now_text()
        normalized_platform = str(platform).strip().lower()
        with self._mutation() as connection:
            resolved = self._require_job_id(connection, job_id)
            release = connection.execute(
                "SELECT * FROM releases WHERE job_id = ? AND platform = ?",
                (resolved, normalized_platform),
            ).fetchone()
            if release is None:
                raise KeyError("Publishing release was not found")
            if release["status"] != "uploading":
                return self._release_dto(release), False
            attempt = connection.execute(
                """SELECT * FROM publishing_attempts
                   WHERE job_id = ? AND platform = ? AND finished_at IS NULL
                   ORDER BY id DESC LIMIT 1""",
                (resolved, normalized_platform),
            ).fetchone()
            if attempt is None:
                raise ValueError("Uploading release has no active publishing attempt")
            if not attempt["lease_expires_at"] or attempt["lease_expires_at"] > now:
                return self._release_dto(release), False
            code = "ambiguous_publish_outcome"
            message = "The interrupted publishing result requires reconciliation."
            connection.execute(
                """UPDATE publishing_attempts SET finished_at = ?, outcome = 'ambiguous',
                       retryable = 0, safe_error_code = ?, safe_error_message = ?
                   WHERE id = ? AND finished_at IS NULL""",
                (now, code, message, attempt["id"]),
            )
            connection.execute(
                """UPDATE releases SET status = 'needs_attention',
                       safe_error_code = ?, safe_error_message = ?, updated_at = ?
                   WHERE id = ?""",
                (code, message, now, release["id"]),
            )
            self._insert_event(
                connection,
                resolved,
                event_type="publishing_ambiguous",
                severity="warning",
                message=f"Publishing to {normalized_platform} was interrupted.",
                data={
                    "platform": normalized_platform,
                    "publishing_attempt_id": int(attempt["id"]),
                    "outcome": "ambiguous",
                    "retryable": False,
                },
                created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM releases WHERE id = ?", (release["id"],)
            ).fetchone()
            return self._release_dto(updated), True

    def claim_publishing_attempt(
        self,
        job_id: str,
        platform: str,
        *,
        retry_cycle: int,
        max_attempts: int = 3,
        trigger: AttemptTrigger | str = AttemptTrigger.AUTOMATIC,
        lease_owner: str | None = None,
        lease_seconds: float = 120.0,
    ) -> tuple[dict[str, Any] | None, bool, dict[str, Any]]:
        """Atomically claim a publication call and mark its release uploading."""
        if lease_seconds <= 0:
            raise ValueError("Publishing lease duration must be positive")
        now_dt = self._now_datetime()
        now = now_dt.isoformat()
        owner = str(lease_owner or f"publishing-{uuid.uuid4().hex}")
        expires = (now_dt + timedelta(seconds=float(lease_seconds))).isoformat()
        normalized_platform = str(platform).strip().lower()
        with self._mutation() as connection:
            resolved = self._require_job_id(connection, job_id)
            release = connection.execute(
                "SELECT * FROM releases WHERE job_id = ? AND platform = ?",
                (resolved, normalized_platform),
            ).fetchone()
            if release is None:
                raise KeyError("Publishing was not requested for this platform")
            if release["status"] in {"uploaded", "uploading", "needs_attention"}:
                active = connection.execute(
                    """SELECT * FROM publishing_attempts
                       WHERE job_id = ? AND platform = ? AND finished_at IS NULL
                       ORDER BY id DESC LIMIT 1""",
                    (resolved, normalized_platform),
                ).fetchone()
                return (
                    self._publishing_dto(active) if active is not None else None,
                    False,
                    self._release_dto(release),
                )
            active = connection.execute(
                """SELECT * FROM publishing_attempts
                   WHERE job_id = ? AND platform = ? AND finished_at IS NULL
                   ORDER BY id DESC LIMIT 1""",
                (resolved, normalized_platform),
            ).fetchone()
            if active is not None:
                return self._publishing_dto(active), False, self._release_dto(release)
            number = int(
                connection.execute(
                    """SELECT COALESCE(MAX(attempt_number), 0) + 1
                       FROM publishing_attempts
                       WHERE job_id = ? AND platform = ? AND retry_cycle = ?""",
                    (resolved, normalized_platform, int(retry_cycle)),
                ).fetchone()[0]
            )
            if number > int(max_attempts):
                return None, False, self._release_dto(release)
            cursor = connection.execute(
                """INSERT INTO publishing_attempts
                   (job_id, platform, retry_cycle, attempt_number, max_attempts,
                    trigger, started_at, metadata_json, lease_owner, lease_expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    resolved,
                    normalized_platform,
                    int(retry_cycle),
                    number,
                    int(max_attempts),
                    _enum_value(AttemptTrigger, trigger),
                    now,
                    release["metadata_json"],
                    owner,
                    expires,
                ),
            )
            connection.execute(
                """UPDATE releases SET status = 'uploading', safe_error_code = NULL,
                       safe_error_message = NULL, updated_at = ? WHERE id = ?""",
                (now, release["id"]),
            )
            self._insert_event(
                connection,
                resolved,
                event_type="publishing_attempt_started",
                message=f"Publishing to {normalized_platform} started.",
                data={
                    "platform": normalized_platform,
                    "retry_cycle": int(retry_cycle),
                    "attempt_number": number,
                    "max_attempts": int(max_attempts),
                },
                created_at=now,
            )
            attempt = connection.execute(
                "SELECT * FROM publishing_attempts WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            updated_release = connection.execute(
                "SELECT * FROM releases WHERE id = ?", (release["id"],)
            ).fetchone()
            return (
                self._publishing_dto(attempt),
                True,
                self._release_dto(updated_release),
            )

    def renew_publishing_attempt_lease(
        self,
        attempt_id: int,
        owner: str,
        *,
        lease_seconds: float,
    ) -> bool:
        """Renew only the current owner's still-live publication claim."""
        if lease_seconds <= 0:
            raise ValueError("Publishing lease duration must be positive")
        now_dt = self._now_datetime()
        with self._mutation() as connection:
            cursor = connection.execute(
                """UPDATE publishing_attempts SET lease_expires_at = ?
                   WHERE id = ? AND finished_at IS NULL AND lease_owner = ?
                     AND lease_expires_at > ?""",
                (
                    (now_dt + timedelta(seconds=float(lease_seconds))).isoformat(),
                    int(attempt_id),
                    str(owner),
                    now_dt.isoformat(),
                ),
            )
            return cursor.rowcount == 1

    def complete_publishing_attempt(
        self,
        attempt_id: int,
        *,
        outcome: str,
        release_status: str,
        retryable: bool = False,
        safe_error_code: str | None = None,
        safe_error_message: object | None = None,
        remote_id: str | None = None,
        lease_owner: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Finish an attempt, release summary, and event in one transaction."""
        now = self._now_text()
        normalized_remote_id = (
            _normalized_remote_id(remote_id) if remote_id is not None else None
        )
        if release_status == "uploaded" and normalized_remote_id is None:
            raise ValueError("An uploaded release requires a non-empty remote ID")
        with self._mutation() as connection:
            attempt = connection.execute(
                "SELECT * FROM publishing_attempts WHERE id = ?", (int(attempt_id),)
            ).fetchone()
            if attempt is None:
                raise KeyError("Publishing attempt was not found")
            if (
                attempt["finished_at"] is None
                and attempt["lease_owner"] is not None
                and (
                    lease_owner is None
                    or attempt["lease_owner"] != str(lease_owner)
                    or not attempt["lease_expires_at"]
                    or attempt["lease_expires_at"] <= now
                )
            ):
                raise PermissionError("Publishing attempt lease is no longer valid")
            release = connection.execute(
                "SELECT * FROM releases WHERE job_id = ? AND platform = ?",
                (attempt["job_id"], attempt["platform"]),
            ).fetchone()
            if release is None:
                raise KeyError("Publishing release was not found")
            if attempt["finished_at"] is not None:
                return self._publishing_dto(attempt), self._release_dto(release)
            connection.execute(
                """UPDATE publishing_attempts SET finished_at = ?, outcome = ?,
                       retryable = ?, safe_error_code = ?, safe_error_message = ?,
                       remote_id = ?, lease_owner = NULL, lease_expires_at = NULL
                   WHERE id = ?""",
                (
                    now,
                    _safe_text(outcome),
                    int(bool(retryable)),
                    _safe_text(safe_error_code),
                    _safe_text(safe_error_message),
                    _safe_text(normalized_remote_id) if normalized_remote_id else None,
                    int(attempt_id),
                ),
            )
            connection.execute(
                """UPDATE releases SET status = ?, remote_id = COALESCE(?, remote_id),
                       uploaded_at = CASE WHEN ? = 'uploaded' THEN COALESCE(uploaded_at, ?)
                                          ELSE uploaded_at END,
                       safe_error_code = ?, safe_error_message = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    _safe_text(release_status),
                    _safe_text(normalized_remote_id) if normalized_remote_id else None,
                    release_status,
                    now,
                    _safe_text(safe_error_code),
                    _safe_text(safe_error_message),
                    now,
                    release["id"],
                ),
            )
            event_type = {
                "completed": "publishing_completed",
                "ambiguous": "publishing_ambiguous",
            }.get(outcome, "publishing_attempt_failed")
            self._insert_event(
                connection,
                attempt["job_id"],
                event_type=event_type,
                severity="info" if outcome == "completed" else "warning",
                message=(
                    f"Publishing to {attempt['platform']} completed."
                    if outcome == "completed"
                    else f"Publishing to {attempt['platform']} did not complete."
                ),
                data={
                    "platform": attempt["platform"],
                    "publishing_attempt_id": int(attempt_id),
                    "outcome": outcome,
                    "retryable": bool(retryable),
                },
                created_at=now,
            )
            updated_attempt = connection.execute(
                "SELECT * FROM publishing_attempts WHERE id = ?", (int(attempt_id),)
            ).fetchone()
            updated_release = connection.execute(
                "SELECT * FROM releases WHERE id = ?", (release["id"],)
            ).fetchone()
            return (
                self._publishing_dto(updated_attempt),
                self._release_dto(updated_release),
            )

    def reconcile_publication_request(
        self,
        job_id: str,
        platform: str,
        *,
        outcome: str,
        remote_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """Atomically reconcile an ambiguous release with idempotent decision replay."""
        now = self._now_text()
        normalized_platform = str(platform).strip().lower()
        key = str(idempotency_key).strip() if idempotency_key else None
        normalized_remote_id = (
            _normalized_remote_id(remote_id) if remote_id is not None else None
        )
        if outcome not in {"uploaded", "not_uploaded"}:
            raise ValueError("Unknown publishing reconciliation outcome")
        if outcome == "uploaded" and normalized_remote_id is None:
            raise ValueError("An uploaded reconciliation requires a remote ID")
        with self._mutation() as connection:
            resolved = self._require_job_id(connection, job_id)
            release = connection.execute(
                "SELECT * FROM releases WHERE job_id = ? AND platform = ?",
                (resolved, normalized_platform),
            ).fetchone()
            if release is None:
                raise KeyError("Publishing release was not found")
            if key:
                existing = connection.execute(
                    "SELECT * FROM admin_decisions WHERE job_id = ? AND idempotency_key = ?",
                    (resolved, key),
                ).fetchone()
                if existing is not None:
                    if existing["action"] != "reconcile_publishing":
                        raise ValueError("Idempotency key belongs to another action")
                    return (
                        self._release_dto(release),
                        self._decision_dto(existing),
                        False,
                    )
            else:
                existing = connection.execute(
                    """SELECT * FROM admin_decisions
                       WHERE job_id = ? AND action = 'reconcile_publishing'
                         AND platform = ? AND accepted = 1 AND reason = ?
                       ORDER BY id DESC LIMIT 1""",
                    (resolved, normalized_platform, outcome),
                ).fetchone()
                expected_status = "uploaded" if outcome == "uploaded" else "failed"
                if existing is not None and release["status"] == expected_status:
                    return (
                        self._release_dto(release),
                        self._decision_dto(existing),
                        False,
                    )
            if not (
                release["status"] == "needs_attention"
                and release["safe_error_code"]
                in {"ambiguous_publish", "ambiguous_publish_outcome"}
            ):
                raise ValueError("Only an ambiguous publication can be reconciled")
            status = "uploaded" if outcome == "uploaded" else "failed"
            error_code = (
                None if outcome == "uploaded" else "publishing_reconciled_absent"
            )
            error_message = (
                None
                if outcome == "uploaded"
                else "Reconciliation confirmed that no remote publication exists."
            )
            connection.execute(
                """UPDATE releases SET status = ?, remote_id = COALESCE(?, remote_id),
                       uploaded_at = CASE WHEN ? = 'uploaded' THEN COALESCE(uploaded_at, ?)
                                          ELSE uploaded_at END,
                       safe_error_code = ?, safe_error_message = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    status,
                    _safe_text(normalized_remote_id) if normalized_remote_id else None,
                    status,
                    now,
                    error_code,
                    error_message,
                    now,
                    release["id"],
                ),
            )
            cursor = connection.execute(
                """INSERT INTO admin_decisions
                   (job_id, action, platform, idempotency_key, accepted, reason, created_at)
                   VALUES (?, 'reconcile_publishing', ?, ?, 1, ?, ?)""",
                (resolved, normalized_platform, key, outcome, now),
            )
            self._insert_event(
                connection,
                resolved,
                event_type="publishing_reconciled",
                message=f"Publishing to {normalized_platform} was reconciled.",
                data={"platform": normalized_platform, "outcome": outcome},
                created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM releases WHERE id = ?", (release["id"],)
            ).fetchone()
            decision = connection.execute(
                "SELECT * FROM admin_decisions WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return self._release_dto(updated), self._decision_dto(decision), True

    def reconcile_publication(
        self,
        job_id: str,
        platform: str,
        *,
        outcome: str,
        remote_id: str | None = None,
    ) -> dict[str, Any]:
        """Record an operator's explicit resolution of an ambiguous upload."""
        now = self._now_text()
        normalized_platform = str(platform).strip().lower()
        normalized_remote_id = (
            _normalized_remote_id(remote_id) if remote_id is not None else None
        )
        if outcome not in {"uploaded", "not_uploaded"}:
            raise ValueError("Unknown publishing reconciliation outcome")
        if outcome == "uploaded" and normalized_remote_id is None:
            raise ValueError("An uploaded reconciliation requires a remote ID")
        with self._mutation() as connection:
            resolved = self._require_job_id(connection, job_id)
            release = connection.execute(
                "SELECT * FROM releases WHERE job_id = ? AND platform = ?",
                (resolved, normalized_platform),
            ).fetchone()
            if release is None:
                raise KeyError("Publishing release was not found")
            if not (
                release["status"] == "needs_attention"
                and release["safe_error_code"]
                in {"ambiguous_publish", "ambiguous_publish_outcome"}
            ):
                raise ValueError("Only an ambiguous publication can be reconciled")
            status = "uploaded" if outcome == "uploaded" else "failed"
            error_code = (
                None if outcome == "uploaded" else "publishing_reconciled_absent"
            )
            error_message = (
                None
                if outcome == "uploaded"
                else "Reconciliation confirmed that no remote publication exists."
            )
            connection.execute(
                """UPDATE releases SET status = ?, remote_id = COALESCE(?, remote_id),
                       uploaded_at = CASE WHEN ? = 'uploaded' THEN COALESCE(uploaded_at, ?)
                                          ELSE uploaded_at END,
                       safe_error_code = ?, safe_error_message = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    status,
                    _safe_text(normalized_remote_id) if normalized_remote_id else None,
                    status,
                    now,
                    error_code,
                    error_message,
                    now,
                    release["id"],
                ),
            )
            connection.execute(
                """INSERT INTO admin_decisions
                   (job_id, action, platform, accepted, reason, created_at)
                   VALUES (?, 'reconcile_publishing', ?, 1, ?, ?)""",
                (resolved, normalized_platform, outcome, now),
            )
            self._insert_event(
                connection,
                resolved,
                event_type="publishing_reconciled",
                message=f"Publishing to {normalized_platform} was reconciled.",
                data={"platform": normalized_platform, "outcome": outcome},
                created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM releases WHERE id = ?", (release["id"],)
            ).fetchone()
            return self._release_dto(updated)

    def store_publishing_stats(
        self,
        job_id: str,
        platform: str,
        date: str,
        metrics: Mapping[str, int | float],
    ) -> dict[str, Any]:
        """Atomically store one verified metrics snapshot and its event."""
        now = self._now_text()
        normalized_platform = str(platform).strip().lower()
        count_fields = ("views", "likes", "comments", "shares")
        if any(
            isinstance(metrics.get(field), bool)
            or not isinstance(metrics.get(field), int)
            or metrics[field] < 0
            for field in count_fields
        ):
            raise ValueError("Publishing count metrics must be non-negative integers")
        revenue = metrics.get("revenue_usd")
        if (
            isinstance(revenue, bool)
            or not isinstance(revenue, (int, float))
            or not math.isfinite(revenue)
            or revenue < 0
        ):
            raise ValueError(
                "Publishing revenue metrics must be finite and non-negative"
            )
        with self._mutation() as connection:
            resolved = self._require_job_id(connection, job_id)
            release = connection.execute(
                "SELECT remote_id, status FROM releases WHERE job_id = ? AND platform = ?",
                (resolved, normalized_platform),
            ).fetchone()
            try:
                confirmed_remote_id = (
                    _normalized_remote_id(release["remote_id"])
                    if release is not None
                    else None
                )
            except ValueError:
                confirmed_remote_id = None
            if (
                release is None
                or release["status"] != "uploaded"
                or confirmed_remote_id is None
            ):
                raise ValueError("Statistics require a confirmed remote publication")
            connection.execute(
                """INSERT INTO revenue
                   (job_id, platform, date, views, revenue_usd, likes, comments,
                    shares, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id, platform, date) DO UPDATE SET
                     views = excluded.views, revenue_usd = excluded.revenue_usd,
                     likes = excluded.likes, comments = excluded.comments,
                     shares = excluded.shares, fetched_at = excluded.fetched_at""",
                (
                    resolved,
                    normalized_platform,
                    str(date),
                    int(metrics["views"]),
                    float(metrics["revenue_usd"]),
                    int(metrics["likes"]),
                    int(metrics["comments"]),
                    int(metrics["shares"]),
                    now,
                ),
            )
            self._insert_event(
                connection,
                resolved,
                event_type="publishing_stats_refreshed",
                message=f"Statistics for {normalized_platform} were refreshed.",
                data={"platform": normalized_platform, "date": str(date)},
                created_at=now,
            )
            row = connection.execute(
                "SELECT * FROM revenue WHERE job_id = ? AND platform = ? AND date = ?",
                (resolved, normalized_platform, str(date)),
            ).fetchone()
            return self._revenue_dto(row)

    def record_cost(
        self,
        job_id: str,
        category: str,
        provider: str,
        amount_usd: float = 0,
        units: int = 1,
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._mutation() as connection:
            resolved = self._require_job_id(connection, job_id)
            cursor = connection.execute(
                """INSERT INTO costs
                   (job_id, category, provider, amount_usd, units, detail_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    resolved,
                    category,
                    provider,
                    float(amount_usd),
                    int(units),
                    _safe_json_dump(detail or {}),
                    self._now_text(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM costs WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return self._cost_dto(row)

    def list_costs(self, job_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            resolved = self._resolve_job_id(connection, job_id)
            return [] if resolved is None else self._cost_rows(connection, resolved)

    def aggregate_costs(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        group_by: str = "category",
    ) -> list[dict[str, Any]]:
        group_columns = {
            "category": "category",
            "day": "substr(created_at, 1, 10)",
            "week": "strftime('%Y-%W', created_at)",
            "month": "substr(created_at, 1, 7)",
        }
        group_column = group_columns.get(group_by, "category")
        conditions: list[str] = []
        parameters: list[str] = []
        if start:
            conditions.append("created_at >= ?")
            parameters.append(start)
        if end:
            conditions.append("created_at <= ?")
            parameters.append(end)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT {group_column} AS period, category, provider,
                           SUM(amount_usd) AS total_usd, SUM(units) AS total_units,
                           COUNT(*) AS count FROM costs{where}
                    GROUP BY {group_column}, category, provider ORDER BY period, category""",
                parameters,
            ).fetchall()
            return [dict(row) for row in rows]

    def upsert_release(
        self,
        job_id: str,
        platform: str,
        *,
        status: str = "pending",
        remote_id: str | None = None,
        safe_error_code: str | None = None,
        safe_error_message: object | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = self._now_text()
        normalized_remote_id = (
            _normalized_remote_id(remote_id) if remote_id is not None else None
        )
        if status == "uploaded" and normalized_remote_id is None:
            raise ValueError("An uploaded release requires a non-empty remote ID")
        uploaded_at = now if status == "uploaded" else None
        with self._mutation() as connection:
            resolved = self._require_job_id(connection, job_id)
            connection.execute(
                """INSERT INTO releases
                   (job_id, platform, remote_id, status, uploaded_at, safe_error_code,
                    safe_error_message, metadata_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id, platform) DO UPDATE SET
                     remote_id = COALESCE(excluded.remote_id, releases.remote_id),
                     status = excluded.status,
                     uploaded_at = COALESCE(excluded.uploaded_at, releases.uploaded_at),
                     safe_error_code = excluded.safe_error_code,
                     safe_error_message = excluded.safe_error_message,
                     metadata_json = CASE WHEN excluded.metadata_json = '{}' THEN releases.metadata_json
                                          ELSE excluded.metadata_json END,
                     updated_at = excluded.updated_at""",
                (
                    resolved,
                    platform,
                    _safe_text(normalized_remote_id) if normalized_remote_id else None,
                    status,
                    uploaded_at,
                    _safe_text(safe_error_code),
                    _safe_text(safe_error_message),
                    _safe_json_dump(metadata or {}),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM releases WHERE job_id = ? AND platform = ?",
                (resolved, platform),
            ).fetchone()
            return self._release_dto(row)

    def list_releases(self, job_id: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if job_id is None:
                rows = connection.execute(
                    "SELECT * FROM releases ORDER BY id DESC"
                ).fetchall()
                return [self._release_dto(row) for row in rows]
            resolved = self._resolve_job_id(connection, job_id)
            return [] if resolved is None else self._release_rows(connection, resolved)

    def upsert_revenue(
        self,
        job_id: str,
        platform: str,
        date: str,
        *,
        views: int = 0,
        revenue_usd: float = 0,
        likes: int = 0,
        comments: int = 0,
        shares: int = 0,
    ) -> dict[str, Any]:
        now = self._now_text()
        with self._mutation() as connection:
            resolved = self._require_job_id(connection, job_id)
            connection.execute(
                """INSERT INTO revenue
                   (job_id, platform, date, views, revenue_usd, likes, comments, shares, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id, platform, date) DO UPDATE SET
                     views = excluded.views, revenue_usd = excluded.revenue_usd,
                     likes = excluded.likes, comments = excluded.comments,
                     shares = excluded.shares, fetched_at = excluded.fetched_at""",
                (
                    resolved,
                    platform,
                    date,
                    int(views),
                    float(revenue_usd),
                    int(likes),
                    int(comments),
                    int(shares),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM revenue WHERE job_id = ? AND platform = ? AND date = ?",
                (resolved, platform, date),
            ).fetchone()
            return self._revenue_dto(row)

    def list_revenue(self, job_id: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if job_id is None:
                rows = connection.execute(
                    "SELECT * FROM revenue ORDER BY date DESC, id DESC"
                ).fetchall()
                return [self._revenue_dto(row) for row in rows]
            resolved = self._resolve_job_id(connection, job_id)
            return [] if resolved is None else self._revenue_rows(connection, resolved)

    def platform_stats(self, job_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            resolved = self._resolve_job_id(connection, job_id)
            if resolved is None:
                return []
            rows = connection.execute(
                """SELECT rev.*, rel.remote_id, rel.status AS release_status, rel.uploaded_at
                   FROM revenue rev LEFT JOIN releases rel
                     ON rev.job_id = rel.job_id AND rev.platform = rel.platform
                   WHERE rev.job_id = ? AND rev.date = (
                     SELECT MAX(r2.date) FROM revenue r2
                     WHERE r2.job_id = rev.job_id AND r2.platform = rev.platform
                   ) ORDER BY rev.platform""",
                (resolved,),
            ).fetchall()
            return [
                {
                    **self._revenue_dto(row),
                    "remote_id": row["remote_id"],
                    "release_status": row["release_status"],
                    "uploaded_at": row["uploaded_at"],
                }
                for row in rows
            ]

    def compatibility_update_job(
        self, job_id: str, **fields: Any
    ) -> dict[str, Any] | None:
        """Keep the pre-dispatcher pipeline operational until later tasks replace it."""
        allowed = {
            "label",
            "progress",
            "message",
            "error",
            "video_path",
            "analysis_json",
            "movie_info",
            "segment_timing",
        }
        unknown = set(fields) - allowed - {"status"}
        if unknown:
            raise ValueError(f"Unknown job fields: {', '.join(sorted(unknown))}")
        now = self._now_text()
        with self._mutation() as connection:
            resolved = self._resolve_job_id(connection, job_id)
            if resolved is None:
                return None
            row = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            payload = _json_load(row["legacy_payload_json"], {})
            payload.update(
                {
                    key: value
                    for key, value in fields.items()
                    if key
                    in {"video_path", "analysis_json", "movie_info", "segment_timing"}
                }
            )
            updates: dict[str, Any] = {
                "updated_at": now,
                "legacy_payload_json": json.dumps(payload, default=str, sort_keys=True),
            }
            if "label" in fields:
                updates["label"] = str(fields["label"])
            if "progress" in fields:
                updates["legacy_progress"] = int(fields["progress"])
            if "message" in fields:
                updates["legacy_message"] = _safe_text(fields["message"])
            if "error" in fields:
                updates["safe_error_message"] = _safe_text(fields["error"])
            if "status" in fields:
                updates["state"] = _legacy_job_state(fields["status"])
                if updates["state"] == "running":
                    updates["started_at"] = row["started_at"] or now
                if updates["state"] in {"completed", "failed", "cancelled"}:
                    updates["finished_at"] = now
            clause = ", ".join(f"{column} = ?" for column in updates)
            connection.execute(
                f"UPDATE job_runs SET {clause} WHERE id = ?",
                (*updates.values(), resolved),
            )
            updated = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            return self._job_dto(updated)

    def invalidate_stage_and_downstream(
        self,
        job_id: str,
        stage_name: str,
        *,
        lease_owner: str,
        safe_error_code: str,
        safe_error_message: object,
    ) -> dict[str, Any] | None:
        """Atomically reset invalid completed work and every dependent stage."""
        now = self._now_text()
        with self._mutation() as connection:
            resolved = self._resolve_job_id(connection, job_id)
            if resolved is None:
                return None
            job = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            if not self._lease_allows(job, lease_owner, now):
                return None
            if JobState(job["state"]) is not JobState.RUNNING:
                return None
            rows = connection.execute(
                """SELECT * FROM pipeline_stages WHERE job_id = ?
                   ORDER BY ordinal, id""",
                (resolved,),
            ).fetchall()
            target = next((row for row in rows if row["name"] == stage_name), None)
            if target is None:
                raise KeyError("Stage was not found")
            old_target = StageState(target["state"])
            if old_target is not StageState.COMPLETED:
                return None
            assert_stage_transition(
                old_target,
                StageState.QUEUED,
                AttemptTrigger.ARTIFACT_INVALIDATION,
                stage_name=stage_name,
            )
            target_ordinal = int(target["ordinal"])
            impacted_ids = {
                int(row["id"])
                for row in rows
                if row["parent_stage_id"] is None
                and int(row["ordinal"]) >= target_ordinal
            }
            changed = True
            while changed:
                changed = False
                for row in rows:
                    parent_id = row["parent_stage_id"]
                    row_id = int(row["id"])
                    if (
                        parent_id is not None
                        and int(parent_id) in impacted_ids
                        and row_id not in impacted_ids
                    ):
                        impacted_ids.add(row_id)
                        changed = True

            for row in rows:
                row_id = int(row["id"])
                if row_id not in impacted_ids:
                    continue
                is_target = row_id == int(target["id"])
                state = StageState.QUEUED if is_target else StageState.PENDING
                retry_cycle = int(row["retry_cycle"]) + (1 if is_target else 0)
                connection.execute(
                    """UPDATE pipeline_stages SET state = ?, retry_cycle = ?,
                           updated_at = ?, started_at = NULL, finished_at = NULL,
                           progress_numerator = 0, progress_denominator = 1,
                           progress_unit = '', warnings_json = '[]',
                           output_manifest_json = '{}', safe_error_code = NULL,
                           safe_error_message = NULL, retryable = 0,
                           next_action = NULL WHERE id = ?""",
                    (state.value, retry_cycle, now, row_id),
                )

            message = _safe_text(safe_error_message)
            code = _safe_text(safe_error_code)
            connection.execute(
                """UPDATE job_runs SET state = 'needs_attention', current_stage = ?,
                       updated_at = ?, finished_at = NULL, next_action = 'retry',
                       safe_error_code = ?, safe_error_message = ?, error_retryable = 0,
                       lease_owner = NULL, lease_expires_at = NULL
                   WHERE id = ? AND state = 'running'""",
                (stage_name, now, code, message, resolved),
            )
            self._insert_event(
                connection,
                resolved,
                stage_id=int(target["id"]),
                event_type="artifact_validation_failed",
                message=message,
                data={
                    "stage": stage_name,
                    "trigger": AttemptTrigger.ARTIFACT_INVALIDATION.value,
                },
                created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?", (resolved,)
            ).fetchone()
            return self._job_dto(updated)

    @staticmethod
    def _lease_allows(
        job: sqlite3.Row,
        lease_owner: str | None,
        now: str,
    ) -> bool:
        stored_owner = job["lease_owner"]
        if stored_owner is None:
            return lease_owner is None
        return (
            lease_owner is not None
            and str(lease_owner) == stored_owner
            and job["lease_expires_at"] is not None
            and job["lease_expires_at"] > now
        )

    @staticmethod
    def _cancel_job_work(
        connection: sqlite3.Connection,
        job_id: str,
        now: str,
    ) -> None:
        stages = connection.execute(
            """SELECT DISTINCT state FROM pipeline_stages
               WHERE job_id = ? AND state IN
                 ('pending', 'queued', 'running', 'needs_attention', 'failed')""",
            (job_id,),
        ).fetchall()
        for stage in stages:
            assert_stage_transition(StageState(stage["state"]), StageState.CANCELLED)
        connection.execute(
            """UPDATE pipeline_attempts SET finished_at = ?, outcome = 'cancelled',
                   retryable = 0, diagnostics_json = ?
               WHERE job_id = ? AND finished_at IS NULL""",
            (
                now,
                _safe_json_dump({"reason": "Cancellation was applied."}),
                job_id,
            ),
        )
        connection.execute(
            """UPDATE pipeline_stages SET state = 'cancelled', updated_at = ?,
                   finished_at = ?, retryable = 0, next_action = NULL,
                   safe_error_code = NULL, safe_error_message = NULL
               WHERE job_id = ? AND state IN
                 ('pending', 'queued', 'running', 'needs_attention', 'failed')""",
            (now, now, job_id),
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _mutation(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _now_datetime(self) -> datetime:
        value = self.clock()
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif isinstance(value, datetime):
            parsed = value
        else:
            raise TypeError("Clock must return datetime or ISO-8601 text")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _now_text(self) -> str:
        return self._now_datetime().isoformat()

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    @staticmethod
    def _resolve_job_id(connection: sqlite3.Connection, identifier: str) -> str | None:
        direct = connection.execute(
            "SELECT id FROM job_runs WHERE id = ?", (str(identifier),)
        ).fetchone()
        if direct is not None:
            return str(direct["id"])
        row = connection.execute(
            """SELECT id FROM job_runs WHERE source_imdb_id = ?
               ORDER BY CASE WHEN state IN ('queued', 'running') THEN 0 ELSE 1 END,
                        created_at DESC, id DESC LIMIT 1""",
            (str(identifier),),
        ).fetchone()
        return str(row["id"]) if row is not None else None

    def _require_job_id(self, connection: sqlite3.Connection, identifier: str) -> str:
        resolved = self._resolve_job_id(connection, identifier)
        if resolved is None:
            raise KeyError("Run was not found")
        return resolved

    @staticmethod
    def _stage_id(connection: sqlite3.Connection, job_id: str, stage_name: str) -> int:
        row = connection.execute(
            "SELECT id FROM pipeline_stages WHERE job_id = ? AND name = ?",
            (job_id, stage_name),
        ).fetchone()
        if row is None:
            raise KeyError("Stage was not found")
        return int(row["id"])

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        *,
        event_type: str,
        message: object,
        created_at: str,
        severity: str = "info",
        stage_id: int | None = None,
        attempt_id: int | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> int:
        cursor = connection.execute(
            """INSERT INTO pipeline_events
               (job_id, stage_id, attempt_id, severity, event_type, message, data_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                stage_id,
                attempt_id,
                _safe_text(severity) or "info",
                _safe_text(event_type) or "event",
                _safe_text(message),
                _safe_json_dump(data or {}),
                created_at,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _job_dto(row: sqlite3.Row) -> dict[str, Any]:
        safe_error = None
        if row["safe_error_code"] or row["safe_error_message"]:
            safe_error = {
                "code": row["safe_error_code"],
                "message": row["safe_error_message"],
                "retryable": bool(row["error_retryable"]),
            }
        return {
            "id": row["id"],
            "source_imdb_id": row["source_imdb_id"],
            "query": row["normalized_query"],
            "label": row["label"],
            "state": row["state"],
            "current_stage": row["current_stage"],
            "next_action": row["next_action"],
            "safe_error": safe_error,
            "artifact_summary": _safe_json_value(
                _json_load(row["artifact_summary_json"], {})
            ),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "lease_expires_at": row["lease_expires_at"],
            "cancel_requested": row["cancel_requested_at"] is not None,
        }

    @staticmethod
    def _stage_dto(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "name": row["name"],
            "parent_stage_id": row["parent_stage_id"],
            "ordinal": row["ordinal"],
            "state": row["state"],
            "retry_cycle": row["retry_cycle"],
            "max_auto_attempts": row["max_auto_attempts"],
            "progress": {
                "numerator": row["progress_numerator"],
                "denominator": row["progress_denominator"],
                "unit": row["progress_unit"],
            },
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "updated_at": row["updated_at"],
            "warnings": _safe_json_value(_json_load(row["warnings_json"], [])),
            "output_manifest": _safe_json_value(
                _json_load(row["output_manifest_json"], {})
            ),
            "safe_error": _error_dto(row),
            "retryable": bool(row["retryable"]),
            "next_action": row["next_action"],
        }

    @staticmethod
    def _attempt_dto(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "stage_id": row["stage_id"],
            "candidate_id": row["candidate_id"],
            "retry_cycle": row["retry_cycle"],
            "attempt_number": row["attempt_number"],
            "max_attempts": row["max_attempts"],
            "trigger": row["trigger"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "outcome": row["outcome"],
            "retryable": bool(row["retryable"]),
            "diagnostics": _safe_json_value(_json_load(row["diagnostics_json"], {})),
            "output": _safe_json_value(_json_load(row["output_json"], {})),
        }

    @staticmethod
    def _event_dto(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "stage_id": row["stage_id"],
            "attempt_id": row["attempt_id"],
            "severity": row["severity"],
            "type": row["event_type"],
            "message": row["message"],
            "data": _safe_json_value(_json_load(row["data_json"], {})),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _candidate_dto(row: sqlite3.Row) -> dict[str, Any]:
        omitted = {
            "rank_reasons_json",
            "quality_reasons_json",
            "rejection_reasons_json",
            "artifact_path",
        }
        result = {key: value for key, value in dict(row).items() if key not in omitted}
        result["imdb_match"] = (
            None if row["imdb_match"] is None else bool(row["imdb_match"])
        )
        result["rank_reasons"] = _safe_json_value(
            _json_load(row["rank_reasons_json"], [])
        )
        result["quality_reasons"] = _safe_json_value(
            _json_load(row["quality_reasons_json"], [])
        )
        result["rejection_reasons"] = _safe_json_value(
            _json_load(row["rejection_reasons_json"], [])
        )
        result["artifact_available"] = bool(row["artifact_path"])
        return result

    @staticmethod
    def _decision_dto(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["accepted"] = bool(result["accepted"])
        return _safe_json_value(result)

    @staticmethod
    def _publishing_dto(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "platform": row["platform"],
            "retry_cycle": row["retry_cycle"],
            "attempt_number": row["attempt_number"],
            "max_attempts": row["max_attempts"],
            "trigger": row["trigger"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "outcome": row["outcome"],
            "retryable": bool(row["retryable"]),
            "safe_error": _error_dto(row),
            "remote_id": row["remote_id"],
            "metadata": _safe_json_value(_json_load(row["metadata_json"], {})),
        }

    @staticmethod
    def _cost_dto(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "category": row["category"],
            "provider": row["provider"],
            "amount_usd": row["amount_usd"],
            "units": row["units"],
            "detail": _safe_json_value(_json_load(row["detail_json"], {})),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _release_dto(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "platform": row["platform"],
            "remote_id": row["remote_id"],
            "status": row["status"],
            "uploaded_at": row["uploaded_at"],
            "safe_error": _error_dto(row),
            "metadata": _safe_json_value(_json_load(row["metadata_json"], {})),
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _revenue_dto(row: sqlite3.Row) -> dict[str, Any]:
        return {
            key: row[key]
            for key in (
                "id",
                "job_id",
                "platform",
                "date",
                "views",
                "revenue_usd",
                "likes",
                "comments",
                "shares",
                "fetched_at",
            )
        }

    def _candidate_rows(
        self, connection: sqlite3.Connection, job_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT * FROM subtitle_candidates WHERE job_id = ?
               ORDER BY discovery_cycle, CASE WHEN rank IS NULL THEN 1 ELSE 0 END, rank, id""",
            (job_id,),
        ).fetchall()
        return [self._candidate_dto(row) for row in rows]

    def _event_rows(
        self, connection: sqlite3.Connection, job_id: str
    ) -> list[dict[str, Any]]:
        return [
            self._event_dto(row)
            for row in connection.execute(
                "SELECT * FROM pipeline_events WHERE job_id = ? ORDER BY id", (job_id,)
            )
        ]

    def _decision_rows(
        self, connection: sqlite3.Connection, job_id: str
    ) -> list[dict[str, Any]]:
        return [
            self._decision_dto(row)
            for row in connection.execute(
                "SELECT * FROM admin_decisions WHERE job_id = ? ORDER BY id", (job_id,)
            )
        ]

    def _publishing_rows(
        self, connection: sqlite3.Connection, job_id: str
    ) -> list[dict[str, Any]]:
        return [
            self._publishing_dto(row)
            for row in connection.execute(
                "SELECT * FROM publishing_attempts WHERE job_id = ? ORDER BY id",
                (job_id,),
            )
        ]

    def _cost_rows(
        self, connection: sqlite3.Connection, job_id: str
    ) -> list[dict[str, Any]]:
        return [
            self._cost_dto(row)
            for row in connection.execute(
                "SELECT * FROM costs WHERE job_id = ? ORDER BY created_at, id",
                (job_id,),
            )
        ]

    def _release_rows(
        self, connection: sqlite3.Connection, job_id: str
    ) -> list[dict[str, Any]]:
        return [
            self._release_dto(row)
            for row in connection.execute(
                "SELECT * FROM releases WHERE job_id = ? ORDER BY id", (job_id,)
            )
        ]

    def _revenue_rows(
        self, connection: sqlite3.Connection, job_id: str
    ) -> list[dict[str, Any]]:
        return [
            self._revenue_dto(row)
            for row in connection.execute(
                "SELECT * FROM revenue WHERE job_id = ? ORDER BY date DESC, id DESC",
                (job_id,),
            )
        ]


def _safe_text(value: object) -> str:
    text = sanitize_text(value)
    return _ABSOLUTE_INTERNAL_PATH_RE.sub("[INTERNAL_PATH]", text)


def _safe_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {_safe_text(key): _safe_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_text(value)


def _safe_json_dump(value: Any) -> str:
    return json.dumps(_safe_json_value(value), sort_keys=True, separators=(",", ":"))


def _json_load(value: object, default: Any) -> Any:
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _error_dto(row: sqlite3.Row) -> dict[str, Any] | None:
    code = row["safe_error_code"]
    message = row["safe_error_message"]
    if not code and not message:
        return None
    return {"code": code, "message": message}


def _enum_value(
    enum_type: type[JobState] | type[StageState] | type[AttemptTrigger], value: Any
) -> str:
    return enum_type(value).value


def _optional_trigger(value: AttemptTrigger | str | None) -> AttemptTrigger | None:
    return None if value is None else AttemptTrigger(value)


def _normalize_query(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _legacy_job_state(value: object) -> str:
    text = str(value or "queued").lower()
    if text in {"done", "complete", "completed", "success"}:
        return "completed"
    if text in {"failed", "error"}:
        return "failed"
    if text in {"cancelled", "canceled"}:
        return "cancelled"
    if text in {"needs_attention", "attention"}:
        return "needs_attention"
    if text in {
        "fetching",
        "analysing",
        "analyzing",
        "rendering",
        "encoding",
        "running",
    }:
        return "running"
    return "queued"


def _default_store() -> OperationStore:
    store = OperationStore(DB_PATH)
    store.initialize()
    return store


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    """Compatibility connection helper configured like store connections."""
    store = _default_store()
    with store._connection() as connection:
        connection.execute("BEGIN")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def init_db() -> None:
    _default_store()


def _legacy_job_dto(
    store: OperationStore, identifier: str, row: dict[str, Any] | None
) -> dict[str, Any] | None:
    if row is None:
        return None
    with store._connection() as connection:
        resolved = store._resolve_job_id(connection, identifier)
        persisted = connection.execute(
            "SELECT * FROM job_runs WHERE id = ?", (resolved,)
        ).fetchone()
        payload = _json_load(persisted["legacy_payload_json"], {})
    status = "done" if row["state"] == "completed" else row["state"]
    return {
        "imdb_id": persisted["source_imdb_id"] or persisted["id"],
        "id": persisted["id"],
        "label": row["label"],
        "query": row["query"],
        "status": status,
        "progress": persisted["legacy_progress"] or 0,
        "message": persisted["legacy_message"] or "",
        "error": row["safe_error"]["message"] if row["safe_error"] else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "video_path": None,
        "analysis_json": _json_load(
            payload.get("analysis_json"), payload.get("analysis_json")
        ),
        "movie_info": _json_load(payload.get("movie_info"), payload.get("movie_info")),
        "segment_timing": _json_load(
            payload.get("segment_timing"), payload.get("segment_timing")
        ),
    }


def upsert_job(
    imdb_id: str,
    label: str,
    query: str = "",
    status: str = "queued",
    progress: int = 0,
    message: str = "Queued — starting pipeline…",
) -> dict[str, Any]:
    store = _default_store()
    row, _created = store.create_or_get_active_job(imdb_id, query, label)
    updated = store.compatibility_update_job(
        row["id"], status=status, progress=progress, message=message, label=label
    )
    return _legacy_job_dto(store, imdb_id, updated)


def update_job(imdb_id: str, **fields: Any) -> dict[str, Any] | None:
    store = _default_store()
    if not fields:
        return get_job(imdb_id)
    row = store.compatibility_update_job(imdb_id, **fields)
    return _legacy_job_dto(store, imdb_id, row)


def get_job(imdb_id: str) -> dict[str, Any] | None:
    store = _default_store()
    return _legacy_job_dto(store, imdb_id, store.get_job(imdb_id))


def list_jobs(
    limit: int = 100, offset: int = 0, status: str | None = None
) -> list[dict[str, Any]]:
    store = _default_store()
    state = "completed" if status == "done" else status
    page = store.list_jobs(state=state, limit=limit, offset=offset)
    items = [_legacy_job_dto(store, row["id"], row) for row in page["items"]]
    for item in items:
        item["analysis_json"] = None
        item["movie_info"] = None
        item["segment_timing"] = None
        item["video_path"] = None
    return items


def record_step(
    imdb_id: str,
    step_name: str,
    status: str = "running",
    message: str = "",
    warnings: list[str] | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    store = _default_store()
    stage_state = {
        "done": StageState.COMPLETED,
        "completed": StageState.COMPLETED,
        "failed": StageState.FAILED,
        "running": StageState.RUNNING,
        "pending": StageState.PENDING,
    }.get(status, StageState.PENDING)
    existing = store.ensure_stage(imdb_id, step_name, state=stage_state)
    with store._mutation() as connection:
        connection.execute(
            """UPDATE pipeline_stages SET state = ?, started_at = COALESCE(?, started_at),
                   finished_at = ?, updated_at = ?, warnings_json = ?,
                   safe_error_message = ? WHERE id = ?""",
            (
                stage_state.value,
                started_at
                or (store._now_text() if stage_state is StageState.RUNNING else None),
                finished_at,
                store._now_text(),
                _safe_json_dump(warnings or []),
                _safe_text(message) if stage_state is StageState.FAILED else None,
                existing["id"],
            ),
        )
        row = connection.execute(
            "SELECT * FROM pipeline_stages WHERE id = ?", (existing["id"],)
        ).fetchone()
        result = store._stage_dto(row)
    return {
        "id": result["id"],
        "imdb_id": imdb_id,
        "step_name": result["name"],
        "status": "done" if result["state"] == "completed" else result["state"],
        "started_at": result["started_at"],
        "finished_at": result["finished_at"],
        "duration_ms": duration_ms,
        "message": message,
        "warnings": result["warnings"],
    }


def get_steps(imdb_id: str) -> list[dict[str, Any]]:
    store = _default_store()
    detail = store.get_job_detail(imdb_id)
    if detail is None:
        return []
    return [
        {
            "id": row["id"],
            "imdb_id": imdb_id,
            "step_name": row["name"],
            "status": "done" if row["state"] == "completed" else row["state"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "duration_ms": None,
            "message": row["safe_error"]["message"] if row["safe_error"] else "",
            "warnings": row["warnings"],
        }
        for row in detail["stages"]
    ]


def record_cost(
    imdb_id: str,
    category: str,
    provider: str,
    amount_usd: float = 0,
    units: int = 1,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = _default_store().record_cost(
        imdb_id, category, provider, amount_usd, units, detail
    )
    return {**row, "imdb_id": imdb_id}


def get_costs(imdb_id: str) -> list[dict[str, Any]]:
    return [{**row, "imdb_id": imdb_id} for row in _default_store().list_costs(imdb_id)]


def get_aggregate_costs(
    start: str | None = None, end: str | None = None, group_by: str = "category"
) -> list[dict[str, Any]]:
    return _default_store().aggregate_costs(start=start, end=end, group_by=group_by)


def upsert_release(
    imdb_id: str,
    platform: str,
    status: str = "pending",
    platform_id: str | None = None,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = _default_store().upsert_release(
        imdb_id,
        platform,
        status=status,
        remote_id=platform_id,
        safe_error_message=error,
        metadata=metadata,
    )
    return {**row, "imdb_id": imdb_id, "platform_id": row["remote_id"], "error": error}


def get_releases(imdb_id: str | None = None) -> list[dict[str, Any]]:
    return [
        {
            **row,
            "imdb_id": row["job_id"],
            "platform_id": row["remote_id"],
            "error": row["safe_error"],
        }
        for row in _default_store().list_releases(imdb_id)
    ]


def upsert_revenue(
    imdb_id: str,
    platform: str,
    date: str,
    views: int = 0,
    revenue_usd: float = 0,
    likes: int = 0,
    comments: int = 0,
    shares: int = 0,
) -> dict[str, Any]:
    row = _default_store().upsert_revenue(
        imdb_id,
        platform,
        date,
        views=views,
        revenue_usd=revenue_usd,
        likes=likes,
        comments=comments,
        shares=shares,
    )
    return {**row, "imdb_id": imdb_id}


def get_revenue(imdb_id: str | None = None) -> list[dict[str, Any]]:
    return [
        {**row, "imdb_id": row["job_id"]}
        for row in _default_store().list_revenue(imdb_id)
    ]


def get_platform_stats(imdb_id: str) -> list[dict[str, Any]]:
    rows = _default_store().platform_stats(imdb_id)
    return [{**row, "platform_id": row["remote_id"]} for row in rows]


def get_alerts(limit: int = 50) -> list[dict[str, Any]]:
    store = _default_store()
    failed = store.list_jobs(state=JobState.FAILED, limit=limit)["items"]
    alerts = [
        {
            "imdb_id": row["source_imdb_id"] or row["id"],
            "label": row["label"],
            "alert_type": "job",
            "message": row["safe_error"]["message"]
            if row["safe_error"]
            else "Run failed",
            "timestamp": row["updated_at"],
        }
        for row in failed
    ]
    failed_releases = [
        row for row in store.list_releases() if row["status"] == "failed"
    ]
    alerts.extend(
        {
            "imdb_id": row["job_id"],
            "label": row["job_id"],
            "alert_type": "release",
            "message": f"{row['platform']}: "
            + (row["safe_error"]["message"] if row["safe_error"] else "Unknown error"),
            "timestamp": row["updated_at"],
        }
        for row in failed_releases
    )
    return sorted(alerts, key=lambda row: row["timestamp"] or "", reverse=True)[:limit]
