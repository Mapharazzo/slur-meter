import asyncio
from types import SimpleNamespace
from typing import get_args, get_origin

import httpx
import pytest

from api.artifacts import ArtifactManager
from api.database import OperationStore
from api.settings import Settings


class Dispatcher:
    async def start(self):
        pass

    async def stop(self):
        pass

    def wake(self):
        pass


@pytest.mark.anyio
async def test_health_is_public_and_jobs_require_a_bearer_token(tmp_path):
    from api.main import create_app

    app = create_app(
        Settings(tmp_path, admin_api_token="token"),
        OperationStore(tmp_path / "db.sqlite"),
        Dispatcher(),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            assert (await client.get("/api/health")).status_code == 200
            denied = await client.get("/api/jobs")
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "unauthorized"


@pytest.mark.anyio
async def test_submit_and_list_are_structured(tmp_path):
    from api.main import create_app

    app = create_app(
        Settings(tmp_path, admin_api_token="token"),
        OperationStore(tmp_path / "db.sqlite"),
        Dispatcher(),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client:
            bad = await client.post("/api/jobs", json={"imdb_id": "tt1", "query": "x"})
            made = await client.post("/api/jobs", json={"imdb_id": "tt0110912"})
            listed = await client.get("/api/jobs?limit=1")
    assert bad.status_code == 422
    assert made.status_code == 201
    assert listed.json()["total"] == 1


@pytest.mark.anyio
async def test_submission_rejects_blank_extra_and_operational_ids_are_opaque(tmp_path):
    from api.main import create_app

    app = create_app(
        Settings(tmp_path, admin_api_token="token"),
        OperationStore(tmp_path / "db.sqlite"),
        Dispatcher(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        blank = await client.post("/api/jobs", json={"query": "  "})
        extra = await client.post("/api/jobs", json={"query": "film", "unexpected": 1})
        traversal = await client.get("/api/jobs/%2E%2E%2Fetc")
        imdb_alias = await client.get("/api/jobs/tt0110912")
    assert blank.status_code == extra.status_code == 422
    assert traversal.status_code == imdb_alias.status_code == 404


@pytest.mark.anyio
@pytest.mark.parametrize("imdb_id", ["abc", "tt", "tt12345678901"])
async def test_submission_rejects_invalid_imdb_ids_with_structured_validation(
    tmp_path, imdb_id
):
    from api.main import create_app

    app = create_app(
        Settings(tmp_path, admin_api_token="token"),
        OperationStore(tmp_path / "db"),
        Dispatcher(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        response = await client.post("/api/jobs", json={"imdb_id": imdb_id})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


@pytest.mark.anyio
async def test_submission_is_unique_for_normalized_queries_and_active_imdb(tmp_path):
    from api.main import create_app

    dispatcher = Dispatcher()
    app = create_app(
        Settings(tmp_path, admin_api_token="token"),
        OperationStore(tmp_path / "db"),
        dispatcher,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        first = await client.post("/api/jobs", json={"query": "  The   Movie "})
        duplicate = await client.post("/api/jobs", json={"query": "the movie"})
        imdb = await client.post("/api/jobs", json={"imdb_id": "tt0110912"})
        imdb_duplicate = await client.post("/api/jobs", json={"imdb_id": "0110912"})
    assert first.json()["id"] == duplicate.json()["id"]
    assert imdb.json()["id"] == imdb_duplicate.json()["id"]


@pytest.mark.anyio
async def test_job_pagination_total_filter_query_and_bounds(tmp_path):
    from api.main import create_app

    store = OperationStore(tmp_path / "db")
    app = create_app(Settings(tmp_path, admin_api_token="token"), store, Dispatcher())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        alpha = (await client.post("/api/jobs", json={"query": "Alpha Film"})).json()
        await client.post("/api/jobs", json={"query": "Beta Film"})
        store.request_cancel(alpha["id"])
        page = await client.get("/api/jobs?limit=1&offset=1")
        filtered = await client.get("/api/jobs?state=cancelled")
        queried = await client.get("/api/jobs?query=BETA")
        bad = [
            await client.get("/api/jobs?limit=0"),
            await client.get("/api/jobs?limit=501"),
            await client.get("/api/jobs?offset=-1"),
        ]
    assert (
        page.json()["total"] == 2
        and page.json()["limit"] == 1
        and page.json()["offset"] == 1
    )
    assert (
        filtered.json()["total"] == 1
        and filtered.json()["items"][0]["id"] == alpha["id"]
    )
    assert (
        queried.json()["total"] == 1
        and queried.json()["items"][0]["query"] == "beta film"
    )
    assert all(response.status_code == 422 for response in bad)


@pytest.mark.anyio
async def test_detail_is_aggregate_events_are_incremental_and_internal_fields_are_redacted(
    tmp_path,
):
    from api.main import create_app

    store = OperationStore(tmp_path / "db")
    app = create_app(Settings(tmp_path, admin_api_token="token"), store, Dispatcher())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        job = (await client.post("/api/jobs", json={"query": "Aggregate"})).json()
        store.ensure_stage(job["id"], "input_resolution")
        store.record_cost(
            job["id"],
            "metadata",
            "fake",
            detail={"path": str(tmp_path / "secret"), "authorization": "Bearer hidden"},
        )
        first = await client.get(f"/api/jobs/{job['id']}")
        after = first.json()["last_event_id"]
        store.record_event(job["id"], event_type="progress", message="next")
        events = await client.get(f"/api/jobs/{job['id']}/events?after={after}")
    payload = first.json()
    assert {
        "run",
        "stages",
        "attempts",
        "candidates",
        "events",
        "decisions",
        "costs",
        "releases",
        "publishing_attempts",
        "available_actions",
        "server_time",
        "last_event_id",
    } <= set(payload)
    assert [event["id"] for event in events.json()["items"]] == sorted(
        event["id"] for event in events.json()["items"]
    )
    assert all(event["id"] > after for event in events.json()["items"])
    rendered = first.text
    assert (
        "artifact_path" not in rendered
        and "lease_owner" not in rendered
        and "diagnostics" not in rendered
    )
    assert str(tmp_path) not in rendered and "Bearer hidden" not in rendered


@pytest.mark.anyio
async def test_events_cursor_is_derived_from_the_returned_snapshot(tmp_path):
    from api.main import create_app

    class RacingStore(OperationStore):
        reads = 0

        def list_events(self, job_id, *, after=0, limit=500):
            items = super().list_events(job_id, after=after, limit=limit)
            self.reads += 1
            if self.reads == 1:
                self.record_event(job_id, event_type="concurrent", message="later")
            return items

    store = RacingStore(tmp_path / "db")
    app = create_app(Settings(tmp_path, admin_api_token="token"), store, Dispatcher())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        job = (await client.post("/api/jobs", json={"query": "cursor"})).json()
        store.reads = 0
        response = await client.get(f"/api/jobs/{job['id']}/events?after=0")
    payload = response.json()
    assert payload["last_event_id"] == max(row["id"] for row in payload["items"])


@pytest.mark.anyio
async def test_detail_lists_all_actions_derived_from_durable_state(tmp_path):
    from api.main import create_app

    store = OperationStore(tmp_path / "db")
    app = create_app(Settings(tmp_path, admin_api_token="token"), store, Dispatcher())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        job = (await client.post("/api/jobs", json={"query": "actions"})).json()
        store.ensure_stage(job["id"], "encode", state="queued")
        store.claim_next_job("worker", lease_seconds=30)
        store.transition_stage(job["id"], "encode", "running", lease_owner="worker")
        store.transition_stage_and_job(
            job["id"], "encode", "failed", "failed", lease_owner="worker"
        )
        response = await client.get(f"/api/jobs/{job['id']}")
    actions = response.json()["available_actions"]
    assert "cancel" in actions and "resume" in actions
    assert "retry_stage:encode" in actions and "rediscover_subtitles" in actions


@pytest.mark.anyio
async def test_compatibility_analytics_are_store_backed_true_aggregates(tmp_path):
    from api.main import create_app

    store = OperationStore(tmp_path / "db")
    app = create_app(Settings(tmp_path, admin_api_token="token"), store, Dispatcher())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        job = (await client.post("/api/jobs", json={"imdb_id": "tt0110912"})).json()
        store.record_cost(job["id"], "audio", "fake", amount_usd=1.25, units=2)
        store.upsert_release(
            job["id"], "youtube", status="failed", safe_error_message="safe"
        )
        store.upsert_revenue(
            job["id"], "youtube", "2026-07-21", views=10, likes=2, revenue_usd=0.5
        )
        costs = await client.get("/api/costs")
        job_costs = await client.get(f"/api/jobs/{job['id']}/costs")
        releases = await client.get("/api/releases")
        stats = await client.get("/api/jobs/tt0110912/platform-stats")
        revenue = await client.get("/api/revenue")
        leaderboard = await client.get("/api/leaderboard")
    assert costs.json()[0]["count"] == 1 and costs.json()[0]["total_units"] == 2
    assert job_costs.json()["total"] == 1
    assert releases.json()["total"] == 1 and releases.json()["items"][0]["updated_at"]
    assert stats.json()["items"][0]["views"] == 10
    assert revenue.json()["total"] == 1
    assert leaderboard.json()["total"] == 0
    for response in (costs, job_costs, releases, stats, revenue, leaderboard):
        assert (
            "sqlite3.Row" not in response.text and "artifact_path" not in response.text
        )


@pytest.mark.anyio
async def test_compatibility_aliases_are_strict_and_api_never_falls_through_to_spa(
    tmp_path,
):
    from api.main import create_app

    store = OperationStore(tmp_path / "db")
    app = create_app(Settings(tmp_path, admin_api_token="token"), store, Dispatcher())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        await client.post("/api/jobs", json={"imdb_id": "tt0110912"})
        alias = await client.get("/api/releases/tt0110912")
        bad = await client.get("/api/releases/%2E%2E%2Fetc")
        api_missing = await client.get("/api/deep/missing")
        spa = await client.get("/some/ui/route")
    assert alias.status_code == 200
    assert bad.status_code == 404 and api_missing.status_code == 404
    assert api_missing.headers["content-type"].startswith("application/json")
    assert spa.status_code == 200 and spa.headers["content-type"].startswith(
        "text/html"
    )


@pytest.mark.anyio
async def test_media_and_analysis_aliases_use_confined_validated_artifacts(tmp_path):
    from api.main import create_app

    store = OperationStore(tmp_path / "db")
    app = create_app(Settings(tmp_path, admin_api_token="token"), store, Dispatcher())
    artifacts = ArtifactManager(tmp_path / "output")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        app.state.pipeline_services = SimpleNamespace(artifacts=artifacts)
        job = (await client.post("/api/jobs", json={"imdb_id": "tt0110912"})).json()
        staged = artifacts.new_staging_file(job["id"], "encode", suffix=".mp4")
        staged.write_bytes(b"video")
        manifest = artifacts.promote_file(
            job["id"], "encode", staged, final_name="final.mp4", artifact_kind="file"
        )
        store.ensure_stage(job["id"], "encode", state="queued")
        store.claim_next_job("worker", lease_seconds=30)
        store.transition_stage(job["id"], "encode", "running", lease_owner="worker")
        store.transition_stage(
            job["id"],
            "encode",
            "completed",
            output_manifest=manifest,
            lease_owner="worker",
        )
        store.compatibility_update_job(
            job["id"],
            analysis_json={
                "summary": {"total_hard": 7},
                "path": str(tmp_path / "private"),
            },
        )
        video = await asyncio.wait_for(client.get("/api/videos/tt0110912"), 3)
        analysis = await asyncio.wait_for(client.get("/api/analysis/tt0110912"), 3)
        traversal = await asyncio.wait_for(client.get("/api/videos/%2E%2E%2Fetc"), 3)
    assert video.status_code == 200 and video.content == b"video"
    assert analysis.status_code == 200 and analysis.json()["summary"]["total_hard"] == 7
    assert str(tmp_path) not in analysis.text
    assert traversal.status_code == 404


def test_aggregate_record_schemas_are_explicit_and_forbid_extra_fields():
    from api.schemas import APIModel, DetailResponse, EventPageResponse

    for field_name in (
        "stages",
        "attempts",
        "candidates",
        "events",
        "decisions",
        "publishing_attempts",
        "costs",
        "releases",
        "revenue",
    ):
        annotation = DetailResponse.model_fields[field_name].annotation
        assert get_origin(annotation) is list
        record_type = get_args(annotation)[0]
        assert isinstance(record_type, type) and issubclass(record_type, APIModel)
        assert record_type.model_config["extra"] == "forbid"
    event_type = get_args(EventPageResponse.model_fields["items"].annotation)[0]
    assert isinstance(event_type, type) and issubclass(event_type, APIModel)


def test_every_structured_and_compatibility_route_has_a_strict_response_model(tmp_path):
    from api.main import create_app
    from api.schemas import APIModel

    app = create_app(Settings(tmp_path), OperationStore(tmp_path / "db"), Dispatcher())
    required_paths = {
        "/api/jobs/{job_id}/publish/{platform}",
        "/api/jobs/{job_id}/publish/{platform}/retry",
        "/api/jobs/{job_id}/costs",
        "/api/costs",
        "/api/releases",
        "/api/releases/{identifier}",
        "/api/jobs/{identifier}/platform-stats",
        "/api/revenue",
        "/api/alerts",
        "/api/leaderboard",
        "/api/analysis/{identifier}",
    }
    models = {
        route.path: route.response_model
        for route in app.routes
        if getattr(route, "path", None) in required_paths
    }
    assert set(models) == required_paths
    for model in models.values():
        record_model = get_args(model)[0] if get_origin(model) is list else model
        assert isinstance(record_model, type) and issubclass(record_model, APIModel)
        assert record_model.model_config["extra"] == "forbid"


@pytest.mark.anyio
async def test_summary_leaderboard_and_revenue_are_true_beyond_legacy_limits(tmp_path):
    from api.main import create_app

    store = OperationStore(tmp_path / "db")
    app = create_app(Settings(tmp_path, admin_api_token="token"), store, Dispatcher())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer token"},
        ) as client,
    ):
        jobs = [
            store.create_or_get_active_job("", f"bulk {index}", f"Bulk {index}")[0]
            for index in range(501)
        ]
        with store._mutation() as connection:
            connection.executemany(
                """UPDATE job_runs SET state = 'completed',
                           artifact_summary_json = ?, finished_at = updated_at
                   WHERE id = ?""",
                [
                    (
                        '{"analysis":{"summary":{"total_hard":1,"total_soft":0,"total_f_bombs":0}}}',
                        job["id"],
                    )
                    for job in jobs
                ],
            )
        for index in range(101):
            store.upsert_revenue(
                jobs[0]["id"],
                "youtube",
                f"2026-{index // 28 + 1:02d}-{index % 28 + 1:02d}",
                views=1,
                revenue_usd=0.25,
            )
        summary = await client.get("/api/operations/summary")
        leaderboard = await client.get("/api/leaderboard")
        revenue = await client.get("/api/revenue")
    assert summary.json()["total"] == 501
    assert summary.json()["states"] == {"completed": 501}
    assert leaderboard.json()["total"] == 501
    assert len(leaderboard.json()["items"]) == 501
    assert revenue.json()["total"] == 101
    assert len(revenue.json()["items"]) == 101
