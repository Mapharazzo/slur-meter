from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from api import database
from api.domain import AttemptTrigger, InvalidTransition, JobState, StageState

OperationStore = getattr(database, "OperationStore", None)


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 7, 21, 10, 0, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, *, seconds):
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return MutableClock()


@pytest.fixture
def store(tmp_path, clock):
    if OperationStore is None:
        pytest.skip("OperationStore has not been implemented")
    result = OperationStore(tmp_path / "operations.db", clock=clock)
    result.initialize()
    return result


def test_concurrent_duplicate_submission_returns_one_run(store):
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(
            pool.map(
                lambda _: store.create_or_get_active_job(
                    "tt0110912", "", "Pulp Fiction"
                ),
                range(2),
            )
        )

    assert len({row[0]["id"] for row in rows}) == 1
    assert sum(created for _, created in rows) == 1
    assert rows[0][0]["id"].startswith("job_")
    assert rows[0][0]["id"] != "tt0110912"


def test_completed_submission_gets_a_new_immutable_run_id(store):
    first, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.transition_job(first["id"], JobState.RUNNING, expected_state=JobState.QUEUED)
    store.transition_job(
        first["id"], JobState.COMPLETED, expected_state=JobState.RUNNING
    )

    second, created = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")

    assert created is True
    assert second["id"] != first["id"]
    assert store.get_job(first["id"])["state"] == "completed"


def test_claim_is_atomic_across_workers(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(
                lambda owner: store.claim_next_job(owner, lease_seconds=10),
                ("worker-a", "worker-b"),
            )
        )

    claimed = [row for row in claims if row is not None]
    assert [row["id"] for row in claimed] == [job["id"]]
    assert store.claim_next_job("worker-c", lease_seconds=10) is None


def test_expired_lease_cannot_be_renewed(store, clock):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.claim_next_job("worker-a-token", lease_seconds=10)
    clock.advance(seconds=11)

    assert store.renew_lease(job["id"], "worker-a-token", lease_seconds=10) is False


def test_restart_recovery_closes_attempt_and_requeues_stage_and_job(store, clock):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.ensure_stage(job["id"], "analysis", ordinal=5, state=StageState.QUEUED)
    claimed = store.claim_next_job("worker-a", lease_seconds=10)
    assert claimed["id"] == job["id"]
    store.transition_stage(
        job["id"],
        "analysis",
        StageState.RUNNING,
        expected_state=StageState.QUEUED,
        lease_owner="worker-a",
    )
    attempt = store.start_attempt(
        job["id"], "analysis", max_attempts=1, lease_owner="worker-a"
    )

    clock.advance(seconds=11)
    recovered = store.recover_expired_leases()

    assert recovered == [job["id"]]
    assert store.get_job(job["id"])["state"] == "queued"
    detail = store.get_job_detail(job["id"])
    stage = detail["stages"][0]
    assert stage["state"] == "queued"
    assert stage["retry_cycle"] == 2
    recovered_attempt = next(
        row for row in detail["attempts"] if row["id"] == attempt["id"]
    )
    assert recovered_attempt["outcome"] == "interrupted"
    assert recovered_attempt["trigger"] == "automatic"
    assert any(event["type"] == "restart_recovery" for event in detail["events"])


def test_recovery_is_atomic_when_called_concurrently(store, clock):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.claim_next_job("worker-a", lease_seconds=10)
    clock.advance(seconds=11)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: store.recover_expired_leases(), range(2)))

    assert sum(result == [job["id"]] for result in results) == 1
    assert sum(result == [] for result in results) == 1


def test_stale_worker_is_fenced_after_recovery_and_reclaim(store, clock):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.ensure_stage(job["id"], "analysis", state=StageState.QUEUED)
    store.claim_next_job("worker-a-token", lease_seconds=10)
    store.transition_stage(
        job["id"], "analysis", StageState.RUNNING, lease_owner="worker-a-token"
    )
    attempt = store.start_attempt(job["id"], "analysis", lease_owner="worker-a-token")
    clock.advance(seconds=11)
    store.recover_expired_leases()
    store.claim_next_job("worker-b-token", lease_seconds=10)

    assert (
        store.transition_job(
            job["id"], JobState.COMPLETED, lease_owner="worker-a-token"
        )
        is None
    )
    assert (
        store.finish_attempt(attempt["id"], "completed", lease_owner="worker-a-token")
        is None
    )
    assert store.get_job(job["id"])["state"] == "running"


def test_expired_lease_applies_pending_cancellation_instead_of_requeueing(store, clock):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.ensure_stage(job["id"], "analysis", state=StageState.QUEUED)
    store.claim_next_job("worker-a", lease_seconds=10)
    store.transition_stage(
        job["id"], "analysis", StageState.RUNNING, lease_owner="worker-a"
    )
    attempt = store.start_attempt(job["id"], "analysis", lease_owner="worker-a")
    pending, changed = store.request_cancel(job["id"])
    assert pending["state"] == "running"
    assert pending["cancel_requested"] is True
    assert changed is True
    clock.advance(seconds=11)

    assert store.recover_expired_leases() == [job["id"]]

    detail = store.get_job_detail(job["id"])
    assert detail["run"]["state"] == "cancelled"
    assert detail["stages"][0]["state"] == "cancelled"
    recovered_attempt = next(
        row for row in detail["attempts"] if row["id"] == attempt["id"]
    )
    assert recovered_attempt["outcome"] == "cancelled"


def test_transitions_are_validated_and_compare_and_set(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")

    with pytest.raises(InvalidTransition):
        store.transition_job(job["id"], JobState.COMPLETED)

    assert (
        store.transition_job(
            job["id"], JobState.RUNNING, expected_state=JobState.FAILED
        )
        is None
    )
    assert store.get_job(job["id"])["state"] == "queued"


def test_restart_recovery_is_the_only_running_to_queued_trigger(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.transition_job(job["id"], JobState.RUNNING)

    with pytest.raises(InvalidTransition):
        store.transition_job(job["id"], JobState.QUEUED)

    recovered = store.transition_job(
        job["id"],
        JobState.QUEUED,
        trigger=AttemptTrigger.RESTART_RECOVERY,
        expected_state=JobState.RUNNING,
    )
    assert recovered["state"] == "queued"


def test_manual_stage_retry_starts_a_new_attempt_cycle(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.ensure_stage(job["id"], "analysis", state=StageState.QUEUED)
    store.transition_stage(job["id"], "analysis", StageState.RUNNING)
    first = store.start_attempt(job["id"], "analysis", max_attempts=3)
    store.finish_attempt(first["id"], "failed")
    store.transition_stage(job["id"], "analysis", StageState.FAILED)

    retried_stage = store.transition_stage(
        job["id"],
        "analysis",
        StageState.QUEUED,
        trigger=AttemptTrigger.MANUAL_RETRY,
    )
    replayed = store.transition_stage(
        job["id"],
        "analysis",
        StageState.QUEUED,
        trigger=AttemptTrigger.MANUAL_RETRY,
    )
    store.transition_stage(job["id"], "analysis", StageState.RUNNING)
    second = store.start_attempt(
        job["id"],
        "analysis",
        trigger=AttemptTrigger.MANUAL_RETRY,
        max_attempts=3,
    )

    assert retried_stage["retry_cycle"] == 2
    assert second["retry_cycle"] == 2
    assert second["attempt_number"] == 1
    assert replayed["retry_cycle"] == 2


def test_duplicate_active_stage_attempt_is_coalesced(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.ensure_stage(job["id"], "analysis", state=StageState.QUEUED)
    store.transition_stage(job["id"], "analysis", StageState.RUNNING)

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(
            pool.map(lambda _: store.start_attempt(job["id"], "analysis"), range(2))
        )

    assert len({attempt["id"] for attempt in attempts}) == 1


def test_cross_run_candidate_cannot_be_attached_to_attempt_or_decision(store):
    first, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    second, _ = store.create_or_get_active_job("tt0068646", "", "The Godfather")
    candidate, _ = store.record_candidate(first["id"], "provider", "candidate-1")
    store.ensure_stage(second["id"], "subtitle_selection", state=StageState.QUEUED)
    store.transition_stage(second["id"], "subtitle_selection", StageState.RUNNING)

    with pytest.raises(ValueError, match="candidate"):
        store.start_attempt(
            second["id"], "subtitle_selection", candidate_id=candidate["id"]
        )
    with pytest.raises(ValueError, match="candidate"):
        store.record_decision(
            second["id"],
            "select_subtitle",
            candidate_id=candidate["id"],
            accepted=True,
        )


def test_events_are_monotonic_and_sanitized(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")

    first = store.record_event(
        job["id"],
        event_type="diagnostic",
        message="Bearer private-token failed at /home/operator/project/file.py",
        data={"cookie": "session=private-cookie", "error": RuntimeError("raw failure")},
    )
    second = store.record_event(job["id"], event_type="progress", message="One frame")

    events = store.list_events(job["id"], after=first["id"])
    assert [event["id"] for event in events] == [second["id"]]
    serialized = repr(store.list_events(job["id"]))
    assert "private-token" not in serialized
    assert "private-cookie" not in serialized
    assert "/home/operator" not in serialized

    broader = store.record_event(
        job["id"],
        event_type="diagnostic",
        message="failed at /usr/src/app/private.py and /mnt/runtime/secret.json",
    )
    assert "/usr/src/app" not in repr(broader)
    assert "/mnt/runtime" not in repr(broader)


def test_queue_summary_omits_internal_and_large_detail_fields(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.record_event(
        job["id"], event_type="analysis", data={"matches": list(range(50))}
    )

    page = store.list_jobs(query="pulp", limit=10, offset=0)

    assert page["total"] == 1
    assert page["items"][0]["id"] == job["id"]
    assert "events" not in page["items"][0]
    assert "lease_owner" not in page["items"][0]
    assert "legacy_payload" not in page["items"][0]


def test_admin_decisions_are_idempotent_by_key(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")

    first, first_created = store.record_decision(
        job["id"], "resume", idempotency_key="decision-1", accepted=True
    )
    second, second_created = store.record_decision(
        job["id"], "resume", idempotency_key="decision-1", accepted=True
    )

    assert first_created is True
    assert second_created is False
    assert second["id"] == first["id"]


def test_request_cancel_is_idempotent_for_queued_work(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.ensure_stage(job["id"], "analysis", state=StageState.QUEUED)

    first, first_changed = store.request_cancel(job["id"])
    second, second_changed = store.request_cancel(job["id"])

    assert first["state"] == "cancelled"
    assert first_changed is True
    assert second["state"] == "cancelled"
    assert second_changed is False
    assert store.get_job_detail(job["id"])["stages"][0]["state"] == "cancelled"


def test_concurrent_duplicate_publishing_attempt_is_coalesced(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(
            pool.map(
                lambda _: store.start_publishing_attempt(job["id"], "youtube"),
                range(2),
            )
        )

    assert len({attempt["id"] for attempt in attempts}) == 1
    assert len(store.get_job_detail(job["id"])["publishing_attempts"]) == 1


def test_publishing_rejects_empty_remote_id_and_reuses_metadata(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    first = store.start_publishing_attempt(
        job["id"], "youtube", metadata={"title": "Original"}
    )
    store.finish_publishing_attempt(first["id"], "failed", retryable=True)

    second = store.start_publishing_attempt(
        job["id"], "youtube", metadata={"title": "Changed"}
    )

    assert second["metadata"] == {"title": "Original"}
    with pytest.raises(ValueError, match="remote ID"):
        store.upsert_release(job["id"], "youtube", status="uploaded", remote_id="")


def test_repeating_terminal_stage_transition_preserves_timestamp_and_history(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.ensure_stage(job["id"], "analysis", state=StageState.QUEUED)
    store.transition_stage(job["id"], "analysis", StageState.RUNNING)
    completed = store.transition_stage(job["id"], "analysis", StageState.COMPLETED)
    event_count = len(store.list_events(job["id"]))

    replayed = store.transition_stage(job["id"], "analysis", StageState.COMPLETED)

    assert replayed["finished_at"] == completed["finished_at"]
    assert len(store.list_events(job["id"])) == event_count


def test_candidate_and_financial_dtos_hide_internal_artifact_paths(store, tmp_path):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    artifact_path = tmp_path / "private" / "candidate.srt"
    candidate, created = store.record_candidate(
        job["id"],
        "opensubtitles",
        "provider-42",
        provider_filename="../../untrusted.srt",
        artifact_path=str(artifact_path),
        rank=1,
        rank_reasons=["IMDb match"],
    )
    store.record_cost(job["id"], "subtitle", "opensubtitles", 0.25, detail={"calls": 1})
    store.upsert_release(job["id"], "youtube", status="uploaded", remote_id="remote-1")
    store.upsert_revenue(job["id"], "youtube", "2026-07-21", views=10, revenue_usd=1.5)

    detail = store.get_job_detail(job["id"])
    assert created is True
    assert candidate["artifact_available"] is True
    assert "artifact_path" not in candidate
    assert str(artifact_path) not in repr(detail)
    assert "artifact_path" not in store.get_candidate(candidate["id"])
    assert store.get_candidate(candidate["id"], include_internal=True)[
        "artifact_path"
    ] == str(artifact_path)
    assert detail["costs"][0]["detail"] == {"calls": 1}
    assert detail["releases"][0]["remote_id"] == "remote-1"
    assert detail["revenue"][0]["views"] == 10


def test_legacy_module_helpers_keep_current_pipeline_callers_functional(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "compatibility.db")
    database.init_db()

    first = database.upsert_job("tt0110912", "Pulp Fiction")
    database.update_job(
        "tt0110912",
        status="fetching",
        progress=10,
        message="Fetching",
    )
    database.record_step("tt0110912", "analysis", status="running")
    database.record_step("tt0110912", "analysis", status="done", message="Complete")
    database.record_cost("tt0110912", "subtitle", "opensubtitles", 0.5)
    database.update_job(
        "tt0110912",
        status="done",
        progress=100,
        analysis_json={"summary": {"total": 7}},
    )

    completed = database.get_job("tt0110912")
    assert completed["id"] == first["id"]
    assert completed["status"] == "done"
    assert completed["analysis_json"] == {"summary": {"total": 7}}
    assert database.get_steps("tt0110912")[0]["status"] == "done"
    assert database.get_costs("tt0110912")[0]["amount_usd"] == pytest.approx(0.5)

    second = database.upsert_job("tt0110912", "Pulp Fiction rerun")
    assert second["id"] != first["id"]
    assert database.get_job(first["id"])["status"] == "done"
