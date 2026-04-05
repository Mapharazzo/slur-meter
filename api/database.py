"""SQLite persistence layer for the admin dashboard.

Database file: data/slur_meter.db
All timestamps are ISO 8601 UTC strings.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "slur_meter.db"

# ─── Connection ──────────────────────────────────────

@contextmanager
def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    # Deserialize JSON columns
    for col in ("analysis_json", "movie_info", "segment_timing", "warnings", "detail", "metadata"):
        if col in d and isinstance(d[col], str):
            try:
                d[col] = json.loads(d[col])
            except (json.JSONDecodeError, TypeError):
                pass
    return d


# ─── Schema ──────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    imdb_id        TEXT PRIMARY KEY,
    label          TEXT NOT NULL,
    query          TEXT DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'queued',
    progress       INTEGER DEFAULT 0,
    message        TEXT DEFAULT '',
    error          TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    video_path     TEXT,
    analysis_json  TEXT,
    movie_info     TEXT,
    segment_timing TEXT
);

CREATE TABLE IF NOT EXISTS job_steps (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    imdb_id      TEXT NOT NULL REFERENCES jobs(imdb_id) ON DELETE CASCADE,
    step_name    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    started_at   TEXT,
    finished_at  TEXT,
    duration_ms  INTEGER,
    message      TEXT,
    warnings     TEXT,
    UNIQUE(imdb_id, step_name)
);

CREATE TABLE IF NOT EXISTS costs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    imdb_id     TEXT NOT NULL REFERENCES jobs(imdb_id) ON DELETE CASCADE,
    category    TEXT NOT NULL,
    provider    TEXT NOT NULL,
    amount_usd  REAL NOT NULL DEFAULT 0.0,
    units       INTEGER DEFAULT 1,
    detail      TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS releases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    imdb_id     TEXT NOT NULL REFERENCES jobs(imdb_id) ON DELETE CASCADE,
    platform    TEXT NOT NULL,
    platform_id TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    uploaded_at TEXT,
    error       TEXT,
    metadata    TEXT,
    UNIQUE(imdb_id, platform)
);

CREATE TABLE IF NOT EXISTS revenue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    imdb_id     TEXT NOT NULL REFERENCES jobs(imdb_id) ON DELETE CASCADE,
    platform    TEXT NOT NULL,
    date        TEXT NOT NULL,
    views       INTEGER DEFAULT 0,
    revenue_usd REAL DEFAULT 0.0,
    likes       INTEGER DEFAULT 0,
    comments    INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_job_steps_imdb ON job_steps(imdb_id);
CREATE INDEX IF NOT EXISTS idx_costs_imdb ON costs(imdb_id);
CREATE INDEX IF NOT EXISTS idx_costs_category ON costs(category);
CREATE INDEX IF NOT EXISTS idx_releases_imdb ON releases(imdb_id);
CREATE INDEX IF NOT EXISTS idx_revenue_imdb ON revenue(imdb_id);
"""


def init_db():
    with get_db() as conn:
        conn.executescript(_SCHEMA)


# ─── Jobs CRUD ───────────────────────────────────────

def upsert_job(
    imdb_id: str,
    label: str,
    query: str = "",
    status: str = "queued",
    progress: int = 0,
    message: str = "Queued — starting pipeline…",
) -> dict:
    now = _now()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO jobs (imdb_id, label, query, status, progress, message, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(imdb_id) DO UPDATE SET
                 label=excluded.label, query=excluded.query,
                 status=excluded.status, progress=excluded.progress,
                 message=excluded.message, error=NULL,
                 video_path=NULL, analysis_json=NULL, movie_info=NULL, segment_timing=NULL,
                 updated_at=excluded.updated_at""",
            (imdb_id, label, query, status, progress, message, now, now),
        )
        # Clear old steps/costs on resubmit
        conn.execute("DELETE FROM job_steps WHERE imdb_id = ?", (imdb_id,))
        conn.execute("DELETE FROM costs WHERE imdb_id = ?", (imdb_id,))
        return _row_to_dict(conn.execute("SELECT * FROM jobs WHERE imdb_id = ?", (imdb_id,)).fetchone())


def update_job(imdb_id: str, **fields) -> dict | None:
    if not fields:
        return get_job(imdb_id)
    # Serialize JSON columns
    for col in ("analysis_json", "movie_info", "segment_timing"):
        if col in fields and not isinstance(fields[col], str):
            fields[col] = json.dumps(fields[col], default=str)
    fields["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [imdb_id]
    with get_db() as conn:
        conn.execute(f"UPDATE jobs SET {set_clause} WHERE imdb_id = ?", values)
        return _row_to_dict(conn.execute("SELECT * FROM jobs WHERE imdb_id = ?", (imdb_id,)).fetchone())


def get_job(imdb_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE imdb_id = ?", (imdb_id,)).fetchone()
        return _row_to_dict(row)


def list_jobs(limit: int = 100, offset: int = 0, status: str | None = None) -> list[dict]:
    with get_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


# ─── Job Steps ───────────────────────────────────────

def record_step(
    imdb_id: str,
    step_name: str,
    status: str = "running",
    message: str = "",
    warnings: list[str] | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_ms: int | None = None,
) -> dict:
    warnings_json = json.dumps(warnings) if warnings else None
    with get_db() as conn:
        conn.execute(
            """INSERT INTO job_steps (imdb_id, step_name, status, started_at, finished_at, duration_ms, message, warnings)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(imdb_id, step_name) DO UPDATE SET
                 status=excluded.status,
                 started_at=COALESCE(excluded.started_at, job_steps.started_at),
                 finished_at=excluded.finished_at,
                 duration_ms=excluded.duration_ms,
                 message=excluded.message,
                 warnings=COALESCE(excluded.warnings, job_steps.warnings)""",
            (imdb_id, step_name, status, started_at or _now(), finished_at, duration_ms, message, warnings_json),
        )
        row = conn.execute(
            "SELECT * FROM job_steps WHERE imdb_id = ? AND step_name = ?",
            (imdb_id, step_name),
        ).fetchone()
        return _row_to_dict(row)


def get_steps(imdb_id: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM job_steps WHERE imdb_id = ? ORDER BY id",
            (imdb_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


# ─── Costs ───────────────────────────────────────────

def record_cost(
    imdb_id: str,
    category: str,
    provider: str,
    amount_usd: float = 0.0,
    units: int = 1,
    detail: dict | None = None,
) -> dict:
    detail_json = json.dumps(detail) if detail else None
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO costs (imdb_id, category, provider, amount_usd, units, detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (imdb_id, category, provider, amount_usd, units, detail_json, _now()),
        )
        row = conn.execute("SELECT * FROM costs WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_dict(row)


def get_costs(imdb_id: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM costs WHERE imdb_id = ? ORDER BY created_at",
            (imdb_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_aggregate_costs(
    start: str | None = None,
    end: str | None = None,
    group_by: str = "category",
) -> list[dict]:
    with get_db() as conn:
        conditions = []
        params: list[Any] = []
        if start:
            conditions.append("created_at >= ?")
            params.append(start)
        if end:
            conditions.append("created_at <= ?")
            params.append(end)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        if group_by == "day":
            group_col = "substr(created_at, 1, 10)"
        elif group_by == "week":
            group_col = "substr(created_at, 1, 10)"  # Approximate; SQLite lacks ISO week
        elif group_by == "month":
            group_col = "substr(created_at, 1, 7)"
        else:
            group_col = "category"

        rows = conn.execute(
            f"""SELECT {group_col} as period, category, provider,
                       SUM(amount_usd) as total_usd, SUM(units) as total_units,
                       COUNT(*) as count
                FROM costs {where}
                GROUP BY {group_col}, category, provider
                ORDER BY period, category""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


# ─── Releases ────────────────────────────────────────

def upsert_release(
    imdb_id: str,
    platform: str,
    status: str = "pending",
    platform_id: str | None = None,
    error: str | None = None,
    metadata: dict | None = None,
) -> dict:
    metadata_json = json.dumps(metadata) if metadata else None
    uploaded_at = _now() if status == "uploaded" else None
    with get_db() as conn:
        conn.execute(
            """INSERT INTO releases (imdb_id, platform, status, platform_id, uploaded_at, error, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(imdb_id, platform) DO UPDATE SET
                 status=excluded.status, platform_id=COALESCE(excluded.platform_id, releases.platform_id),
                 uploaded_at=COALESCE(excluded.uploaded_at, releases.uploaded_at),
                 error=excluded.error, metadata=COALESCE(excluded.metadata, releases.metadata)""",
            (imdb_id, platform, status, platform_id, uploaded_at, error, metadata_json),
        )
        row = conn.execute(
            "SELECT * FROM releases WHERE imdb_id = ? AND platform = ?",
            (imdb_id, platform),
        ).fetchone()
        return _row_to_dict(row)


def get_releases(imdb_id: str | None = None) -> list[dict]:
    with get_db() as conn:
        if imdb_id:
            rows = conn.execute(
                "SELECT * FROM releases WHERE imdb_id = ? ORDER BY id", (imdb_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM releases ORDER BY id DESC").fetchall()
        return [_row_to_dict(r) for r in rows]


# ─── Alerts ──────────────────────────────────────────

def get_alerts(limit: int = 50) -> list[dict]:
    with get_db() as conn:
        failed_jobs = conn.execute(
            """SELECT imdb_id, label, 'job' as alert_type, error as message, updated_at as timestamp
               FROM jobs WHERE status = 'failed'
               ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        failed_releases = conn.execute(
            """SELECT r.imdb_id, j.label, 'release' as alert_type,
                      r.platform || ': ' || COALESCE(r.error, 'Unknown error') as message,
                      r.uploaded_at as timestamp
               FROM releases r JOIN jobs j ON r.imdb_id = j.imdb_id
               WHERE r.status = 'failed'
               ORDER BY r.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        alerts = [dict(r) for r in failed_jobs] + [dict(r) for r in failed_releases]
        alerts.sort(key=lambda a: a.get("timestamp", ""), reverse=True)
        return alerts[:limit]


# ─── Revenue (stubbed) ──────────────────────────────

def get_revenue(imdb_id: str | None = None) -> list[dict]:
    with get_db() as conn:
        if imdb_id:
            rows = conn.execute(
                "SELECT * FROM revenue WHERE imdb_id = ? ORDER BY date DESC", (imdb_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM revenue ORDER BY date DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]
