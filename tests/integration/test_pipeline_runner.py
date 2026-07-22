import asyncio

import pytest

from api.database import OperationStore
from api.errors import AttentionRequired, TransientFailure
from api.pipeline import PipelineRunner, StageResult
from api.retry import RetryPolicy
from api.settings import Settings

STAGES = ("input_resolution", "metadata", "analysis")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def store(tmp_path):
    result = OperationStore(tmp_path / "runner.db")
    result.initialize()
    return result


class FakeServices:
    def __init__(self):
        self.calls = []
        self.valid = {stage: True for stage in STAGES}
        self.handlers = {}

    async def run_stage(self, stage_name, job_id, progress):
        self.calls.append(stage_name)
        handler = self.handlers.get(stage_name)
        if handler:
            return await handler(progress)
        return StageResult(output_manifest={"stage": stage_name})

    async def validate_stage(self, stage_name, output_manifest):
        return self.valid[stage_name] and output_manifest.get("stage") == stage_name

    def retry_policy(self, stage_name):
        return RetryPolicy(max_attempts=3 if stage_name == "metadata" else 1, delays=(0, 0))


async def claimed_job(store, stages=STAGES):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    for ordinal, stage in enumerate(stages, 1):
        store.ensure_stage(job["id"], stage, ordinal=ordinal)
    store.claim_next_job("test-owner", lease_seconds=30)
    return job


@pytest.mark.anyio
async def test_runner_executes_stages_in_order_and_records_truthful_attempts(store):
    job = await claimed_job(store)
    services = FakeServices()

    await PipelineRunner(store, services, stages=STAGES, sleep=asyncio.sleep).run(
        job["id"], "test-owner"
    )

    detail = store.get_job_detail(job["id"])
    assert services.calls == list(STAGES)
    assert detail["run"]["state"] == "completed"
    assert [stage["state"] for stage in detail["stages"]] == ["completed"] * 3
    assert [attempt["outcome"] for attempt in detail["attempts"]] == ["completed"] * 3
    assert detail["attempts"][0]["output"] == {
        "output_manifest": {"stage": "input_resolution"},
        "warnings": [],
    }
    assert all(stage["started_at"] and stage["finished_at"] for stage in detail["stages"])


@pytest.mark.anyio
async def test_completed_stage_is_reused_only_after_validation(store):
    job = await claimed_job(store)
    services = FakeServices()
    store.transition_stage(job["id"], STAGES[0], "queued", lease_owner="test-owner")
    store.transition_stage(job["id"], STAGES[0], "running", lease_owner="test-owner")
    attempt = store.start_attempt(job["id"], STAGES[0], lease_owner="test-owner")
    store.finish_attempt(attempt["id"], "completed", output={"stage": STAGES[0]}, lease_owner="test-owner")
    store.transition_stage(
        job["id"], STAGES[0], "completed", output_manifest={"stage": STAGES[0]}, lease_owner="test-owner"
    )

    await PipelineRunner(store, services, stages=STAGES).run(job["id"], "test-owner")

    assert services.calls == ["metadata", "analysis"]
    assert any(event["type"] == "artifact_reused" for event in store.list_events(job["id"]))


@pytest.mark.anyio
async def test_cancellation_is_applied_between_stages(store):
    job = await claimed_job(store)
    services = FakeServices()

    async def cancel_after_first(progress):
        store.request_cancel(job["id"])
        return StageResult(output_manifest={"stage": STAGES[0]})

    services.handlers[STAGES[0]] = cancel_after_first
    await PipelineRunner(store, services, stages=STAGES).run(job["id"], "test-owner")

    detail = store.get_job_detail(job["id"])
    assert services.calls == [STAGES[0]]
    assert detail["run"]["state"] == "cancelled"
    assert detail["stages"][0]["state"] == "completed"
    assert [stage["state"] for stage in detail["stages"][1:]] == ["cancelled", "cancelled"]


@pytest.mark.anyio
async def test_restart_resumes_from_interrupted_stage_without_replaying_completed(store):
    job = await claimed_job(store)
    first_services = FakeServices()

    async def interrupted(progress):
        raise asyncio.CancelledError

    first_services.handlers["metadata"] = interrupted
    with pytest.raises(asyncio.CancelledError):
        await PipelineRunner(store, first_services, stages=STAGES).run(job["id"], "test-owner")

    # Simulate process death and durable lease recovery.
    with store._mutation() as connection:
        connection.execute(
            "UPDATE job_runs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (job["id"],),
        )
    store.recover_expired_leases()
    store.claim_next_job("restart-owner", lease_seconds=30)
    resumed = FakeServices()

    await PipelineRunner(store, resumed, stages=STAGES).run(job["id"], "restart-owner")

    assert resumed.calls == ["metadata", "analysis"]
    detail = store.get_job_detail(job["id"])
    metadata_attempts = [row for row in detail["attempts"] if row["stage_id"] == detail["stages"][1]["id"]]
    assert [row["outcome"] for row in metadata_attempts] == ["cancelled", "completed"]
    assert metadata_attempts[-1]["trigger"] == "restart_recovery"


@pytest.mark.anyio
async def test_progress_is_recorded_only_when_handler_reports_measurable_work(store):
    job = await claimed_job(store, ("analysis",))
    services = FakeServices()

    async def analysis(progress):
        await progress(2, 5, "items")
        return StageResult(output_manifest={"stage": "analysis"})

    services.handlers["analysis"] = analysis
    await PipelineRunner(store, services, stages=("analysis",)).run(job["id"], "test-owner")

    stage = store.get_job_detail(job["id"])["stages"][0]
    assert stage["progress"] == {"numerator": 2, "denominator": 5, "unit": "items"}


@pytest.mark.anyio
async def test_outcomes_map_to_attention_and_failed_without_leaking_secrets(store, monkeypatch):
    monkeypatch.setenv("PRIVATE_API_TOKEN", "secret-token")
    for error, expected in (
        (AttentionRequired("Fix the input", technical_detail="Bearer secret-token"), "needs_attention"),
        (TransientFailure("Try later", technical_detail="Bearer secret-token"), "failed"),
    ):
        job, _ = store.create_or_get_active_job("tt0110912", expected, expected)
        store.ensure_stage(job["id"], "metadata", ordinal=1)
        store.claim_next_job(f"owner-{expected}", lease_seconds=30)
        services = FakeServices()

        async def fail(progress, failure=error):
            raise failure

        services.handlers["metadata"] = fail
        await PipelineRunner(store, services, stages=("metadata",), sleep=asyncio.sleep).run(
            job["id"], f"owner-{expected}"
        )
        detail = store.get_job_detail(job["id"])
        assert detail["run"]["state"] == expected
        assert "secret-token" not in repr(detail)


@pytest.mark.anyio
async def test_raw_validation_exception_records_sanitized_attempt_stage_and_job(store, tmp_path):
    job = await claimed_job(store, ("analysis",))
    services = FakeServices()
    settings = Settings(base_dir=tmp_path, admin_api_token="settings-only-token")

    async def invalid(progress):
        raise ValueError(f"settings-only-token at {tmp_path}/private.srt")

    services.handlers["analysis"] = invalid
    await PipelineRunner(store, services, stages=("analysis",), settings=settings).run(
        job["id"], "test-owner"
    )

    detail = store.get_job_detail(job["id"])
    assert detail["run"]["state"] == "needs_attention"
    assert detail["stages"][0]["state"] == "needs_attention"
    assert detail["attempts"][0]["outcome"] == "failed"
    assert "settings-only-token" not in repr(detail)
    assert str(tmp_path) not in repr(detail)


@pytest.mark.anyio
async def test_completed_artifact_validation_exception_becomes_durable_attention(store):
    job = await claimed_job(store, ("analysis",))
    services = FakeServices()
    store.transition_stage(job["id"], "analysis", "queued", lease_owner="test-owner")
    store.transition_stage(job["id"], "analysis", "running", lease_owner="test-owner")
    attempt = store.start_attempt(job["id"], "analysis", lease_owner="test-owner")
    store.finish_attempt(attempt["id"], "completed", lease_owner="test-owner")
    store.transition_stage(
        job["id"],
        "analysis",
        "completed",
        output_manifest={"stage": "analysis"},
        lease_owner="test-owner",
    )

    async def explode(stage_name, manifest):
        raise ValueError("invalid cached artifact")

    services.validate_stage = explode
    await PipelineRunner(store, services, stages=("analysis",)).run(
        job["id"], "test-owner"
    )

    detail = store.get_job_detail(job["id"])
    assert detail["run"]["state"] == "needs_attention"
    assert any(event["type"] == "artifact_validation_failed" for event in detail["events"])


@pytest.mark.anyio
async def test_runner_terminal_failure_rolls_back_stage_and_job_together(store, monkeypatch):
    job = await claimed_job(store, ("analysis",))
    services = FakeServices()

    async def invalid(progress):
        raise ValueError("bad input")

    services.handlers["analysis"] = invalid
    original_insert_event = store._insert_event

    def fail_on_terminal_event(connection, job_id, **fields):
        if fields.get("event_type") == "job_state_changed":
            raise RuntimeError("injected event failure")
        return original_insert_event(connection, job_id, **fields)

    monkeypatch.setattr(store, "_insert_event", fail_on_terminal_event)

    with pytest.raises(RuntimeError, match="injected event failure"):
        await PipelineRunner(store, services, stages=("analysis",)).run(
            job["id"], "test-owner"
        )

    detail = store.get_job_detail(job["id"])
    assert detail["run"]["state"] == "running"
    assert detail["stages"][0]["state"] == "running"
