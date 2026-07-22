from __future__ import annotations

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from api.database import OperationStore
from api.errors import AmbiguousPublishOutcome, ConfigurationRequired, TransientFailure
from api.publishing import PublishingService
from src.publishing.errors import PlatformConfirmationError, PlatformStatsError


class FakeClient:
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


class BlockingClient(FakeClient):
    def __init__(self):
        super().__init__(uploads=("remote-1",))
        self.entered = threading.Event()
        self.release = threading.Event()

    def upload(self, video_path, **metadata):
        self.entered.set()
        if not self.release.wait(timeout=2):
            raise AssertionError("test did not release upload")
        return super().upload(video_path, **metadata)


@pytest.fixture
def store(tmp_path):
    result = OperationStore(
        tmp_path / "operations.db",
        clock=lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
    )
    result.initialize()
    return result


@pytest.fixture
def job(store):
    return store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")[0]


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "final.mp4"
    path.write_bytes(b"validated-video")
    return path


def metadata_factory_counter():
    calls: list[tuple[str, dict]] = []

    def generate(title, summary):
        calls.append((title, summary))
        return {
            "video_title": f"Generated {len(calls)} for {title}",
            "description": "safe description",
            "tags": ["safe-tag"],
            "hashtags": ["#Safe"],
        }

    return generate, calls


def transient(message="temporary upstream outage"):
    return TransientFailure(
        "Publishing was interrupted by a temporary platform failure.",
        code="publishing_transient_failure",
        technical_detail=message,
    )


def request(service, job_id):
    return service.request(
        job_id,
        "youtube",
        title="Pulp Fiction",
        summary={"total_hard": 5, "total_f_bombs": 12},
    )


def test_three_transient_failures_are_durable_and_never_make_a_fourth_call(
    store, job, video
):
    client = FakeClient(uploads=(transient("one"), transient("two"), transient("three")))
    service = PublishingService(store, {"youtube": client}, sleep=lambda _: None)
    request(service, job["id"])

    with pytest.raises(TransientFailure, match="temporary platform failure"):
        service.publish(job["id"], "youtube", video)

    detail = store.get_job_detail(job["id"])
    attempts = detail["publishing_attempts"]
    assert len(client.upload_calls) == 3
    assert [(row["retry_cycle"], row["attempt_number"]) for row in attempts] == [
        (1, 1),
        (1, 2),
        (1, 3),
    ]
    assert all(row["outcome"] == "failed" for row in attempts)
    assert detail["releases"][0]["status"] == "failed"
    assert detail["releases"][0]["safe_error"] == {
        "code": "publishing_transient_failure",
        "message": "Publishing was interrupted by a temporary platform failure.",
    }


def test_missing_credentials_are_one_sanitized_deterministic_attempt(
    store, job, video, monkeypatch
):
    secret = "super-secret-cookie"
    monkeypatch.setenv("TIKTOK_SESSION_ID", secret)
    unsafe = (
        f"Bearer raw-bearer-token Cookie: sessionid={secret} "
        "https://upstream.test/x?access_token=query-secret "
        "/home/mapha/slur-meter/private/raw-body"
    )
    client = FakeClient(
        uploads=(
            ConfigurationRequired(
                "Publishing credentials are missing.",
                code="publishing_credentials_required",
                technical_detail=unsafe,
            ),
        )
    )
    service = PublishingService(store, {"youtube": client}, sleep=lambda _: None)
    request(service, job["id"])

    with pytest.raises(ConfigurationRequired):
        service.publish(job["id"], "youtube", video)

    detail = store.get_job_detail(job["id"])
    assert len(client.upload_calls) == 1
    assert len(detail["publishing_attempts"]) == 1
    serialized = repr(detail)
    for forbidden in (
        "raw-bearer-token",
        secret,
        "query-secret",
        "/home/mapha",
        "raw-body",
    ):
        assert forbidden not in serialized


def test_concurrent_duplicate_publish_calls_claim_one_upload(store, job, video):
    client = BlockingClient()
    service = PublishingService(store, {"youtube": client}, sleep=lambda _: None)
    request(service, job["id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.publish, job["id"], "youtube", video)
        assert client.entered.wait(timeout=2)
        duplicate = pool.submit(service.publish, job["id"], "youtube", video)
        duplicate_result = duplicate.result(timeout=2)
        client.release.set()
        first_result = first.result(timeout=2)

    assert len(client.upload_calls) == 1
    assert duplicate_result["status"] == "uploading"
    assert first_result["status"] == "uploaded"
    assert len(store.get_job_detail(job["id"])["publishing_attempts"]) == 1


def test_already_uploaded_request_publish_and_retry_are_idempotent(
    store, job, video
):
    client = FakeClient(uploads=())
    service = PublishingService(store, {"youtube": client}, sleep=lambda _: None)
    requested = request(service, job["id"])
    uploaded = store.upsert_release(
        job["id"],
        "youtube",
        status="uploaded",
        remote_id="remote-existing",
        metadata=requested["metadata"],
    )

    replayed_request = request(service, job["id"])
    replayed_publish = service.publish(job["id"], "youtube", video)
    replayed_retry = service.retry(job["id"], "youtube", video)

    assert replayed_request["id"] == uploaded["id"]
    assert replayed_publish["remote_id"] == "remote-existing"
    assert replayed_retry["remote_id"] == "remote-existing"
    assert client.upload_calls == []


def test_already_uploaded_publish_is_idempotent_when_local_video_is_gone(
    store, job, tmp_path
):
    client = FakeClient(uploads=())
    service = PublishingService(store, {"youtube": client}, sleep=lambda _: None)
    requested = request(service, job["id"])
    store.upsert_release(
        job["id"],
        "youtube",
        status="uploaded",
        remote_id="remote-existing",
        metadata=requested["metadata"],
    )

    replayed = service.publish(job["id"], "youtube", tmp_path / "removed.mp4")

    assert replayed["remote_id"] == "remote-existing"
    assert client.upload_calls == []


def test_empty_remote_id_is_ambiguous_and_requires_reconciliation(store, job, video):
    client = FakeClient(uploads=("   ",))
    service = PublishingService(store, {"youtube": client}, sleep=lambda _: None)
    request(service, job["id"])

    with pytest.raises(AmbiguousPublishOutcome):
        service.publish(job["id"], "youtube", video)

    detail = store.get_job_detail(job["id"])
    assert detail["publishing_attempts"][0]["remote_id"] is None
    assert detail["publishing_attempts"][0]["outcome"] == "ambiguous"
    assert detail["releases"][0]["status"] == "needs_attention"


@pytest.mark.parametrize(
    "malformed_remote_id",
    [
        {"id": "mapping-is-not-an-id"},
        object(),
        "remote\nid",
        "remote\x00id",
    ],
)
def test_malformed_remote_identity_is_ambiguous_not_stringified(
    store, job, video, malformed_remote_id
):
    client = FakeClient(uploads=(malformed_remote_id,))
    service = PublishingService(store, {"youtube": client}, sleep=lambda _: None)
    request(service, job["id"])

    with pytest.raises(AmbiguousPublishOutcome):
        service.publish(job["id"], "youtube", video)

    detail = store.get_job_detail(job["id"])
    assert detail["publishing_attempts"][0]["remote_id"] is None
    assert detail["releases"][0]["remote_id"] is None
    assert detail["releases"][0]["status"] == "needs_attention"


@pytest.mark.parametrize("reconciliation", ["uploaded", "not_uploaded"])
def test_empty_remote_id_can_be_explicitly_reconciled(
    store, job, video, reconciliation
):
    uploads = ("",) if reconciliation == "uploaded" else ("", "remote-retry")
    client = FakeClient(uploads=uploads)
    service = PublishingService(store, {"youtube": client}, sleep=lambda _: None)
    request(service, job["id"])
    with pytest.raises(AmbiguousPublishOutcome):
        service.publish(job["id"], "youtube", video)

    result = service.retry(
        job["id"],
        "youtube",
        video,
        reconciliation=reconciliation,
        reconciled_remote_id=("remote-found" if reconciliation == "uploaded" else None),
    )

    assert result["status"] == "uploaded"
    assert result["remote_id"] == (
        "remote-found" if reconciliation == "uploaded" else "remote-retry"
    )
    assert len(client.upload_calls) == (1 if reconciliation == "uploaded" else 2)


def test_live_upload_cannot_be_abandoned_by_concurrent_reconciliation(
    store, job, video
):
    client = BlockingClient()
    first = PublishingService(store, {"youtube": client}, sleep=lambda _: None)
    second = PublishingService(store, {"youtube": client}, sleep=lambda _: None)
    request(first, job["id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        upload = pool.submit(first.publish, job["id"], "youtube", video)
        assert client.entered.wait(timeout=2)
        blocked = second.retry(
            job["id"], "youtube", video, reconciliation="not_uploaded"
        )
        client.release.set()
        completed = upload.result(timeout=2)

    assert blocked["status"] == "uploading"
    assert completed["remote_id"] == "remote-1"
    assert len(client.upload_calls) == 1


def test_heartbeat_failure_and_expiry_cannot_supersede_live_upload(
    store, job, video, monkeypatch
):
    blocking = BlockingClient()
    replacement = FakeClient(uploads=("duplicate",))
    renewal_failed = threading.Event()

    def fail_renewal(*args, **kwargs):
        renewal_failed.set()
        raise RuntimeError("database temporarily unavailable")

    monkeypatch.setattr(store, "renew_publishing_attempt_lease", fail_renewal)
    first = PublishingService(
        store,
        {"youtube": blocking},
        sleep=lambda _: None,
        lease_seconds=0.05,
        heartbeat_interval=0.01,
    )
    second = PublishingService(store, {"youtube": replacement}, sleep=lambda _: None)
    request(first, job["id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        upload = pool.submit(first.publish, job["id"], "youtube", video)
        assert blocking.entered.wait(timeout=2)
        assert renewal_failed.wait(timeout=2)
        store.clock = lambda: datetime(2026, 7, 22, 12, 1, tzinfo=UTC)
        blocked = second.retry(
            job["id"], "youtube", video, reconciliation="not_uploaded"
        )
        blocking.release.set()
        with pytest.raises(AmbiguousPublishOutcome):
            upload.result(timeout=2)

    assert blocked["status"] == "uploading"
    assert replacement.upload_calls == []


def test_ambiguous_submit_requires_reconciliation_before_manual_retry(
    store, job, video
):
    client = FakeClient(
        uploads=(AmbiguousPublishOutcome("The platform may have accepted the upload."), "remote-2")
    )
    service = PublishingService(store, {"youtube": client}, sleep=lambda _: None)
    request(service, job["id"])

    with pytest.raises(AmbiguousPublishOutcome):
        service.publish(job["id"], "youtube", video)

    blocked = service.retry(job["id"], "youtube", video)
    assert blocked["status"] == "needs_attention"
    assert len(client.upload_calls) == 1

    uploaded = service.retry(
        job["id"],
        "youtube",
        video,
        reconciliation="not_uploaded",
    )

    detail = store.get_job_detail(job["id"])
    assert uploaded["status"] == "uploaded"
    assert len(client.upload_calls) == 2
    assert [row["retry_cycle"] for row in detail["publishing_attempts"]] == [1, 2]
    decision = detail["decisions"][0]
    assert decision["action"] == "reconcile_publishing"
    assert decision["accepted"] is True


def test_restarted_service_can_reconcile_abandoned_upload_before_retry(
    store, job, video
):
    first_service = PublishingService(store, {"youtube": FakeClient()})
    request(first_service, job["id"])
    attempt, claimed, release = store.claim_publishing_attempt(
        job["id"],
        "youtube",
        retry_cycle=1,
        lease_owner="crashed-worker",
        lease_seconds=1,
    )
    assert claimed is True
    assert attempt is not None
    assert release["status"] == "uploading"
    store.clock = lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC) + timedelta(
        seconds=2
    )

    client = FakeClient(uploads=("remote-after-reconciliation",))
    restarted = PublishingService(store, {"youtube": client}, sleep=lambda _: None)
    blocked = restarted.retry(job["id"], "youtube", video)
    assert blocked["status"] == "uploading"
    assert client.upload_calls == []

    uploaded = restarted.retry(
        job["id"], "youtube", video, reconciliation="not_uploaded"
    )

    assert uploaded["status"] == "uploaded"
    assert uploaded["remote_id"] == "remote-after-reconciliation"
    detail = store.get_job_detail(job["id"])
    assert detail["publishing_attempts"][0]["outcome"] == "ambiguous"
    assert len(client.upload_calls) == 1


def test_reconciliation_can_confirm_existing_remote_without_upload(store, job, video):
    client = FakeClient(uploads=(AmbiguousPublishOutcome(),))
    service = PublishingService(store, {"youtube": client}, sleep=lambda _: None)
    request(service, job["id"])
    with pytest.raises(AmbiguousPublishOutcome):
        service.publish(job["id"], "youtube", video)

    result = service.retry(
        job["id"],
        "youtube",
        video,
        reconciliation="uploaded",
        reconciled_remote_id="remote-confirmed",
    )

    assert result["status"] == "uploaded"
    assert result["remote_id"] == "remote-confirmed"
    assert len(client.upload_calls) == 1


def test_metadata_is_generated_once_reused_exactly_and_youtube_defaults_private(
    store, job, video
):
    generator, generation_calls = metadata_factory_counter()
    client = FakeClient(uploads=(transient(), "remote-1"))
    service = PublishingService(
        store,
        {"youtube": client},
        metadata_factory=generator,
        sleep=lambda _: None,
    )

    first = request(service, job["id"])
    second = request(service, job["id"])
    uploaded = service.publish(job["id"], "youtube", video)

    assert first["metadata"] == second["metadata"] == uploaded["metadata"]
    assert len(generation_calls) == 1
    assert client.upload_calls[0] == client.upload_calls[1]
    assert client.upload_calls[0]["privacy_status"] == "private"


def test_explicit_safe_youtube_privacy_is_persisted(store, job):
    service = PublishingService(store, {"youtube": FakeClient()})

    release = service.request(
        job["id"],
        "youtube",
        title="Pulp Fiction",
        summary={},
        privacy_status="unlisted",
    )

    assert release["metadata"]["privacy_status"] == "unlisted"


def test_concurrent_requests_across_service_instances_generate_metadata_once(
    store, job
):
    calls = 0
    calls_lock = threading.Lock()

    def generate(title, summary):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return {"video_title": "Only title", "description": "Stable"}

    services = [
        PublishingService(store, {"youtube": FakeClient()}, metadata_factory=generate)
        for _ in range(2)
    ]
    start = threading.Barrier(2)

    def run(service):
        start.wait(timeout=2)
        return request(service, job["id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        releases = list(pool.map(run, services))

    assert calls == 1
    assert releases[0]["id"] == releases[1]["id"]
    assert releases[0]["metadata"] == releases[1]["metadata"]


VALID_STATS = {
    "views": 120,
    "likes": 10,
    "comments": 3,
    "shares": 2,
    "revenue_usd": 1.25,
}


@pytest.mark.parametrize(
    "bad_stats",
    [
        PlatformStatsError("Platform statistics could not be refreshed."),
        {"views": 10},
        {**VALID_STATS, "likes": -1},
        {**VALID_STATS, "views": "not-a-number"},
        {**VALID_STATS, "revenue_usd": math.nan},
        {**VALID_STATS, "views": math.inf},
        {**VALID_STATS, "views": 1.5},
        {**VALID_STATS, "shares": 2.5},
    ],
)
def test_stats_failures_preserve_exact_last_good_snapshot(
    store, job, video, bad_stats
):
    client = FakeClient(uploads=("remote-1",), stats=(VALID_STATS, bad_stats))
    service = PublishingService(
        store,
        {"youtube": client},
        date_factory=lambda: "2026-07-22",
        sleep=lambda _: None,
    )
    request(service, job["id"])
    service.publish(job["id"], "youtube", video)
    first = service.refresh_stats(job["id"], "youtube")
    before = store.list_revenue(job["id"])

    with pytest.raises((PlatformStatsError, ValueError)):
        service.refresh_stats(job["id"], "youtube")

    assert first["views"] == VALID_STATS["views"]
    assert store.list_revenue(job["id"]) == before
    assert any(
        event["type"] == "publishing_stats_failed"
        for event in store.list_events(job["id"])
    )


def test_stats_require_nonempty_remote_identity_and_resolve_imdb_alias(store, job):
    client = FakeClient(stats=(VALID_STATS,))
    service = PublishingService(store, {"youtube": client}, date_factory=lambda: "2026-07-22")
    service.request(job["id"], "youtube", title="Pulp Fiction", summary={})

    with pytest.raises(PlatformConfirmationError):
        service.refresh_stats("tt0110912", "youtube")

    assert client.stats_calls == []
    assert store.list_revenue(job["id"]) == []


@pytest.mark.parametrize("remote_id", [{"id": "mapping"}, object(), "bad\nidentity"])
def test_stats_reject_malformed_remote_identity_without_client_call(
    store, job, monkeypatch, remote_id
):
    client = FakeClient(stats=(VALID_STATS,))
    service = PublishingService(store, {"youtube": client})
    monkeypatch.setattr(
        service,
        "_require_release",
        lambda *_: {"status": "uploaded", "remote_id": remote_id},
    )

    with pytest.raises(PlatformConfirmationError):
        service.refresh_stats(job["id"], "youtube")

    assert client.stats_calls == []


def test_publish_rejects_missing_or_empty_video_before_client_call(store, job, tmp_path):
    client = FakeClient(uploads=())
    service = PublishingService(store, {"youtube": client})
    request(service, job["id"])

    with pytest.raises(ValueError, match="validated video"):
        service.publish(job["id"], "youtube", tmp_path / "missing.mp4")

    empty = tmp_path / "empty.mp4"
    empty.touch()
    with pytest.raises(ValueError, match="validated video"):
        service.publish(job["id"], "youtube", empty)
    assert client.upload_calls == []
