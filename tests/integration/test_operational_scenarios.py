"""End-to-end operational scenario verification.

Each test exercises one acceptance scenario from the operations control panel
design using in-memory fakes and temporary artifacts — no network, no real
encoder, no credentials. Three seams are driven directly:

* the durable stage runner (`PipelineRunner`) with a lightweight services double
  that carries the *real* retry policy (3 attempts for discovery/metadata, 1 for
  everything else) — for generation orchestration scenarios;
* the real `SubtitleService` — for discovery/rejection/selection scenarios;
* the real `PublishingService` — for publish/reconcile/stats scenarios.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from api.database import OperationStore
from api.errors import (
    AmbiguousPublishOutcome,
    AttentionRequired,
    TransientFailure,
)
from api.pipeline import PipelineRunner, StageResult
from api.publishing import PublishingService
from api.retry import RetryPolicy
from api.settings import Settings
from api.subtitles import SubtitleService
from src.data.opensubtitles import SubtitleCache, SubtitleResult
from src.publishing.errors import PlatformStatsError

# The full production pipeline (documentation reference). The compositor's
# child-stage mechanics are covered by test_generation_scenarios /
# test_pipeline_runner; these orchestration scenarios drive a representative
# linear stage list so a services double doesn't have to synthesize composite
# children.
GENERATION_STAGES = (
    "input_resolution",
    "subtitle_discovery",
    "metadata",
    "analysis",
    "graph",
    "composite",
    "audio",
    "encode",
)
ORCH_STAGES = tuple(stage for stage in GENERATION_STAGES if stage != "composite")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def store(tmp_path):
    result = OperationStore(tmp_path / "operations.db")
    result.initialize()
    return result


# ─────────────────────────── generation harness ────────────────────────────


def _transient(message: str = "temporary upstream outage") -> TransientFailure:
    return TransientFailure(
        "A temporary failure interrupted the stage.",
        code="stage_transient_failure",
        technical_detail=message,
    )


class ScenarioServices:
    """Runner services double with the production retry policy.

    Handlers are async callables ``handler(progress) -> StageResult`` keyed by
    stage name; absent stages complete immediately. ``valid`` toggles artifact
    validation per stage so reuse/resume paths can be exercised.
    """

    def __init__(self, stages=ORCH_STAGES):
        self.stages = tuple(stages)
        self.calls: list[str] = []
        self.handlers: dict = {}
        self.valid = {stage: True for stage in self.stages}

    async def run_stage(self, stage_name, job_id, progress):
        self.calls.append(stage_name)
        handler = self.handlers.get(stage_name)
        if handler is not None:
            return await handler(progress)
        return StageResult(output_manifest={"stage": stage_name})

    async def validate_stage(self, stage_name, expected_job_id, output_manifest):
        return self.valid.get(stage_name, True) and (
            output_manifest.get("stage") == stage_name
        )

    def retry_policy(self, stage_name):
        attempts = 3 if stage_name in {"subtitle_discovery", "metadata"} else 1
        return RetryPolicy(max_attempts=attempts, delays=(0, 0, 0))


def _claim_generation_job(store, stages=ORCH_STAGES, owner="worker"):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    for ordinal, stage in enumerate(stages, 1):
        store.ensure_stage(job["id"], stage, ordinal=ordinal)
    store.claim_next_job(owner, lease_seconds=30)
    return job


async def _run(store, services, job_id, owner="worker", stages=ORCH_STAGES):
    await PipelineRunner(
        store, services, stages=stages, sleep=asyncio.sleep
    ).run(job_id, owner)


def _expire_lease(store, job_id):
    with store._mutation() as connection:
        connection.execute(
            "UPDATE job_runs SET lease_expires_at = "
            "'2000-01-01T00:00:00+00:00' WHERE id = ?",
            (job_id,),
        )
    store.recover_expired_leases()


# ───────────────────────────── 1. success ──────────────────────────────────


@pytest.mark.anyio
async def test_generation_success_runs_every_stage_in_order_and_completes(store):
    job = _claim_generation_job(store)
    services = ScenarioServices()

    await _run(store, services, job["id"])

    detail = store.get_job_detail(job["id"])
    assert services.calls == list(ORCH_STAGES)
    assert detail["run"]["state"] == "completed"
    assert [stage["state"] for stage in detail["stages"]] == (
        ["completed"] * len(ORCH_STAGES)
    )
    assert [a["outcome"] for a in detail["attempts"]] == (
        ["completed"] * len(ORCH_STAGES)
    )


# ──────────────────── 2. transient discovery retry ─────────────────────────


@pytest.mark.anyio
async def test_transient_subtitle_discovery_is_retried_then_succeeds(store):
    job = _claim_generation_job(store)
    services = ScenarioServices()
    attempts = {"count": 0}

    async def flaky_discovery(progress):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise _transient("provider blip")
        return StageResult(output_manifest={"stage": "subtitle_discovery"})

    services.handlers["subtitle_discovery"] = flaky_discovery

    await _run(store, services, job["id"])

    detail = store.get_job_detail(job["id"])
    assert attempts["count"] == 3  # two transient failures, third succeeds
    assert detail["run"]["state"] == "completed"
    discovery = next(s for s in detail["stages"] if s["name"] == "subtitle_discovery")
    discovery_attempts = [
        a for a in detail["attempts"] if a["stage_id"] == discovery["id"]
    ]
    # Two transient failures are recorded, then a successful third attempt.
    assert [a["outcome"] for a in discovery_attempts] == [
        "failed",
        "failed",
        "completed",
    ]


# ──────────────────── 3. metadata retry exhaustion ─────────────────────────


@pytest.mark.anyio
async def test_metadata_retry_exhaustion_fails_after_exactly_three_attempts(store):
    job = _claim_generation_job(store)
    services = ScenarioServices()
    calls = {"count": 0}

    async def always_transient(progress):
        calls["count"] += 1
        raise _transient("metadata provider down")

    services.handlers["metadata"] = always_transient

    await _run(store, services, job["id"])

    detail = store.get_job_detail(job["id"])
    assert calls["count"] == 3  # exhausted, never a fourth call
    assert detail["run"]["state"] == "failed"
    metadata = next(s for s in detail["stages"] if s["name"] == "metadata")
    assert metadata["state"] == "failed"
    metadata_attempts = [a for a in detail["attempts"] if a["stage_id"] == metadata["id"]]
    assert [a["outcome"] for a in metadata_attempts] == ["failed", "failed", "failed"]
    # Downstream stages were never started.
    assert "analysis" not in services.calls


# ─────────────────── 4. three subtitle rejections ──────────────────────────


def _subtitle_service(store, settings, results, payloads):
    cache = SubtitleCache(settings.results_dir)
    client = _FakeSubtitleClient(results, payloads)
    return SubtitleService(store, client, cache, settings)


class _FakeSubtitleClient:
    def __init__(self, results, payloads):
        self.results = results
        self.payloads = payloads

    def search(self, **_kwargs):
        return self.results

    def download(self, file_id, destination):
        from pathlib import Path

        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.payloads[file_id])
        return path


TOO_SHORT_SRT = b"1\n00:00:01,000 --> 00:00:02,000\nToo short\n"
VALID_SRT = (
    b"1\n00:00:01,000 --> 00:00:02,000\nHello\n\n"
    b"2\n01:20:00,000 --> 01:25:00,000\nBye\n"
)


def test_three_subtitle_rejections_move_job_to_needs_attention(store, tmp_path):
    settings = Settings(base_dir=tmp_path, results_dir=tmp_path / "results")
    results = [
        SubtitleResult(str(i), f"{i}.srt", "Pulp Fiction", "1994", "en", None, "tt0110912")
        for i in range(4)
    ]
    payloads = {str(i): TOO_SHORT_SRT for i in range(4)}
    job, _ = store.create_or_get_active_job("tt0110912", "pulp fiction", "Pulp Fiction")
    service = _subtitle_service(store, settings, results, payloads)
    service.discover(job["id"])

    with pytest.raises(AttentionRequired) as raised:
        service.select(job["id"])

    assert "upload_subtitle" in raised.value.actions
    detail = store.get_job_detail(job["id"])
    assert detail["run"]["state"] == "needs_attention"
    rejected = [c for c in detail["candidates"] if c["rejection_reasons"]]
    assert len(rejected) == 3  # exactly three candidates tried and rejected


# ─────────────────── 5. manual selection / resume ──────────────────────────


def test_manual_selection_overrides_threshold_and_records_one_decision(store, tmp_path):
    settings = Settings(base_dir=tmp_path, results_dir=tmp_path / "results")
    result = SubtitleResult(
        "1", "one.srt", "Pulp Fiction", "1994", "en", None, "tt0110912",
        runtime_seconds=100 * 60,
    )
    job, _ = store.create_or_get_active_job("tt0110912", "pulp fiction", "Pulp Fiction")
    service = _subtitle_service(store, settings, [result], {"1": TOO_SHORT_SRT})
    candidate = service.discover(job["id"])[0]

    # A below-threshold candidate is rejected automatically...
    with pytest.raises(AttentionRequired):
        service.select(job["id"])

    # ...but an operator can manually select it, overriding the threshold,
    # and repeating the selection (a duplicate resume click) is idempotent.
    first = service.select(job["id"], manual_candidate_id=candidate["id"])
    second = service.select(job["id"], manual_candidate_id=candidate["id"])

    assert first["selection_method"] == "manual"
    assert "manual_threshold_override" in first["quality_reasons"]
    assert second["id"] == first["id"]
    assert len(store.list_decisions(job["id"])) == 1


# ─────────────────── 6. duplicate resume is a safe no-op ────────────────────


@pytest.mark.anyio
async def test_duplicate_resume_after_completion_replays_nothing(store):
    job = _claim_generation_job(store)
    await _run(store, ScenarioServices(), job["id"])
    assert store.get_job_detail(job["id"])["run"]["state"] == "completed"
    attempts_before = len(store.get_job_detail(job["id"])["attempts"])

    # Re-claim and re-run: every stage is already completed and validated, so
    # a duplicate resume must not replay work or add attempts.
    store.claim_next_job("worker", lease_seconds=30)
    replay = ScenarioServices()
    await _run(store, replay, job["id"])

    detail = store.get_job_detail(job["id"])
    assert replay.calls == []
    assert detail["run"]["state"] == "completed"
    assert len(detail["attempts"]) == attempts_before


# ─────────────────── 7. restart recovery from checkpoint ────────────────────


@pytest.mark.anyio
async def test_restart_recovery_resumes_without_replaying_completed_stages(store):
    job = _claim_generation_job(store)
    interrupted = ScenarioServices()

    async def die_mid_metadata(progress):
        raise asyncio.CancelledError

    interrupted.handlers["metadata"] = die_mid_metadata
    with pytest.raises(asyncio.CancelledError):
        await _run(store, interrupted, job["id"])

    # Simulate process death + durable lease recovery, then resume.
    _expire_lease(store, job["id"])
    store.claim_next_job("restart-owner", lease_seconds=30)
    resumed = ScenarioServices()
    await _run(store, resumed, job["id"], owner="restart-owner")

    detail = store.get_job_detail(job["id"])
    assert detail["run"]["state"] == "completed"
    # input_resolution was completed pre-crash and is not replayed.
    assert "input_resolution" not in resumed.calls
    assert resumed.calls[0] == "metadata"
    metadata = next(s for s in detail["stages"] if s["name"] == "metadata")
    metadata_attempts = [a for a in detail["attempts"] if a["stage_id"] == metadata["id"]]
    assert metadata_attempts[-1]["trigger"] == "restart_recovery"
    assert metadata_attempts[-1]["outcome"] == "completed"


# ─────────────────── 8. deterministic render failure ───────────────────────


@pytest.mark.anyio
async def test_deterministic_render_failure_is_not_blindly_retried(store):
    job = _claim_generation_job(store)
    services = ScenarioServices()
    calls = {"count": 0}

    async def broken_encode(progress):
        calls["count"] += 1
        raise AttentionRequired(
            "The render failed deterministically and needs operator attention.",
            code="deterministic_render_failure",
        )

    services.handlers["encode"] = broken_encode

    await _run(store, services, job["id"])

    detail = store.get_job_detail(job["id"])
    assert calls["count"] == 1  # render stages get one attempt — no blind retry
    assert detail["run"]["state"] == "needs_attention"
    encode = next(s for s in detail["stages"] if s["name"] == "encode")
    assert encode["state"] == "needs_attention"


# ─────────────────────────── 9. cancellation ───────────────────────────────


@pytest.mark.anyio
async def test_cancellation_stops_the_pipeline_and_cancels_remaining_stages(store):
    job = _claim_generation_job(store)
    services = ScenarioServices()

    async def cancel_during_metadata(progress):
        store.request_cancel(job["id"])
        return StageResult(output_manifest={"stage": "metadata"})

    services.handlers["metadata"] = cancel_during_metadata

    await _run(store, services, job["id"])

    detail = store.get_job_detail(job["id"])
    assert detail["run"]["state"] == "cancelled"
    # Metadata (stage 3) finished; nothing past it ran.
    assert services.calls == ["input_resolution", "subtitle_discovery", "metadata"]
    downstream = [s for s in detail["stages"] if s["name"] in ("analysis", "encode")]
    assert all(s["state"] == "cancelled" for s in downstream)


# ─────────────────── publishing scenarios (real service) ────────────────────


class _FakePlatformClient:
    def __init__(self, *, uploads=(), stats=()):
        self.upload_results = list(uploads)
        self.stats_results = list(stats)
        self.upload_calls: list[dict] = []
        self.stats_calls: list[str] = []

    def upload(self, video_path, **metadata):
        self.upload_calls.append({"video_path": str(video_path), **metadata})
        result = self.upload_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def get_video_stats(self, remote_id):
        self.stats_calls.append(remote_id)
        result = self.stats_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def published_store(tmp_path):
    result = OperationStore(
        tmp_path / "publishing.db",
        clock=lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
    )
    result.initialize()
    return result


@pytest.fixture
def publish_job(published_store):
    return published_store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")[0]


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "final.mp4"
    path.write_bytes(b"validated-video")
    return path


def _request_publish(service, job_id):
    return service.request(
        job_id, "youtube", title="Pulp Fiction", summary={"total_hard": 5}
    )


# ─────────────────── 10. publish transient exhaustion ──────────────────────


def test_publish_transient_exhaustion_stops_after_three_uploads(
    published_store, publish_job, video
):
    client = _FakePlatformClient(
        uploads=(_transient("one"), _transient("two"), _transient("three"))
    )
    service = PublishingService(
        published_store, {"youtube": client}, sleep=lambda _: None
    )
    _request_publish(service, publish_job["id"])

    with pytest.raises(TransientFailure):
        service.publish(publish_job["id"], "youtube", video)

    assert len(client.upload_calls) == 3  # exhausted; never a fourth call
    detail = published_store.get_job_detail(publish_job["id"])
    assert detail["releases"][0]["status"] == "failed"


# ─────────────────── 11. ambiguous publish → reconcile ─────────────────────


def test_ambiguous_publish_requires_reconciliation_not_blind_retry(
    published_store, publish_job, video
):
    # An empty remote id means the platform may or may not have accepted the
    # upload — ambiguous, so it must not be blindly retried.
    client = _FakePlatformClient(uploads=("   ",))
    service = PublishingService(
        published_store, {"youtube": client}, sleep=lambda _: None
    )
    _request_publish(service, publish_job["id"])

    with pytest.raises(AmbiguousPublishOutcome):
        service.publish(publish_job["id"], "youtube", video)

    detail = published_store.get_job_detail(publish_job["id"])
    assert detail["publishing_attempts"][0]["outcome"] == "ambiguous"
    assert detail["publishing_attempts"][0]["remote_id"] is None
    assert detail["releases"][0]["status"] == "needs_attention"

    # An operator confirms the upload existed; reconciliation resolves it
    # without a second upload.
    reconciled = service.retry(
        publish_job["id"],
        "youtube",
        video,
        reconciliation="uploaded",
        reconciled_remote_id="remote-found",
    )
    assert reconciled["status"] == "uploaded"
    assert reconciled["remote_id"] == "remote-found"
    assert len(client.upload_calls) == 1  # no extra upload during reconciliation


# ─────────────────── 12. stats failure retention ───────────────────────────


VALID_STATS = {
    "views": 120,
    "likes": 10,
    "comments": 3,
    "shares": 2,
    "revenue_usd": 1.25,
}


def test_stats_failure_preserves_the_last_good_snapshot(
    published_store, publish_job, video
):
    client = _FakePlatformClient(
        uploads=("remote-1",),
        stats=(VALID_STATS, PlatformStatsError("stats temporarily unavailable")),
    )
    service = PublishingService(
        published_store,
        {"youtube": client},
        date_factory=lambda: "2026-07-22",
        sleep=lambda _: None,
    )
    _request_publish(service, publish_job["id"])
    service.publish(publish_job["id"], "youtube", video)

    good = service.refresh_stats(publish_job["id"], "youtube")
    preserved = published_store.list_revenue(publish_job["id"])

    with pytest.raises((PlatformStatsError, ValueError)):
        service.refresh_stats(publish_job["id"], "youtube")

    assert good["views"] == VALID_STATS["views"]
    # The failed refresh does not overwrite the last good snapshot.
    assert published_store.list_revenue(publish_job["id"]) == preserved
    assert any(
        event["type"] == "publishing_stats_failed"
        for event in published_store.list_events(publish_job["id"])
    )
