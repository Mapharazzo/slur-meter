import sqlite3

import pytest

from api import database

OperationStore = getattr(database, "OperationStore", None)


def _create_legacy_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE jobs (
            imdb_id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            query TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued',
            progress INTEGER DEFAULT 0,
            message TEXT DEFAULT '',
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            video_path TEXT,
            analysis_json TEXT,
            movie_info TEXT,
            segment_timing TEXT
        );
        CREATE TABLE job_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imdb_id TEXT NOT NULL,
            step_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            started_at TEXT,
            finished_at TEXT,
            duration_ms INTEGER,
            message TEXT,
            warnings TEXT,
            UNIQUE(imdb_id, step_name)
        );
        CREATE TABLE costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imdb_id TEXT NOT NULL,
            category TEXT NOT NULL,
            provider TEXT NOT NULL,
            amount_usd REAL NOT NULL DEFAULT 0,
            units INTEGER DEFAULT 1,
            detail TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE releases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imdb_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            platform_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            uploaded_at TEXT,
            error TEXT,
            metadata TEXT,
            UNIQUE(imdb_id, platform)
        );
        CREATE TABLE revenue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imdb_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            date TEXT NOT NULL,
            views INTEGER DEFAULT 0,
            revenue_usd REAL DEFAULT 0,
            likes INTEGER DEFAULT 0,
            comments INTEGER DEFAULT 0
        );
        CREATE INDEX idx_jobs_status ON jobs(status);
        CREATE INDEX idx_job_steps_imdb ON job_steps(imdb_id);
        CREATE INDEX idx_costs_imdb ON costs(imdb_id);
        CREATE INDEX idx_costs_category ON costs(category);
        CREATE INDEX idx_releases_imdb ON releases(imdb_id);
        CREATE INDEX idx_revenue_imdb ON revenue(imdb_id);
        CREATE UNIQUE INDEX idx_revenue_unique ON revenue(imdb_id, platform, date);
        """
    )
    timestamp = "2026-07-20T12:00:00+00:00"
    connection.execute(
        """INSERT INTO jobs
           (imdb_id, label, query, status, progress, message, error, created_at,
            updated_at, video_path, analysis_json, movie_info, segment_timing)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "tt0110912",
            "Pulp Fiction",
            "",
            "done",
            100,
            "Complete",
            None,
            timestamp,
            timestamp,
            "/home/operator/slur-meter/output/tt0110912/final.mp4",
            '{"total": 7}',
            '{"Title": "Pulp Fiction"}',
            '{"graph": {"frames": 10}}',
        ),
    )
    connection.execute(
        """INSERT INTO jobs
           (imdb_id, label, query, status, progress, message, error, created_at,
            updated_at, video_path, analysis_json, movie_info, segment_timing)
           VALUES (?, ?, '', 'rendering', 60, 'Rendering', NULL, ?, ?, NULL, NULL, NULL, NULL)""",
        ("tt_active_no_steps", "Interrupted without steps", timestamp, timestamp),
    )
    connection.executemany(
        """INSERT INTO job_steps
           (imdb_id, step_name, status, started_at, finished_at, duration_ms, message, warnings)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                "tt0110912",
                "analysis",
                "done",
                timestamp,
                timestamp,
                50,
                "Analysed",
                "[]",
            ),
            (
                "orphan_legacy",
                "encode",
                "running",
                timestamp,
                None,
                None,
                "Encoding",
                None,
            ),
        ],
    )
    connection.executemany(
        """INSERT INTO costs
           (imdb_id, category, provider, amount_usd, units, detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            ("tt0110912", "tts", "edge", 1.25, 2, '{"voice": "test"}', timestamp),
            ("orphan_cost", "api", "provider", 0.5, 1, None, timestamp),
        ],
    )
    connection.execute(
        """INSERT INTO releases
           (imdb_id, platform, platform_id, status, uploaded_at, error, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            "orphan_release",
            "youtube",
            "remote-1",
            "uploaded",
            timestamp,
            None,
            '{"title": "x"}',
        ),
    )
    connection.execute(
        """INSERT INTO revenue
           (imdb_id, platform, date, views, revenue_usd, likes, comments)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("orphan_revenue", "youtube", "2026-07-20", 123, 4.5, 6, 7),
    )
    connection.commit()
    connection.close()


def test_operation_store_contract_exists():
    assert OperationStore is not None


@pytest.fixture
def migrated_store(tmp_path):
    if OperationStore is None:
        pytest.skip("OperationStore has not been implemented")
    path = tmp_path / "legacy.db"
    _create_legacy_database(path)
    store = OperationStore(path)
    store.initialize()
    return store


def test_legacy_jobs_children_and_orphans_are_preserved(migrated_store):
    assert migrated_store.get_job("tt0110912")["state"] == "completed"

    recovered_ids = {"orphan_legacy", "orphan_cost", "orphan_release", "orphan_revenue"}
    for job_id in recovered_ids:
        assert migrated_store.get_job(job_id)["label"].startswith(
            "Recovered legacy run"
        )

    detail = migrated_store.get_job_detail("tt0110912")
    assert detail["stages"][0]["name"] == "analysis"
    assert detail["attempts"][0]["outcome"] == "completed"
    assert detail["costs"][0]["amount_usd"] == pytest.approx(1.25)

    assert (
        migrated_store.get_job_detail("orphan_release")["releases"][0]["remote_id"]
        == "remote-1"
    )
    assert migrated_store.get_job_detail("orphan_revenue")["revenue"][0]["views"] == 123
    assert migrated_store.foreign_key_violations() == []
    assert migrated_store.schema_versions() == [1, 2, 3]


def test_interrupted_legacy_work_is_queued_with_recovery_history(migrated_store):
    recovered = migrated_store.get_job("orphan_legacy")
    assert recovered["state"] == "queued"

    detail = migrated_store.get_job_detail("orphan_legacy")
    assert detail["stages"][0]["state"] == "queued"
    assert detail["attempts"][0]["outcome"] == "interrupted"
    assert any(event["type"] == "restart_recovery" for event in detail["events"])

    no_steps = migrated_store.get_job_detail("tt_active_no_steps")
    assert no_steps["run"]["state"] == "queued"
    assert any(event["type"] == "restart_recovery" for event in no_steps["events"])


def test_initialize_is_idempotent_and_does_not_duplicate_migrated_history(
    migrated_store,
):
    before = migrated_store.get_job_detail("tt0110912")

    migrated_store.initialize()

    after = migrated_store.get_job_detail("tt0110912")
    assert migrated_store.schema_versions() == [1, 2, 3]
    assert after["stages"] == before["stages"]
    assert after["attempts"] == before["attempts"]
    assert after["events"] == before["events"]


def test_fresh_database_has_versioned_foreign_key_safe_schema(tmp_path):
    if OperationStore is None:
        pytest.skip("OperationStore has not been implemented")
    store = OperationStore(tmp_path / "fresh.db")

    store.initialize()

    assert store.schema_versions() == [1, 2, 3]
    assert store.foreign_key_violations() == []
    with sqlite3.connect(store.path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "job_runs",
        "pipeline_stages",
        "subtitle_candidates",
        "pipeline_attempts",
        "pipeline_events",
        "admin_decisions",
        "publishing_attempts",
        "costs",
        "releases",
        "revenue",
    } <= tables


def test_v3_migration_marks_unfinished_lease_less_publication_ambiguous(tmp_path):
    path = tmp_path / "v2-active-publication.db"
    store = OperationStore(path)
    store.initialize()
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.request_publication(job["id"], "youtube", metadata={"title": "Stable"})
    store.claim_publishing_attempt(job["id"], "youtube", retry_cycle=1)
    with store._connection() as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = 3")
        connection.execute(
            "ALTER TABLE publishing_attempts DROP COLUMN lease_expires_at"
        )
        connection.execute("ALTER TABLE publishing_attempts DROP COLUMN lease_owner")

    store.initialize()

    detail = store.get_job_detail(job["id"])
    assert store.schema_versions() == [1, 2, 3]
    assert detail["publishing_attempts"][0]["outcome"] == "ambiguous"
    assert detail["publishing_attempts"][0]["finished_at"] is not None
    assert detail["releases"][0]["status"] == "needs_attention"


def test_v3_migrates_helper_shaped_attempt_without_release_to_attention(tmp_path):
    path = tmp_path / "v2-helper-attempt.db"
    store, job_id = _prepare_v2_helper_attempt(path)

    store.initialize()

    detail = store.get_job_detail(job_id)
    assert detail["publishing_attempts"][0]["outcome"] == "ambiguous"
    assert len(detail["releases"]) == 1
    assert detail["releases"][0]["status"] == "needs_attention"
    assert detail["releases"][0]["remote_id"] is None
    assert detail["releases"][0]["metadata"] == {
        "title": "Legacy helper metadata"
    }


def test_v3_preserves_release_reconciled_after_historical_ambiguity(tmp_path):
    path = tmp_path / "v2-reconciled-publication.db"
    store = OperationStore(path)
    store.initialize()
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.upsert_release(
        job["id"], "youtube", status="uploaded", remote_id="confirmed-remote"
    )
    with store._connection() as connection:
        connection.execute(
            """INSERT INTO publishing_attempts
               (job_id, platform, retry_cycle, attempt_number, max_attempts,
                trigger, started_at, finished_at, outcome, retryable,
                safe_error_code, safe_error_message, metadata_json)
               VALUES (?, 'youtube', 1, 1, 3, 'automatic', ?, ?, 'ambiguous', 0,
                       'ambiguous_publish_outcome', 'Already reconciled', '{}')""",
            (
                job["id"],
                "2026-07-21T12:00:00+00:00",
                "2026-07-21T12:01:00+00:00",
            ),
        )
        connection.execute("DELETE FROM schema_migrations WHERE version = 3")
        connection.execute(
            "ALTER TABLE publishing_attempts DROP COLUMN lease_expires_at"
        )
        connection.execute("ALTER TABLE publishing_attempts DROP COLUMN lease_owner")

    store.initialize()

    release = store.get_job_detail(job["id"])["releases"][0]
    assert release["status"] == "uploaded"
    assert release["remote_id"] == "confirmed-remote"
    assert release["safe_error"] is None


def test_v3_failure_rolls_back_alter_and_attempt_release_backfill(tmp_path):
    path = tmp_path / "v3-rollback.db"
    store, _ = _prepare_v2_helper_attempt(path)
    with store._connection() as connection:
        connection.execute(
            """CREATE TRIGGER reject_v3_release_recovery
               BEFORE INSERT ON releases
               BEGIN
                   SELECT RAISE(ABORT, 'injected v3 backfill failure');
               END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected v3 backfill failure"):
        store.initialize()

    with sqlite3.connect(path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(publishing_attempts)")
        }
        attempt = connection.execute(
            "SELECT outcome, finished_at FROM publishing_attempts"
        ).fetchone()
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        release_count = connection.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
    assert "lease_owner" not in columns
    assert "lease_expires_at" not in columns
    assert attempt == ("running", None)
    assert versions == [(1,), (2,)]
    assert release_count == 0


def _prepare_v2_helper_attempt(path):
    store = OperationStore(path)
    store.initialize()
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    with store._connection() as connection:
        connection.execute(
            """INSERT INTO publishing_attempts
               (job_id, platform, retry_cycle, attempt_number, max_attempts,
                trigger, started_at, metadata_json, lease_owner, lease_expires_at)
               VALUES (?, 'youtube', 1, 1, 3, 'automatic', ?, ?, NULL, NULL)""",
            (job["id"], "2026-07-22T12:00:00+00:00", '{"title":"Legacy helper metadata"}'),
        )
        connection.execute("DELETE FROM schema_migrations WHERE version = 3")
        connection.execute(
            "ALTER TABLE publishing_attempts DROP COLUMN lease_expires_at"
        )
        connection.execute("ALTER TABLE publishing_attempts DROP COLUMN lease_owner")
    return store, job["id"]


def test_failed_migration_rolls_back_schema_and_data_changes(tmp_path):
    path = tmp_path / "rollback.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
        INSERT INTO schema_migrations VALUES (1, 'legacy_schema_detected', '2026-07-20');
        CREATE TRIGGER reject_second_migration
        BEFORE INSERT ON schema_migrations
        WHEN NEW.version = 2
        BEGIN
            SELECT RAISE(ABORT, 'injected migration failure');
        END;
        CREATE TABLE jobs (
            imdb_id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO jobs VALUES (
            'tt0110912', 'Pulp Fiction', 'done', '2026-07-20', '2026-07-20'
        );
        """
    )
    connection.commit()
    connection.close()
    store = OperationStore(path)

    with pytest.raises(sqlite3.IntegrityError, match="injected migration failure"):
        store.initialize()

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert (
            connection.execute("SELECT label FROM jobs").fetchone()[0] == "Pulp Fiction"
        )
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,)]
    assert "jobs" in tables
    assert "job_runs" not in tables
