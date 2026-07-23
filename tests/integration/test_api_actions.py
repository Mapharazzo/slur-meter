from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from api.database import OperationStore
from api.settings import Settings
from api.subtitles import SubtitleService
from src.data.opensubtitles import OpenSubtitlesClient, SubtitleCache


class Dispatcher:
    def __init__(self, store=None):
        self.wakes = 0
        self.store = store
        self.committed_states = []

    async def start(self):
        pass

    async def stop(self):
        pass

    def wake(self):
        self.wakes += 1
        if self.store is not None:
            self.committed_states.append(self.store.list_jobs(limit=500))


@pytest.mark.anyio
async def test_cancel_is_idempotent_and_wakes_only_for_a_change(tmp_path):
    from api.main import create_app

    store, dispatcher = OperationStore(tmp_path / "db.sqlite"), Dispatcher()
    app = create_app(Settings(tmp_path, admin_api_token="token"), store, dispatcher)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer token", "Idempotency-Key": "one"},
        ) as client:
            job = (await client.post("/api/jobs", json={"query": "film"})).json()
            first = await client.post(f"/api/jobs/{job['id']}/actions/cancel")
            second = await client.post(f"/api/jobs/{job['id']}/actions/cancel")
    assert first.status_code == second.status_code == 200
    assert dispatcher.wakes == 2  # submit + first cancellation


@pytest.mark.anyio
async def test_idempotency_key_cannot_be_reused_across_actions_targets_or_platforms(
    tmp_path,
):
    app, store, dispatcher = await _client(tmp_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        job = (await client.post("/api/jobs", json={"query": "scope keys"})).json()
        store.ensure_stage(job["id"], "encode", state="failed")
        store.ensure_stage(job["id"], "graph", state="failed")
        first = await client.post(
            f"/api/jobs/{job['id']}/stages/encode/retry",
            headers={"Idempotency-Key": "same-key"},
        )
        wrong_target = await client.post(
            f"/api/jobs/{job['id']}/stages/graph/retry",
            headers={"Idempotency-Key": "same-key"},
        )
        wrong_action = await client.post(
            f"/api/jobs/{job['id']}/actions/cancel",
            headers={"Idempotency-Key": "same-key"},
        )
        completed = (
            await client.post("/api/jobs", json={"query": "platform key"})
        ).json()
        store.compatibility_update_job(completed["id"], status="completed")
        youtube = await client.post(
            f"/api/jobs/{completed['id']}/publish/youtube",
            headers={"Idempotency-Key": "platform-key"},
        )
        instagram = await client.post(
            f"/api/jobs/{completed['id']}/publish/instagram",
            headers={"Idempotency-Key": "platform-key"},
        )
    assert first.status_code == youtube.status_code == 200
    assert wrong_target.status_code == wrong_action.status_code == 409
    assert instagram.status_code == 409
    decisions = store.list_decisions(job["id"])
    assert decisions[-1]["accepted"] is False
    assert dispatcher.wakes == 4  # two submissions + one stage retry + one publish


@pytest.mark.anyio
async def test_upload_idempotency_key_cannot_replay_an_unrelated_action(tmp_path):
    app, store, _dispatcher = await _client(tmp_path)
    settings = Settings(tmp_path, admin_api_token="token")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        app.state.pipeline_services = SimpleNamespace(
            subtitle_service=SubtitleService(
                store,
                OpenSubtitlesClient(api_key="unused", user_agent="tests"),
                SubtitleCache(tmp_path / "cache"),
                settings,
            )
        )
        job = (await client.post("/api/jobs", json={"query": "upload scope"})).json()
        await client.post(
            f"/api/jobs/{job['id']}/actions/cancel",
            headers={"Idempotency-Key": "shared-upload-key"},
        )
        response = await client.post(
            f"/api/jobs/{job['id']}/subtitles/upload",
            files={
                "file": (
                    "safe.srt",
                    b"1\n00:00:00,000 --> 00:00:01,000\nHello\n",
                )
            },
            headers={"Idempotency-Key": "shared-upload-key"},
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert store.list_candidates(job["id"]) == []
    assert store.list_decisions(job["id"])[-1]["action"] == "upload_subtitle"
    assert store.list_decisions(job["id"])[-1]["accepted"] is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        {"remote_id": "remote-only"},
        {"reconciliation": "uploaded"},
        {"reconciliation": "uploaded", "remote_id": "../bad"},
        {"reconciliation": "not_uploaded", "remote_id": "remote"},
        {"reconciliation": "other"},
    ],
)
async def test_publish_action_body_enforces_reconciliation_semantics(tmp_path, body):
    app, store, dispatcher = await _client(tmp_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        job = (await client.post("/api/jobs", json={"query": "body semantics"})).json()
        store.compatibility_update_job(job["id"], status="completed")
        before = dispatcher.wakes
        response = await client.post(
            f"/api/jobs/{job['id']}/publish/youtube", json=body
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert dispatcher.wakes == before
    assert store.list_releases(job["id"]) == []


@pytest.mark.anyio
async def test_state_equivalent_duplicate_without_key_is_stable(tmp_path):
    app, store, dispatcher = await _client(tmp_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        job = (await client.post("/api/jobs", json={"query": "duplicate"})).json()
        first = await client.post(f"/api/jobs/{job['id']}/actions/cancel")
        replay = await client.post(f"/api/jobs/{job['id']}/actions/cancel")
    assert first.status_code == replay.status_code == 200
    assert first.json()["decision"]["id"] == replay.json()["decision"]["id"]
    assert replay.json()["changed"] is False
    assert dispatcher.wakes == 2


@pytest.mark.anyio
async def test_idempotency_key_is_never_returned_in_action_or_detail(tmp_path):
    app, store, _dispatcher = await _client(tmp_path)
    secret_key = "operator-secret-idempotency-value"
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        job = (await client.post("/api/jobs", json={"query": "private key"})).json()
        action = await client.post(
            f"/api/jobs/{job['id']}/actions/cancel",
            headers={"Idempotency-Key": secret_key},
        )
        detail = await client.get(f"/api/jobs/{job['id']}")
    assert action.status_code == detail.status_code == 200
    assert secret_key not in action.text and secret_key not in detail.text
    assert "idempotency_key" not in action.text and "idempotency_key" not in detail.text
    assert store.list_decisions(job["id"])[0]["idempotency_key"] == secret_key


@pytest.mark.anyio
@pytest.mark.parametrize(
    "candidate_id",
    ["candidate", "../../escape", "candidate_1234", f"candidate_{'f' * 32}"],
)
async def test_candidate_selection_rejects_invalid_or_unknown_ids_without_a_decision(
    tmp_path, candidate_id
):
    app, store, _dispatcher = await _client(tmp_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        job = (
            await client.post("/api/jobs", json={"query": "strict candidate"})
        ).json()
        response = await client.post(
            f"/api/jobs/{job['id']}/subtitle-candidates/{candidate_id}/select"
        )
    assert response.status_code == 404
    assert store.list_decisions(job["id"]) == []


async def _client(tmp_path, *, store=None, dispatcher=None):
    from api.main import create_app

    store = store or OperationStore(tmp_path / "db")
    dispatcher = dispatcher or Dispatcher(store)
    app = create_app(Settings(tmp_path, admin_api_token="token"), store, dispatcher)
    return app, store, dispatcher


@pytest.mark.anyio
async def test_cancel_conflict_records_rejected_decision_and_does_not_wake(tmp_path):
    app, store, dispatcher = await _client(tmp_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        job = (await client.post("/api/jobs", json={"query": "done"})).json()
        store.claim_next_job("worker", lease_seconds=30)
        store.transition_job(
            job["id"], "completed", expected_state="running", lease_owner="worker"
        )
        before = dispatcher.wakes
        response = await client.post(
            f"/api/jobs/{job['id']}/actions/cancel",
            headers={"Idempotency-Key": "cancel-done"},
        )
    assert response.status_code == 409
    decisions = store.list_decisions(job["id"])
    assert decisions[-1]["action"] == "cancel" and decisions[-1]["accepted"] is False
    assert dispatcher.wakes == before


@pytest.mark.anyio
async def test_resume_and_retry_stage_are_durable_idempotent_and_wake_once(tmp_path):
    app, store, dispatcher = await _client(tmp_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        cancelled = (await client.post("/api/jobs", json={"query": "cancelled"})).json()
        store.request_cancel(cancelled["id"])
        before = dispatcher.wakes
        first = await client.post(
            f"/api/jobs/{cancelled['id']}/actions/resume",
            headers={"Idempotency-Key": "resume-1"},
        )
        replay = await client.post(
            f"/api/jobs/{cancelled['id']}/actions/resume",
            headers={"Idempotency-Key": "resume-1"},
        )
        assert first.json()["run"]["state"] == replay.json()["run"]["state"] == "queued"
        assert dispatcher.wakes == before + 1
        store.request_cancel(cancelled["id"])

        failed = (await client.post("/api/jobs", json={"query": "failed stage"})).json()
        store.ensure_stage(failed["id"], "encode", state="queued")
        store.claim_next_job("worker", lease_seconds=30)
        store.transition_stage(failed["id"], "encode", "running", lease_owner="worker")
        store.transition_stage_and_job(
            failed["id"], "encode", "failed", "failed", lease_owner="worker"
        )
        before_retry = dispatcher.wakes
        retried = await client.post(
            f"/api/jobs/{failed['id']}/stages/encode/retry",
            headers={"Idempotency-Key": "retry-1"},
        )
        replayed = await client.post(
            f"/api/jobs/{failed['id']}/stages/encode/retry",
            headers={"Idempotency-Key": "retry-1"},
        )
    assert retried.status_code == replayed.status_code == 200
    detail = store.get_job_detail(failed["id"])
    assert detail["run"]["state"] == "queued"
    assert (
        next(stage for stage in detail["stages"] if stage["name"] == "encode")["state"]
        == "queued"
    )
    assert dispatcher.wakes == before_retry + 1


@pytest.mark.anyio
async def test_rediscovery_and_manual_selection_are_idempotent(tmp_path):
    app, store, dispatcher = await _client(tmp_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        job = (await client.post("/api/jobs", json={"query": "subtitles"})).json()
        candidate, _ = store.record_candidate(
            job["id"], "fake", "1", status="validated"
        )
        base = dispatcher.wakes
        selected = await client.post(
            f"/api/jobs/{job['id']}/subtitle-candidates/{candidate['id']}/select",
            headers={"Idempotency-Key": "select-1"},
        )
        selected_again = await client.post(
            f"/api/jobs/{job['id']}/subtitle-candidates/{candidate['id']}/select",
            headers={"Idempotency-Key": "select-1"},
        )
        rediscovered = await client.post(
            f"/api/jobs/{job['id']}/subtitles/rediscover",
            headers={"Idempotency-Key": "discover-1"},
        )
        rediscovered_again = await client.post(
            f"/api/jobs/{job['id']}/subtitles/rediscover",
            headers={"Idempotency-Key": "discover-1"},
        )
    assert all(
        response.status_code == 200
        for response in (selected, selected_again, rediscovered, rediscovered_again)
    )
    assert dispatcher.wakes == base + 2


@pytest.mark.anyio
async def test_publish_retry_reconciliation_and_stats_refresh_are_durable_only(
    tmp_path,
):
    app, store, dispatcher = await _client(tmp_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        job = (await client.post("/api/jobs", json={"query": "publish"})).json()
        store.claim_next_job("worker", lease_seconds=30)
        store.transition_job(
            job["id"], "completed", expected_state="running", lease_owner="worker"
        )
        base = dispatcher.wakes
        requested = await client.post(
            f"/api/jobs/{job['id']}/publish/youtube",
            headers={"Idempotency-Key": "publish-1"},
        )
        replay = await client.post(
            f"/api/jobs/{job['id']}/publish/youtube",
            headers={"Idempotency-Key": "publish-1"},
        )
        store.upsert_release(
            job["id"], "youtube", status="failed", safe_error_message="failed"
        )
        retried = await client.post(
            f"/api/jobs/{job['id']}/publish/youtube/retry",
            headers={"Idempotency-Key": "retry-publish"},
        )
        store.upsert_release(
            job["id"], "youtube", status="uploaded", remote_id="remote-safe"
        )
        refreshed = await client.post(
            f"/api/jobs/{job['id']}/stats/refresh",
            headers={"Idempotency-Key": "stats-1"},
        )
    assert [
        response.status_code for response in (requested, replay, retried, refreshed)
    ] == [200, 200, 200, 200]
    assert store.list_releases(job["id"])[0]["status"] in {"pending", "uploaded"}
    assert store.list_releases(job["id"])[0]["metadata"]["privacy_status"] == "private"
    assert dispatcher.wakes == base + 3
    assert store.platform_stats(job["id"]) == []  # no provider/stat fetch ran inline


@pytest.mark.anyio
async def test_upload_enforces_size_type_parse_generated_path_and_idempotency(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(OpenSubtitlesClient, "MAX_DOWNLOAD_BYTES", 16)
    app, store, dispatcher = await _client(tmp_path)
    settings = Settings(tmp_path, admin_api_token="token")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        app.state.pipeline_services = SimpleNamespace(
            subtitle_service=SubtitleService(
                store,
                OpenSubtitlesClient(api_key="unused", user_agent="tests"),
                SubtitleCache(tmp_path / "cache"),
                settings,
            )
        )
        job = (await client.post("/api/jobs", json={"query": "upload"})).json()
        oversized = await client.post(
            f"/api/jobs/{job['id']}/subtitles/upload",
            files={"file": ("x.srt", b"x" * 17)},
        )
        wrong_type = await client.post(
            f"/api/jobs/{job['id']}/subtitles/upload",
            files={"file": ("x.txt", b"text")},
        )
        malformed = await client.post(
            f"/api/jobs/{job['id']}/subtitles/upload",
            files={"file": ("bad.srt", b"not an srt")},
        )
        monkeypatch.setattr(OpenSubtitlesClient, "MAX_DOWNLOAD_BYTES", 1024)
        valid_srt = b"1\n00:00:00,000 --> 00:00:01,000\nHello\n"
        first = await client.post(
            f"/api/jobs/{job['id']}/subtitles/upload",
            files={"file": ("../../escape.srt", valid_srt)},
            headers={"Idempotency-Key": "upload-1"},
        )
        replay = await client.post(
            f"/api/jobs/{job['id']}/subtitles/upload",
            files={"file": ("../../escape.srt", valid_srt)},
            headers={"Idempotency-Key": "upload-1"},
        )
    assert (
        oversized.status_code == 413
        and wrong_type.status_code == malformed.status_code == 422
    )
    assert oversized.json()["error"]["code"] == "payload_too_large"
    assert oversized.json()["error"]["request_id"] == oversized.headers["x-request-id"]
    assert first.status_code == replay.status_code == 200
    candidates = store.list_candidates(job["id"])
    assert len(candidates) == 1 and candidates[0]["provider_filename"] == "escape.srt"
    internal = store.get_candidate(candidates[0]["id"], include_internal=True)
    assert (
        internal["artifact_path"]
        and str(tmp_path.resolve()) in internal["artifact_path"]
    )
    assert ".." not in internal["artifact_path"]
    assert len(store.list_decisions(job["id"])) == 1


@pytest.mark.anyio
async def test_upload_rolls_back_candidate_and_artifact_when_decision_fails(
    tmp_path, monkeypatch
):
    app, store, _dispatcher = await _client(tmp_path)
    settings = Settings(tmp_path, admin_api_token="token")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        app.state.pipeline_services = SimpleNamespace(
            subtitle_service=SubtitleService(
                store,
                OpenSubtitlesClient(api_key="unused", user_agent="tests"),
                SubtitleCache(tmp_path / "cache"),
                settings,
            )
        )
        job = (await client.post("/api/jobs", json={"query": "rollback"})).json()
        monkeypatch.setattr(
            store,
            "finalize_uploaded_candidate",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("injected decision failure")
            ),
        )
        response = await client.post(
            f"/api/jobs/{job['id']}/subtitles/upload",
            files={"file": ("safe.srt", b"1\n00:00:00,000 --> 00:00:01,000\nHello\n")},
            headers={"Idempotency-Key": "rollback-1"},
        )
    assert response.status_code == 500
    assert store.list_candidates(job["id"]) == []
    assert not list((tmp_path / "results" / "subtitle-candidates").rglob("*.srt"))


@pytest.mark.anyio
async def test_upload_rollback_ignores_tampered_persisted_artifact_path(
    tmp_path, monkeypatch
):
    app, store, _dispatcher = await _client(tmp_path)
    settings = Settings(tmp_path, admin_api_token="token")
    outside = tmp_path / "must-survive-route-cleanup.srt"
    outside.write_bytes(b"outside")

    def fail_after_tampering(job_id, candidate_id, **_kwargs):
        with store._mutation() as connection:
            connection.execute(
                "UPDATE subtitle_candidates SET artifact_path = ? WHERE id = ?",
                (str(outside), candidate_id),
            )
        raise RuntimeError("injected decision failure")

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        app.state.pipeline_services = SimpleNamespace(
            subtitle_service=SubtitleService(
                store,
                OpenSubtitlesClient(api_key="unused", user_agent="tests"),
                SubtitleCache(tmp_path / "cache"),
                settings,
            )
        )
        job = (await client.post("/api/jobs", json={"query": "tampered rollback"})).json()
        monkeypatch.setattr(store, "finalize_uploaded_candidate", fail_after_tampering)
        response = await client.post(
            f"/api/jobs/{job['id']}/subtitles/upload",
            files={"file": ("safe.srt", b"1\n00:00:00,000 --> 00:00:01,000\nHello\n")},
        )
    assert response.status_code == 500
    assert outside.read_bytes() == b"outside"
    assert store.list_candidates(job["id"]) == []
    assert not list((tmp_path / "results" / "subtitle-candidates").rglob("*.srt"))


@pytest.mark.anyio
async def test_upload_rolls_back_when_durable_event_fails(tmp_path, monkeypatch):
    app, store, _dispatcher = await _client(tmp_path)
    settings = Settings(tmp_path, admin_api_token="token")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        app.state.pipeline_services = SimpleNamespace(
            subtitle_service=SubtitleService(
                store,
                OpenSubtitlesClient(api_key="unused", user_agent="tests"),
                SubtitleCache(tmp_path / "cache"),
                settings,
            )
        )
        job = (await client.post("/api/jobs", json={"query": "event rollback"})).json()
        monkeypatch.setattr(
            store,
            "_insert_event",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("injected event failure")
            ),
        )
        response = await client.post(
            f"/api/jobs/{job['id']}/subtitles/upload",
            files={"file": ("safe.srt", b"1\n00:00:00,000 --> 00:00:01,000\nHello\n")},
            headers={"Idempotency-Key": "event-fail"},
        )
    assert response.status_code == 500
    assert store.list_candidates(job["id"]) == []
    assert store.list_decisions(job["id"]) == []


def test_interrupted_upload_is_durably_pending_and_reconciled_on_restart(tmp_path):
    store = OperationStore(tmp_path / "db")
    store.initialize()
    job, _ = store.create_or_get_active_job("", "interrupted upload", "interrupted")
    settings = Settings(tmp_path, admin_api_token="token")
    service = SubtitleService(
        store,
        OpenSubtitlesClient(api_key="unused", user_agent="tests"),
        SubtitleCache(tmp_path / "cache"),
        settings,
    )
    candidate = service.upload(
        job["id"],
        "safe.srt",
        b"1\n00:00:00,000 --> 00:00:01,000\nHello\n",
    )
    internal = store.get_candidate(candidate["id"], include_internal=True)
    assert internal["status"] == "upload_pending"
    assert Path(internal["artifact_path"]).is_file()
    assert store.list_decisions(job["id"]) == []

    recovered = service.recover_pending_uploads()
    after = store.get_candidate(candidate["id"], include_internal=True)
    assert recovered == [candidate["id"]]
    assert after["status"] == "rejected"
    assert after["artifact_path"] is None
    assert not Path(internal["artifact_path"]).exists()


def test_pending_upload_recovery_never_deletes_an_unconfined_path(tmp_path):
    store = OperationStore(tmp_path / "db")
    store.initialize()
    job, _ = store.create_or_get_active_job("", "confined recovery", "confined")
    settings = Settings(tmp_path, admin_api_token="token")
    service = SubtitleService(
        store,
        OpenSubtitlesClient(api_key="unused", user_agent="tests"),
        SubtitleCache(tmp_path / "cache"),
        settings,
    )
    candidate = service.upload(
        job["id"],
        "safe.srt",
        b"1\n00:00:00,000 --> 00:00:01,000\nHello\n",
    )
    outside = tmp_path / "must-survive.srt"
    outside.write_bytes(b"outside")
    with store._mutation() as connection:
        connection.execute(
            "UPDATE subtitle_candidates SET artifact_path = ? WHERE id = ?",
            (str(outside), candidate["id"]),
        )
    service.recover_pending_uploads()
    assert outside.read_bytes() == b"outside"
    assert store.get_candidate(candidate["id"])["status"] == "rejected"


def test_upload_retry_ignores_tampered_persisted_artifact_path(tmp_path):
    store = OperationStore(tmp_path / "db")
    store.initialize()
    job, _ = store.create_or_get_active_job("", "retry confinement", "retry")
    settings = Settings(tmp_path, admin_api_token="token")
    service = SubtitleService(
        store,
        OpenSubtitlesClient(api_key="unused", user_agent="tests"),
        SubtitleCache(tmp_path / "cache"),
        settings,
    )
    content = b"1\n00:00:00,000 --> 00:00:01,000\nHello\n"
    candidate = service.upload(job["id"], "safe.srt", content)
    outside = tmp_path / "must-survive-upload-retry.srt"
    outside.write_bytes(b"outside")
    with store._mutation() as connection:
        connection.execute(
            "UPDATE subtitle_candidates SET artifact_path = ? WHERE id = ?",
            (str(outside), candidate["id"]),
        )

    retried = service.upload(job["id"], "safe.srt", content)

    assert outside.read_bytes() == b"outside"
    assert retried["id"] == candidate["id"]
    internal = store.get_candidate(candidate["id"], include_internal=True)
    assert Path(internal["artifact_path"]).is_file()
    assert str(internal["artifact_path"]).startswith(
        str((settings.results_dir / "subtitle-candidates").resolve())
    )


def test_pending_upload_recovery_removes_generated_artifact_before_path_commit(
    tmp_path,
):
    store = OperationStore(tmp_path / "db")
    store.initialize()
    job, _ = store.create_or_get_active_job("", "precommit crash", "precommit")
    settings = Settings(tmp_path, admin_api_token="token")
    service = SubtitleService(
        store,
        OpenSubtitlesClient(api_key="unused", user_agent="tests"),
        SubtitleCache(tmp_path / "cache"),
        settings,
    )
    candidate = service.upload(
        job["id"],
        "safe.srt",
        b"1\n00:00:00,000 --> 00:00:01,000\nHello\n",
    )
    internal = store.get_candidate(candidate["id"], include_internal=True)
    generated = Path(internal["artifact_path"])
    with store._mutation() as connection:
        connection.execute(
            "UPDATE subtitle_candidates SET artifact_path = NULL WHERE id = ?",
            (candidate["id"],),
        )
    service.recover_pending_uploads()
    assert not generated.exists()
    assert store.get_candidate(candidate["id"])["status"] == "rejected"


@pytest.mark.anyio
async def test_state_equivalent_upload_without_key_reuses_finalized_decision(tmp_path):
    app, store, dispatcher = await _client(tmp_path)
    settings = Settings(tmp_path, admin_api_token="token")
    content = b"1\n00:00:00,000 --> 00:00:01,000\nHello\n"
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        app.state.pipeline_services = SimpleNamespace(
            subtitle_service=SubtitleService(
                store,
                OpenSubtitlesClient(api_key="unused", user_agent="tests"),
                SubtitleCache(tmp_path / "cache"),
                settings,
            )
        )
        job = (await client.post("/api/jobs", json={"query": "no key upload"})).json()
        before = dispatcher.wakes
        first = await client.post(
            f"/api/jobs/{job['id']}/subtitles/upload",
            files={"file": ("safe.srt", content)},
        )
        replay = await client.post(
            f"/api/jobs/{job['id']}/subtitles/upload",
            files={"file": ("safe.srt", content)},
        )
    assert first.status_code == replay.status_code == 200
    assert first.json()["decision"]["id"] == replay.json()["decision"]["id"]
    assert len(store.list_decisions(job["id"])) == 1
    assert dispatcher.wakes == before + 1


@pytest.mark.anyio
async def test_ambiguous_publish_requires_explicit_reconciliation(tmp_path):
    app, store, dispatcher = await _client(tmp_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        job = (await client.post("/api/jobs", json={"query": "ambiguous"})).json()
        store.claim_next_job("worker", lease_seconds=30)
        store.transition_job(
            job["id"], "completed", expected_state="running", lease_owner="worker"
        )
        store.upsert_release(
            job["id"],
            "youtube",
            status="needs_attention",
            safe_error_code="ambiguous_publish_outcome",
            safe_error_message="Unknown",
        )
        before = dispatcher.wakes
        response = await client.post(
            f"/api/jobs/{job['id']}/publish/youtube",
            json={"reconciliation": "uploaded", "remote_id": "remote-confirmed"},
            headers={"Idempotency-Key": "reconcile-1"},
        )
        replay = await client.post(
            f"/api/jobs/{job['id']}/publish/youtube",
            json={"reconciliation": "uploaded", "remote_id": "remote-confirmed"},
            headers={"Idempotency-Key": "reconcile-1"},
        )
    assert response.status_code == replay.status_code == 200
    assert response.json()["decision"]["id"] == replay.json()["decision"]["id"]
    assert response.json()["release"] == replay.json()["release"]
    assert replay.json()["changed"] is False
    assert store.list_releases(job["id"])[0]["status"] == "uploaded"
    assert store.list_releases(job["id"])[0]["remote_id"] == "remote-confirmed"
    assert dispatcher.wakes == before + 1
