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


def test_exact_claim_is_atomic_and_does_not_disturb_older_queued_work(store):
    older, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    requested, _ = store.create_or_get_active_job("tt0068646", "", "The Godfather")

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(
                lambda owner: store.claim_job(
                    requested["id"], owner, lease_seconds=10
                ),
                ("cli-a", "cli-b"),
            )
        )

    claimed = [row for row in claims if row is not None]
    assert [row["id"] for row in claimed] == [requested["id"]]
    assert store.get_job(older["id"])["state"] == "queued"
    assert store.claim_next_job("worker", lease_seconds=10)["id"] == older["id"]


def test_exact_claim_does_not_resolve_imdb_alias(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    assert store.claim_job("tt0110912", "cli", lease_seconds=10) is None
    assert store.get_job(job["id"])["state"] == "queued"


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


def test_running_job_events_are_fenced_by_lease_owner(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.ensure_stage(job["id"], "analysis", state=StageState.QUEUED)
    store.claim_next_job("worker-a", lease_seconds=30)

    stale = store.record_event(
        job["id"],
        event_type="stale_diagnostic",
        stage_name="analysis",
        lease_owner="worker-b",
    )
    owned = store.record_event(
        job["id"],
        event_type="owned_diagnostic",
        stage_name="analysis",
        lease_owner="worker-a",
    )

    assert stale is None
    assert owned["type"] == "owned_diagnostic"
    assert not any(
        event["type"] == "stale_diagnostic" for event in store.list_events(job["id"])
    )


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


def test_legacy_publishing_attempt_helpers_are_removed(store):
    assert not hasattr(store, "start_publishing_attempt")
    assert not hasattr(store, "finish_publishing_attempt")


@pytest.mark.parametrize(
    "malformed_remote_id",
    [{"id": "mapping"}, object(), "remote\nid", "remote\x00id"],
)
def test_store_rejects_malformed_uploaded_remote_identity(
    store, malformed_remote_id
):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.request_publication(job["id"], "youtube", metadata={"title": "Stable"})
    attempt, claimed, _ = store.claim_publishing_attempt(
        job["id"], "youtube", retry_cycle=1, lease_owner="worker-a"
    )
    assert claimed is True

    with pytest.raises(ValueError, match="remote ID"):
        store.complete_publishing_attempt(
            attempt["id"],
            outcome="completed",
            release_status="uploaded",
            remote_id=malformed_remote_id,
            lease_owner="worker-a",
        )

    with pytest.raises(ValueError, match="remote ID"):
        store.upsert_release(
            job["id"],
            "youtube",
            status="uploaded",
            remote_id=malformed_remote_id,
        )


@pytest.mark.parametrize("field", ["views", "likes", "comments", "shares"])
def test_store_rejects_fractional_count_metrics(store, field):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.upsert_release(job["id"], "youtube", status="uploaded", remote_id="video-1")
    metrics = {
        "views": 1,
        "likes": 1,
        "comments": 1,
        "shares": 1,
        "revenue_usd": 1.5,
    }
    metrics[field] = 1.5

    with pytest.raises(ValueError, match="metrics"):
        store.store_publishing_stats(
            job["id"], "youtube", "2026-07-22", metrics
        )


def test_publication_request_rolls_back_release_when_event_insert_fails(
    store, monkeypatch
):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    before = store.get_job_detail(job["id"])

    def fail_event(connection, job_id, **fields):
        raise RuntimeError("injected event failure")

    monkeypatch.setattr(store, "_insert_event", fail_event)

    with pytest.raises(RuntimeError, match="injected event failure"):
        store.request_publication(
            job["id"],
            "youtube",
            metadata={"video_title": "Persist me once"},
        )

    assert store.get_job_detail(job["id"]) == before


def test_publication_claim_and_completion_are_atomic_with_release_and_event(
    store, monkeypatch
):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.request_publication(
        job["id"],
        "youtube",
        metadata={"video_title": "Original"},
    )
    attempt, claimed, release = store.claim_publishing_attempt(
        job["id"], "youtube", retry_cycle=1, lease_owner="atomic-worker"
    )
    assert claimed is True
    assert release["status"] == "uploading"
    before = store.get_job_detail(job["id"])

    def fail_event(connection, job_id, **fields):
        raise RuntimeError("injected completion event failure")

    monkeypatch.setattr(store, "_insert_event", fail_event)

    with pytest.raises(RuntimeError, match="injected completion event failure"):
        store.complete_publishing_attempt(
            attempt["id"],
            outcome="completed",
            release_status="uploaded",
            remote_id="remote-1",
            lease_owner="atomic-worker",
        )

    assert store.get_job_detail(job["id"]) == before


def test_expired_publishing_owner_cannot_complete_attempt(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.request_publication(job["id"], "youtube", metadata={"title": "Stable"})
    attempt, claimed, _ = store.claim_publishing_attempt(
        job["id"],
        "youtube",
        retry_cycle=1,
        lease_owner="worker-a",
        lease_seconds=1,
    )
    assert claimed is True
    store.clock = lambda: datetime(2026, 7, 22, 12, 0, 2, tzinfo=UTC)

    with pytest.raises(PermissionError, match="lease"):
        store.complete_publishing_attempt(
            attempt["id"],
            outcome="completed",
            release_status="uploaded",
            remote_id="remote-late",
            lease_owner="worker-a",
        )

    detail = store.get_job_detail(job["id"])
    assert detail["publishing_attempts"][0]["finished_at"] is None
    assert detail["releases"][0]["remote_id"] is None


def test_wrong_publishing_owner_cannot_complete_attempt(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.request_publication(job["id"], "youtube", metadata={"title": "Stable"})
    attempt, claimed, _ = store.claim_publishing_attempt(
        job["id"],
        "youtube",
        retry_cycle=1,
        lease_owner="worker-a",
        lease_seconds=60,
    )
    assert claimed is True

    with pytest.raises(PermissionError, match="lease"):
        store.complete_publishing_attempt(
            attempt["id"],
            outcome="completed",
            release_status="uploaded",
            remote_id="remote-wrong-owner",
            lease_owner="worker-b",
        )


def test_omitted_publishing_owner_cannot_bypass_completion_fence(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.request_publication(job["id"], "youtube", metadata={"title": "Stable"})
    attempt, claimed, _ = store.claim_publishing_attempt(
        job["id"],
        "youtube",
        retry_cycle=1,
        lease_owner="worker-a",
        lease_seconds=60,
    )
    assert claimed is True

    with pytest.raises(PermissionError, match="lease"):
        store.complete_publishing_attempt(
            attempt["id"],
            outcome="completed",
            release_status="uploaded",
            remote_id="remote-owner-omitted",
        )


def test_repeating_terminal_stage_transition_preserves_timestamp_and_history(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.ensure_stage(job["id"], "analysis", state=StageState.QUEUED)
    store.transition_stage(job["id"], "analysis", StageState.RUNNING)
    completed = store.transition_stage(job["id"], "analysis", StageState.COMPLETED)
    event_count = len(store.list_events(job["id"]))

    replayed = store.transition_stage(job["id"], "analysis", StageState.COMPLETED)

    assert replayed["finished_at"] == completed["finished_at"]
    assert len(store.list_events(job["id"])) == event_count


def test_stage_and_job_terminal_transition_is_atomic_and_lease_fenced(
    store, monkeypatch
):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.ensure_stage(job["id"], "analysis", state=StageState.QUEUED)
    store.claim_next_job("worker-a", lease_seconds=30)
    store.transition_stage(
        job["id"], "analysis", StageState.RUNNING, lease_owner="worker-a"
    )

    assert (
        store.transition_stage_and_job(
            job["id"],
            "analysis",
            StageState.FAILED,
            JobState.FAILED,
            lease_owner="stale-worker",
        )
        is None
    )
    original_insert_event = store._insert_event

    def fail_on_job_event(connection, job_id, **fields):
        if fields.get("event_type") == "job_state_changed":
            raise RuntimeError("injected event failure")
        return original_insert_event(connection, job_id, **fields)

    monkeypatch.setattr(store, "_insert_event", fail_on_job_event)
    with pytest.raises(RuntimeError, match="injected event failure"):
        store.transition_stage_and_job(
            job["id"],
            "analysis",
            StageState.FAILED,
            JobState.FAILED,
            lease_owner="worker-a",
        )

    detail = store.get_job_detail(job["id"])
    assert detail["run"]["state"] == "running"
    assert detail["stages"][0]["state"] == "running"


def test_composite_completion_atomically_finishes_running_children(store, monkeypatch):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.ensure_stage(job["id"], "composite", state=StageState.PENDING)
    for index, name in enumerate(("intro", "graph"), 1):
        store.ensure_stage(
            job["id"],
            f"composite.{name}",
            ordinal=100 + index,
            parent_name="composite",
            state=StageState.PENDING,
        )
    store.claim_next_job("worker-a", lease_seconds=30)
    store.transition_stage(
        job["id"], "composite", StageState.QUEUED, lease_owner="worker-a"
    )
    store.transition_stage(
        job["id"], "composite", StageState.RUNNING, lease_owner="worker-a"
    )
    for name in ("intro", "graph"):
        stage = f"composite.{name}"
        store.transition_stage(
            job["id"], stage, StageState.QUEUED, lease_owner="worker-a"
        )
        store.transition_stage(
            job["id"], stage, StageState.RUNNING, lease_owner="worker-a"
        )

    assert (
        store.complete_stage_and_children(
            job["id"],
            "composite",
            output_manifest={"stage": "composite"},
            lease_owner="worker-a",
        )
        is None
    )
    for name in ("intro", "graph"):
        store.transition_stage(
            job["id"],
            f"composite.{name}",
            StageState.RUNNING,
            expected_state=StageState.RUNNING,
            output_manifest={"stage": f"composite.{name}"},
            lease_owner="worker-a",
        )

    original_insert_event = store._insert_event

    def fail_on_graph_event(connection, job_id, **fields):
        if fields.get("message", "").startswith("Stage composite.graph moved"):
            raise RuntimeError("injected child event failure")
        return original_insert_event(connection, job_id, **fields)

    monkeypatch.setattr(store, "_insert_event", fail_on_graph_event)
    with pytest.raises(RuntimeError, match="injected child event failure"):
        store.complete_stage_and_children(
            job["id"],
            "composite",
            output_manifest={"stage": "composite"},
            lease_owner="worker-a",
        )

    assert {
        stage["name"]: stage["state"]
        for stage in store.get_job_detail(job["id"])["stages"]
    } == {
        "composite": "running",
        "composite.intro": "running",
        "composite.graph": "running",
    }

    monkeypatch.setattr(store, "_insert_event", original_insert_event)
    store.complete_stage_and_children(
        job["id"],
        "composite",
        output_manifest={"stage": "composite"},
        lease_owner="worker-a",
    )
    assert {
        stage["name"]: stage["state"]
        for stage in store.get_job_detail(job["id"])["stages"]
    } == {
        "composite": "completed",
        "composite.intro": "completed",
        "composite.graph": "completed",
    }


def test_composite_failure_atomically_resets_all_children(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.ensure_stage(job["id"], "composite", state=StageState.PENDING)
    for index, name in enumerate(("intro", "graph"), 1):
        store.ensure_stage(
            job["id"],
            f"composite.{name}",
            ordinal=100 + index,
            parent_name="composite",
            state=StageState.PENDING,
        )
    store.claim_next_job("worker-a", lease_seconds=30)
    store.transition_stage(
        job["id"], "composite", StageState.QUEUED, lease_owner="worker-a"
    )
    store.transition_stage(
        job["id"], "composite", StageState.RUNNING, lease_owner="worker-a"
    )
    store.transition_stage(
        job["id"], "composite.intro", StageState.QUEUED, lease_owner="worker-a"
    )
    store.transition_stage(
        job["id"], "composite.intro", StageState.RUNNING, lease_owner="worker-a"
    )

    store.transition_stage_and_job(
        job["id"],
        "composite",
        StageState.NEEDS_ATTENTION,
        "needs_attention",
        reset_descendants=True,
        lease_owner="worker-a",
    )

    detail = store.get_job_detail(job["id"])
    assert detail["run"]["state"] == "needs_attention"
    children = [stage for stage in detail["stages"] if stage["parent_stage_id"]]
    assert [stage["state"] for stage in children] == ["pending", "pending"]
    assert all(stage["output_manifest"] == {} for stage in children)


def _completed_generation_tree(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    for ordinal, name in enumerate(("metadata", "analysis", "composite", "audio"), 1):
        store.ensure_stage(job["id"], name, ordinal=ordinal, state=StageState.PENDING)
    for index, name in enumerate(
        ("intro_hold", "intro_transition", "graph", "verdict"), 1
    ):
        store.ensure_stage(
            job["id"],
            f"composite.{name}",
            ordinal=300 + index,
            parent_name="composite",
            state=StageState.PENDING,
        )
    store.claim_next_job("worker-a", lease_seconds=30)
    for name in ("metadata", "analysis", "composite", "audio"):
        store.transition_stage(
            job["id"], name, StageState.QUEUED, lease_owner="worker-a"
        )
        store.transition_stage(
            job["id"], name, StageState.RUNNING, lease_owner="worker-a"
        )
        store.transition_stage(
            job["id"],
            name,
            StageState.COMPLETED,
            progress_numerator=3,
            progress_denominator=3,
            progress_unit="items",
            warnings=["old warning"],
            output_manifest={"stage": name, "version": "old"},
            lease_owner="worker-a",
        )
    for name in ("intro_hold", "intro_transition", "graph", "verdict"):
        child = f"composite.{name}"
        store.transition_stage(
            job["id"], child, StageState.QUEUED, lease_owner="worker-a"
        )
        store.transition_stage(
            job["id"], child, StageState.RUNNING, lease_owner="worker-a"
        )
        store.transition_stage(
            job["id"],
            child,
            StageState.COMPLETED,
            progress_numerator=2,
            progress_denominator=2,
            progress_unit="frames",
            output_manifest={"stage": child, "version": "old"},
            lease_owner="worker-a",
        )
    return job


def test_artifact_invalidation_atomically_requeues_target_and_resets_downstream(store):
    job = _completed_generation_tree(store)

    outcome = store.invalidate_stage_and_downstream(
        job["id"],
        "analysis",
        lease_owner="worker-a",
        safe_error_code="invalid_completed_artifact",
        safe_error_message="Completed analysis output no longer validates.",
    )

    assert outcome is not None
    detail = store.get_job_detail(job["id"])
    stages = {stage["name"]: stage for stage in detail["stages"]}
    assert detail["run"]["state"] == "needs_attention"
    assert not store.renew_lease(job["id"], "worker-a", lease_seconds=30)
    assert stages["metadata"]["state"] == "completed"
    assert stages["metadata"]["output_manifest"]["version"] == "old"
    assert stages["analysis"]["state"] == "queued"
    assert stages["analysis"]["retry_cycle"] == 2
    for name in (
        "analysis",
        "composite",
        "audio",
        "composite.intro_hold",
        "composite.intro_transition",
        "composite.graph",
        "composite.verdict",
    ):
        stage = stages[name]
        if name != "analysis":
            assert stage["state"] == "pending"
        assert stage["output_manifest"] == {}
        assert stage["warnings"] == []
        assert stage["progress"] == {"numerator": 0, "denominator": 1, "unit": ""}
        assert stage["safe_error"] is None
    assert any(
        event["type"] == "artifact_validation_failed" for event in detail["events"]
    )


def test_artifact_invalidation_is_lease_fenced_and_rolls_back_event_failure(
    store, monkeypatch
):
    job = _completed_generation_tree(store)
    before = store.get_job_detail(job["id"])

    assert (
        store.invalidate_stage_and_downstream(
            job["id"],
            "analysis",
            lease_owner="stale-worker",
            safe_error_code="invalid",
            safe_error_message="invalid",
        )
        is None
    )
    assert store.get_job_detail(job["id"]) == before

    original_insert_event = store._insert_event

    def fail_invalidation_event(connection, job_id, **fields):
        if fields.get("event_type") == "artifact_validation_failed":
            raise RuntimeError("injected invalidation event failure")
        return original_insert_event(connection, job_id, **fields)

    monkeypatch.setattr(store, "_insert_event", fail_invalidation_event)
    with pytest.raises(RuntimeError, match="injected invalidation event failure"):
        store.invalidate_stage_and_downstream(
            job["id"],
            "analysis",
            lease_owner="worker-a",
            safe_error_code="invalid",
            safe_error_message="invalid",
        )

    assert store.get_job_detail(job["id"]) == before


def test_subtitle_owned_mutations_reject_a_stale_lease_owner(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.ensure_stage(job["id"], "subtitle_selection", ordinal=1)
    candidate, _ = store.record_candidate(
        job["id"], "provider", "candidate-1", status="discovered"
    )
    store.claim_next_job("current-owner", lease_seconds=30)
    before = store.get_job_detail(job["id"])

    assert (
        store.ensure_stage(
            job["id"],
            "subtitle_selection",
            ordinal=1,
            lease_owner="stale-owner",
        )
        is None
    )
    assert (
        store.record_candidate(
            job["id"],
            "provider",
            "candidate-2",
            status="discovered",
            lease_owner="stale-owner",
        )
        is None
    )
    assert (
        store.update_candidate(
            candidate["id"], status="rejected", lease_owner="stale-owner"
        )
        is None
    )
    assert (
        store.record_decision(
            job["id"],
            "select_subtitle",
            candidate_id=candidate["id"],
            accepted=True,
            lease_owner="stale-owner",
        )
        is None
    )
    assert store.get_job_detail(job["id"]) == before


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
