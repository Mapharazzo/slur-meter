"""Ordered, transactional SQLite migrations for durable operations."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from typing import Any

from api.errors import sanitize_text

Migration = Callable[[sqlite3.Connection, str], None]
_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![:/A-Za-z0-9_])/(?:[^/\s]+/)*[^\s,;\"']+|"
    r"\b[A-Za-z]:\\(?:[^\\\s]+\\)*[^\s,;\"']+"
)


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE job_runs (
        id TEXT PRIMARY KEY,
        source_imdb_id TEXT,
        normalized_query TEXT NOT NULL DEFAULT '',
        submission_key TEXT NOT NULL,
        label TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN
            ('queued', 'running', 'needs_attention', 'failed', 'cancelled', 'completed')),
        current_stage TEXT,
        next_action TEXT,
        safe_error_code TEXT,
        safe_error_message TEXT,
        error_retryable INTEGER NOT NULL DEFAULT 0 CHECK (error_retryable IN (0, 1)),
        artifact_summary_json TEXT NOT NULL DEFAULT '{}',
        legacy_progress INTEGER,
        legacy_message TEXT,
        legacy_payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        lease_owner TEXT,
        lease_expires_at TEXT,
        cancel_requested_at TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX idx_job_runs_active_submission
    ON job_runs(submission_key)
    WHERE state IN ('queued', 'running')
    """,
    "CREATE INDEX idx_job_runs_state_updated ON job_runs(state, updated_at DESC)",
    "CREATE INDEX idx_job_runs_source ON job_runs(source_imdb_id, created_at DESC)",
    """
    CREATE TABLE pipeline_stages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES job_runs(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        parent_stage_id INTEGER,
        ordinal INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL CHECK (state IN
            ('pending', 'queued', 'running', 'needs_attention', 'failed',
             'cancelled', 'completed', 'skipped')),
        retry_cycle INTEGER NOT NULL DEFAULT 1 CHECK (retry_cycle >= 1),
        max_auto_attempts INTEGER NOT NULL DEFAULT 1 CHECK (max_auto_attempts >= 1),
        progress_numerator INTEGER,
        progress_denominator INTEGER,
        progress_unit TEXT,
        started_at TEXT,
        finished_at TEXT,
        updated_at TEXT NOT NULL,
        warnings_json TEXT NOT NULL DEFAULT '[]',
        output_manifest_json TEXT NOT NULL DEFAULT '{}',
        safe_error_code TEXT,
        safe_error_message TEXT,
        retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1)),
        next_action TEXT,
        UNIQUE(job_id, name),
        UNIQUE(id, job_id),
        FOREIGN KEY(parent_stage_id, job_id)
            REFERENCES pipeline_stages(id, job_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX idx_pipeline_stages_job_ordinal ON pipeline_stages(job_id, ordinal, id)",
    """
    CREATE TABLE subtitle_candidates (
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES job_runs(id) ON DELETE CASCADE,
        provider TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        provider_filename TEXT,
        source_type TEXT NOT NULL DEFAULT 'provider',
        language TEXT,
        fps REAL,
        title TEXT,
        year INTEGER,
        imdb_match INTEGER,
        provider_rating REAL,
        provider_download_count INTEGER,
        discovery_cycle INTEGER NOT NULL DEFAULT 1,
        rank INTEGER,
        rank_reasons_json TEXT NOT NULL DEFAULT '[]',
        detected_encoding TEXT,
        cue_count INTEGER,
        first_cue_seconds REAL,
        final_cue_seconds REAL,
        parsed_duration_seconds REAL,
        expected_runtime_seconds REAL,
        coverage_percent REAL,
        download_error TEXT,
        parse_error TEXT,
        quality_reasons_json TEXT NOT NULL DEFAULT '[]',
        rejection_reasons_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'discovered',
        content_hash TEXT,
        artifact_path TEXT,
        selected_at TEXT,
        selection_method TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(job_id, provider, provider_id, discovery_cycle),
        UNIQUE(id, job_id)
    )
    """,
    "CREATE INDEX idx_candidates_rank ON subtitle_candidates(job_id, discovery_cycle, rank, id)",
    """
    CREATE TABLE pipeline_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES job_runs(id) ON DELETE CASCADE,
        stage_id INTEGER NOT NULL,
        candidate_id TEXT,
        retry_cycle INTEGER NOT NULL CHECK (retry_cycle >= 1),
        attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
        max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
        trigger TEXT NOT NULL CHECK (trigger IN
            ('automatic', 'manual_retry', 'resume', 'restart_recovery')),
        started_at TEXT NOT NULL,
        finished_at TEXT,
        outcome TEXT NOT NULL DEFAULT 'running',
        retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1)),
        diagnostics_json TEXT NOT NULL DEFAULT '{}',
        output_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(stage_id, retry_cycle, attempt_number),
        UNIQUE(id, job_id),
        FOREIGN KEY(stage_id, job_id)
            REFERENCES pipeline_stages(id, job_id) ON DELETE CASCADE,
        FOREIGN KEY(candidate_id, job_id)
            REFERENCES subtitle_candidates(id, job_id)
    )
    """,
    "CREATE INDEX idx_pipeline_attempts_job ON pipeline_attempts(job_id, id)",
    """
    CREATE UNIQUE INDEX idx_pipeline_attempts_active
    ON pipeline_attempts(stage_id)
    WHERE finished_at IS NULL
    """,
    """
    CREATE TABLE pipeline_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES job_runs(id) ON DELETE CASCADE,
        stage_id INTEGER,
        attempt_id INTEGER,
        severity TEXT NOT NULL DEFAULT 'info',
        event_type TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',
        data_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        FOREIGN KEY(stage_id, job_id)
            REFERENCES pipeline_stages(id, job_id) ON DELETE CASCADE,
        FOREIGN KEY(attempt_id, job_id)
            REFERENCES pipeline_attempts(id, job_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX idx_pipeline_events_job_id ON pipeline_events(job_id, id)",
    """
    CREATE TABLE admin_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES job_runs(id) ON DELETE CASCADE,
        action TEXT NOT NULL,
        target_stage TEXT,
        candidate_id TEXT,
        platform TEXT,
        idempotency_key TEXT,
        accepted INTEGER NOT NULL CHECK (accepted IN (0, 1)),
        reason TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY(candidate_id, job_id)
            REFERENCES subtitle_candidates(id, job_id)
    )
    """,
    """
    CREATE UNIQUE INDEX idx_admin_decisions_idempotency
    ON admin_decisions(job_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL
    """,
    "CREATE INDEX idx_admin_decisions_job ON admin_decisions(job_id, id)",
    """
    CREATE TABLE publishing_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES job_runs(id) ON DELETE CASCADE,
        platform TEXT NOT NULL,
        retry_cycle INTEGER NOT NULL CHECK (retry_cycle >= 1),
        attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
        max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
        trigger TEXT NOT NULL CHECK (trigger IN
            ('automatic', 'manual_retry', 'resume', 'restart_recovery')),
        started_at TEXT NOT NULL,
        finished_at TEXT,
        outcome TEXT NOT NULL DEFAULT 'running',
        retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1)),
        safe_error_code TEXT,
        safe_error_message TEXT,
        remote_id TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        lease_owner TEXT,
        lease_expires_at TEXT,
        UNIQUE(job_id, platform, retry_cycle, attempt_number)
    )
    """,
    "CREATE INDEX idx_publishing_attempts_job ON publishing_attempts(job_id, id)",
    """
    CREATE UNIQUE INDEX idx_publishing_attempts_active
    ON publishing_attempts(job_id, platform)
    WHERE finished_at IS NULL
    """,
    """
    CREATE TABLE costs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES job_runs(id) ON DELETE CASCADE,
        category TEXT NOT NULL,
        provider TEXT NOT NULL,
        amount_usd REAL NOT NULL DEFAULT 0,
        units INTEGER NOT NULL DEFAULT 1,
        detail_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_costs_job ON costs(job_id, created_at, id)",
    "CREATE INDEX idx_costs_category ON costs(category)",
    """
    CREATE TABLE releases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES job_runs(id) ON DELETE CASCADE,
        platform TEXT NOT NULL,
        remote_id TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        uploaded_at TEXT,
        safe_error_code TEXT,
        safe_error_message TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL,
        UNIQUE(job_id, platform)
    )
    """,
    "CREATE INDEX idx_releases_job ON releases(job_id, id)",
    """
    CREATE TABLE revenue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES job_runs(id) ON DELETE CASCADE,
        platform TEXT NOT NULL,
        date TEXT NOT NULL,
        views INTEGER NOT NULL DEFAULT 0,
        revenue_usd REAL NOT NULL DEFAULT 0,
        likes INTEGER NOT NULL DEFAULT 0,
        comments INTEGER NOT NULL DEFAULT 0,
        shares INTEGER NOT NULL DEFAULT 0,
        fetched_at TEXT,
        UNIQUE(job_id, platform, date)
    )
    """,
    "CREATE INDEX idx_revenue_job ON revenue(job_id, date DESC, id)",
)


def apply_migrations(connection: sqlite3.Connection, now: str) -> None:
    """Apply all pending migrations atomically under an immediate write lock."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version INTEGER PRIMARY KEY,
                   name TEXT NOT NULL,
                   applied_at TEXT NOT NULL
               )"""
        )
        applied = {
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
        migrations: tuple[tuple[int, str, Migration], ...] = (
            (1, "legacy_schema_detected", _mark_legacy_schema),
            (2, "operational_schema", _migrate_operational_schema),
            (3, "publishing_attempt_leases", _add_publishing_attempt_leases),
        )
        for version, name, migration in migrations:
            if version in applied:
                continue
            migration(connection, now)
            connection.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, now),
            )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("Migration left foreign-key violations")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _mark_legacy_schema(connection: sqlite3.Connection, _now: str) -> None:
    """Version one records the explicitly inspected pre-operational baseline."""
    _table_names(connection)


def _add_publishing_attempt_leases(
    connection: sqlite3.Connection, _now: str
) -> None:
    """Add only the liveness fields needed to reconcile abandoned uploads safely."""
    if "publishing_attempts" not in _table_names(connection):
        return
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(publishing_attempts)")
    }
    if "lease_owner" not in columns:
        connection.execute("ALTER TABLE publishing_attempts ADD COLUMN lease_owner TEXT")
    if "lease_expires_at" not in columns:
        connection.execute(
            "ALTER TABLE publishing_attempts ADD COLUMN lease_expires_at TEXT"
        )
    code = "ambiguous_publish_outcome"
    message = "An unfinished pre-lease publishing attempt requires reconciliation."
    affected_ids = [
        int(row[0])
        for row in connection.execute(
            """SELECT id FROM publishing_attempts
               WHERE finished_at IS NULL
                 AND (lease_owner IS NULL OR lease_expires_at IS NULL)"""
        )
    ]
    if not affected_ids:
        return
    placeholders = ", ".join("?" for _ in affected_ids)
    connection.execute(
        f"""UPDATE publishing_attempts SET finished_at = ?, outcome = 'ambiguous',
               retryable = 0, safe_error_code = ?, safe_error_message = ?
           WHERE id IN ({placeholders})""",
        (_now, code, message, *affected_ids),
    )
    connection.execute(
        f"""UPDATE releases SET status = 'needs_attention', safe_error_code = ?,
               safe_error_message = ?, updated_at = ?
           WHERE EXISTS (
               SELECT 1 FROM publishing_attempts attempt
               WHERE attempt.job_id = releases.job_id
                 AND attempt.platform = releases.platform
                 AND attempt.id IN ({placeholders})
           )""",
        (code, message, _now, *affected_ids),
    )
    connection.execute(
        f"""INSERT INTO releases
               (job_id, platform, status, safe_error_code, safe_error_message,
                metadata_json, updated_at)
           SELECT attempt.job_id, attempt.platform, 'needs_attention', ?, ?,
                  attempt.metadata_json, ?
           FROM publishing_attempts attempt
           WHERE attempt.id IN ({placeholders})
             AND NOT EXISTS (
                 SELECT 1 FROM releases
                 WHERE releases.job_id = attempt.job_id
                   AND releases.platform = attempt.platform
             )""",
        (code, message, _now, *affected_ids),
    )


def _migrate_operational_schema(connection: sqlite3.Connection, now: str) -> None:
    tables = _table_names(connection)
    legacy_names: dict[str, str] = {}
    for name in ("jobs", "job_steps", "costs", "releases", "revenue"):
        if name not in tables:
            continue
        legacy_name = f"legacy_{name}_v1"
        connection.execute(f'ALTER TABLE "{name}" RENAME TO "{legacy_name}"')
        legacy_names[name] = legacy_name

    _drop_explicit_indexes(connection, legacy_names.values())

    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)

    if not legacy_names:
        return

    legacy_rows = {
        name: _rows(connection, table_name) for name, table_name in legacy_names.items()
    }
    jobs = {str(row["imdb_id"]): row for row in legacy_rows.get("jobs", [])}
    referenced_ids = set(jobs)
    for child_name in ("job_steps", "costs", "releases", "revenue"):
        referenced_ids.update(
            str(row["imdb_id"])
            for row in legacy_rows.get(child_name, [])
            if row.get("imdb_id") is not None
        )

    steps_by_job: dict[str, list[dict[str, Any]]] = {}
    for step in legacy_rows.get("job_steps", []):
        steps_by_job.setdefault(str(step["imdb_id"]), []).append(step)

    for job_id in sorted(referenced_ids):
        legacy_job = jobs.get(job_id)
        child_steps = steps_by_job.get(job_id, [])
        if legacy_job is None:
            state = (
                "queued"
                if any(_legacy_active(step.get("status")) for step in child_steps)
                else "completed"
            )
            label = f"Recovered legacy run {job_id}"
            created_at = _first_value(child_steps, "started_at") or now
            updated_at = (
                _first_value(reversed(child_steps), "finished_at") or created_at
            )
            payload: dict[str, Any] = {}
            source_imdb_id = job_id
            query = ""
            progress = None
            message = "Recovered from orphaned legacy history."
            safe_error = None
        else:
            state = _job_state(legacy_job.get("status"))
            label = str(legacy_job.get("label") or job_id)
            created_at = str(legacy_job.get("created_at") or now)
            updated_at = str(legacy_job.get("updated_at") or created_at)
            raw_payload = {
                key: legacy_job.get(key)
                for key in (
                    "video_path",
                    "analysis_json",
                    "movie_info",
                    "segment_timing",
                )
                if legacy_job.get(key) is not None
            }
            payload = _safe_json_value(raw_payload)
            source_imdb_id = job_id
            query = str(legacy_job.get("query") or "")
            progress = legacy_job.get("progress")
            message = _safe_text(legacy_job.get("message") or "")
            safe_error = _safe_text(legacy_job.get("error") or "") or None

        artifact_summary = {
            "legacy_video_available": bool(payload.get("video_path")),
            "legacy_analysis_available": bool(payload.get("analysis_json")),
            "legacy_movie_info_available": bool(payload.get("movie_info")),
        }
        connection.execute(
            """INSERT INTO job_runs (
                   id, source_imdb_id, normalized_query, submission_key, label, state,
                   safe_error_message, artifact_summary_json, legacy_progress,
                   legacy_message, legacy_payload_json, created_at, updated_at,
                   started_at, finished_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                source_imdb_id,
                query,
                f"legacy:{job_id}",
                label,
                state,
                safe_error,
                json.dumps(artifact_summary, sort_keys=True),
                progress,
                message,
                json.dumps(payload, default=str, sort_keys=True),
                created_at,
                updated_at,
                created_at
                if state in {"running", "completed", "failed", "needs_attention"}
                else None,
                updated_at if state in {"completed", "failed", "cancelled"} else None,
            ),
        )

    _copy_steps(connection, legacy_rows.get("job_steps", []), now)
    for job_id in sorted(referenced_ids):
        queued = connection.execute(
            "SELECT 1 FROM job_runs WHERE id = ? AND state = 'queued'", (job_id,)
        ).fetchone()
        recovery = connection.execute(
            """SELECT 1 FROM pipeline_events
               WHERE job_id = ? AND event_type = 'restart_recovery' LIMIT 1""",
            (job_id,),
        ).fetchone()
        if queued is not None and recovery is None:
            connection.execute(
                """INSERT INTO pipeline_events
                   (job_id, severity, event_type, message, data_json, created_at)
                   VALUES (?, 'warning', 'restart_recovery', ?, '{}', ?)""",
                (
                    job_id,
                    "Interrupted legacy work was recovered and queued for restart.",
                    now,
                ),
            )
    _copy_costs(connection, legacy_rows.get("costs", []), now)
    _copy_releases(connection, legacy_rows.get("releases", []), now)
    _copy_revenue(connection, legacy_rows.get("revenue", []))

    for legacy_name in reversed(tuple(legacy_names.values())):
        connection.execute(f'DROP TABLE "{legacy_name}"')


def _copy_steps(
    connection: sqlite3.Connection,
    rows: Iterable[dict[str, Any]],
    now: str,
) -> None:
    for ordinal, row in enumerate(rows, start=1):
        job_id = str(row["imdb_id"])
        legacy_status = str(row.get("status") or "pending")
        state = _stage_state(legacy_status)
        updated_at = str(row.get("finished_at") or row.get("started_at") or now)
        warnings = _json_value(row.get("warnings"), [])
        cursor = connection.execute(
            """INSERT INTO pipeline_stages (
                   job_id, name, ordinal, state, retry_cycle, max_auto_attempts,
                   started_at, finished_at, updated_at, warnings_json,
                   safe_error_message
               ) VALUES (?, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?)""",
            (
                job_id,
                str(row.get("step_name") or f"legacy_step_{ordinal}"),
                ordinal,
                state,
                row.get("started_at"),
                row.get("finished_at") if state not in {"queued", "running"} else None,
                updated_at,
                json.dumps(warnings, default=str),
                _safe_text(row.get("message") or "")
                if state in {"failed", "needs_attention"}
                else None,
            ),
        )
        stage_id = int(cursor.lastrowid)
        if row.get("started_at") is None and legacy_status == "pending":
            continue
        interrupted = _legacy_active(legacy_status)
        outcome = (
            "interrupted"
            if interrupted
            else "completed"
            if state == "completed"
            else "failed"
            if state in {"failed", "needs_attention"}
            else state
        )
        attempt = connection.execute(
            """INSERT INTO pipeline_attempts (
                   job_id, stage_id, retry_cycle, attempt_number, max_attempts,
                   trigger, started_at, finished_at, outcome, retryable,
                   diagnostics_json
               ) VALUES (?, ?, 1, 1, 1, ?, ?, ?, ?, 0, ?)""",
            (
                job_id,
                stage_id,
                "restart_recovery" if interrupted else "automatic",
                row.get("started_at") or updated_at,
                updated_at,
                outcome,
                json.dumps(
                    {
                        "legacy_duration_ms": row.get("duration_ms"),
                        "legacy_message": _safe_text(row.get("message") or ""),
                    },
                    sort_keys=True,
                ),
            ),
        )
        event_type = "restart_recovery" if interrupted else "legacy_stage_migrated"
        connection.execute(
            """INSERT INTO pipeline_events
               (job_id, stage_id, attempt_id, severity, event_type, message, data_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, '{}', ?)""",
            (
                job_id,
                stage_id,
                attempt.lastrowid,
                "warning" if interrupted else "info",
                event_type,
                "Interrupted legacy work was recovered for restart."
                if interrupted
                else "Legacy stage history was migrated.",
                updated_at,
            ),
        )


def _copy_costs(
    connection: sqlite3.Connection, rows: Iterable[dict[str, Any]], now: str
) -> None:
    for row in rows:
        connection.execute(
            """INSERT INTO costs
               (id, job_id, category, provider, amount_usd, units, detail_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row.get("id"),
                str(row["imdb_id"]),
                str(row.get("category") or "legacy"),
                str(row.get("provider") or "legacy"),
                float(row.get("amount_usd") or 0),
                int(row.get("units") or 1),
                json.dumps(_json_value(row.get("detail"), {}), default=str),
                str(row.get("created_at") or now),
            ),
        )


def _copy_releases(
    connection: sqlite3.Connection, rows: Iterable[dict[str, Any]], now: str
) -> None:
    for row in rows:
        connection.execute(
            """INSERT INTO releases
               (id, job_id, platform, remote_id, status, uploaded_at,
                safe_error_message, metadata_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row.get("id"),
                str(row["imdb_id"]),
                str(row.get("platform") or "unknown"),
                row.get("platform_id"),
                str(row.get("status") or "pending"),
                row.get("uploaded_at"),
                _safe_text(row.get("error") or "") or None,
                json.dumps(_json_value(row.get("metadata"), {}), default=str),
                str(row.get("uploaded_at") or now),
            ),
        )


def _copy_revenue(
    connection: sqlite3.Connection, rows: Iterable[dict[str, Any]]
) -> None:
    for row in rows:
        connection.execute(
            """INSERT INTO revenue
               (id, job_id, platform, date, views, revenue_usd, likes, comments, shares, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row.get("id"),
                str(row["imdb_id"]),
                str(row.get("platform") or "unknown"),
                str(row.get("date") or ""),
                int(row.get("views") or 0),
                float(row.get("revenue_usd") or 0),
                int(row.get("likes") or 0),
                int(row.get("comments") or 0),
                int(row.get("shares") or 0),
                row.get("fetched_at"),
            ),
        )


def _rows(connection: sqlite3.Connection, table_name: str) -> list[dict[str, Any]]:
    cursor = connection.execute(f'SELECT * FROM "{table_name}"')
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _drop_explicit_indexes(
    connection: sqlite3.Connection,
    table_names: Iterable[str],
) -> None:
    names = tuple(table_names)
    if not names:
        return
    placeholders = ", ".join("?" for _ in names)
    indexes = connection.execute(
        f"""SELECT name FROM sqlite_master
            WHERE type = 'index' AND sql IS NOT NULL AND tbl_name IN ({placeholders})""",
        names,
    ).fetchall()
    for (index_name,) in indexes:
        quoted_name = str(index_name).replace('"', '""')
        connection.execute(f'DROP INDEX "{quoted_name}"')


def _job_state(status: object) -> str:
    value = str(status or "queued").lower()
    if value in {"done", "complete", "completed", "success", "uploaded"}:
        return "completed"
    if value in {"failed", "error"}:
        return "failed"
    if value in {"cancelled", "canceled"}:
        return "cancelled"
    if value in {"needs_attention", "attention"}:
        return "needs_attention"
    return "queued"


def _stage_state(status: object) -> str:
    value = str(status or "pending").lower()
    if value in {"done", "complete", "completed", "success"}:
        return "completed"
    if value in {"failed", "error"}:
        return "failed"
    if value in {"cancelled", "canceled"}:
        return "cancelled"
    if value == "skipped":
        return "skipped"
    if value in {"needs_attention", "attention"}:
        return "needs_attention"
    return "queued"


def _legacy_active(status: object) -> bool:
    return str(status or "").lower() in {
        "queued",
        "pending",
        "fetching",
        "analysing",
        "analyzing",
        "rendering",
        "encoding",
        "running",
    }


def _json_value(value: object, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return _safe_text(value)
    return value


def _safe_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {_safe_text(key): _safe_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_text(value)


def _safe_text(value: object) -> str:
    return _ABSOLUTE_PATH_RE.sub("[INTERNAL_PATH]", sanitize_text(value))


def _first_value(rows: Iterable[dict[str, Any]], key: str) -> Any:
    return next((row.get(key) for row in rows if row.get(key) is not None), None)
