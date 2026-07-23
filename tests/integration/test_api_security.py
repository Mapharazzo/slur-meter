import inspect
from pathlib import Path

import httpx
import pytest

from api.database import OperationStore
from api.settings import Settings


class Dispatcher:
    def __init__(self, *, fail_start=False):
        self.fail_start = fail_start
        self.started = 0
        self.stopped = 0

    async def start(self):
        self.started += 1
        if self.fail_start:
            raise RuntimeError("construction secret /home/operator/private")

    async def stop(self):
        self.stopped += 1

    def wake(self):
        pass


@pytest.mark.anyio
async def test_errors_have_request_ids_and_unknown_api_is_json(tmp_path):
    from api.main import create_app

    app = create_app(
        Settings(tmp_path, admin_api_token="token", allowed_origins=("http://ui",)),
        OperationStore(tmp_path / "db.sqlite"),
        Dispatcher(),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/nope", headers={"Authorization": "Bearer token"}
            )
            preflight = await client.options(
                "/api/jobs",
                headers={
                    "Origin": "http://ui",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "authorization,idempotency-key",
                },
            )
    assert response.status_code == 404
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
    assert preflight.headers["access-control-allow-origin"] == "http://ui"


@pytest.mark.anyio
async def test_auth_fails_closed_uses_comparator_and_accepts_explicit_local_override(
    tmp_path, monkeypatch
):
    from api import auth
    from api.main import create_app

    calls = []
    monkeypatch.setattr(
        auth.hmac, "compare_digest", lambda a, b: calls.append((a, b)) or a == b
    )
    closed = create_app(
        Settings(tmp_path), OperationStore(tmp_path / "closed.sqlite"), Dispatcher()
    )
    local = create_app(
        Settings(tmp_path, allow_local_development_auth=True),
        OperationStore(tmp_path / "local.sqlite"),
        Dispatcher(),
    )
    tokened = create_app(
        Settings(tmp_path, admin_api_token="secret"),
        OperationStore(tmp_path / "token.sqlite"),
        Dispatcher(),
    )
    async with (
        closed.router.lifespan_context(closed),
        local.router.lifespan_context(local),
        tokened.router.lifespan_context(tokened),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=closed), base_url="http://test"
        ) as client:
            assert (await client.get("/api/jobs")).status_code == 401
            assert (await client.get("/api/health")).status_code == 200
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=local), base_url="http://test"
        ) as client:
            assert (await client.get("/api/jobs")).status_code == 200
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=tokened), base_url="http://test"
        ) as client:
            assert (
                await client.get("/api/jobs", headers={"Authorization": "Basic secret"})
            ).status_code == 401
            assert (
                await client.get("/api/jobs", headers={"Authorization": "Bearer wrong"})
            ).status_code == 401
            assert (
                await client.get(
                    "/api/jobs", headers={"Authorization": "Bearer secret"}
                )
            ).status_code == 200
    assert calls == [("wrong", "secret"), ("secret", "secret")]


@pytest.mark.anyio
async def test_every_api_route_except_health_is_private(tmp_path):
    from api.main import create_app

    app = create_app(
        Settings(tmp_path, admin_api_token="secret"),
        OperationStore(tmp_path / "db"),
        Dispatcher(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        for path in (
            "/api/operations/summary",
            "/api/jobs",
            "/api/costs",
            "/api/releases",
            "/api/leaderboard",
            "/api/revenue",
            "/api/alerts",
        ):
            response = await client.get(path)
            assert response.status_code == 401, path
        assert (await client.get("/api/health")).status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_only_get_health_is_public(tmp_path, method):
    from api.main import create_app

    app = create_app(
        Settings(tmp_path, admin_api_token="token"),
        OperationStore(tmp_path / "db"),
        Dispatcher(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        response = await client.request(method, "/api/health")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("method", "path"), [("POST", "/api/health"), ("OPTIONS", "/api/health")]
)
async def test_authenticated_unsupported_api_method_uses_common_error_envelope(
    tmp_path, method, path
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
        response = await client.request(method, path)
    assert response.status_code == 405
    assert set(response.json()) == {"error"}
    assert response.json()["error"]["code"] == "method_not_allowed"
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


@pytest.mark.anyio
async def test_cors_is_exact_and_allows_only_required_headers(tmp_path):
    from api.main import create_app

    app = create_app(
        Settings(
            tmp_path,
            admin_api_token="token",
            allowed_origins=("https://one.test", "https://two.test"),
        ),
        OperationStore(tmp_path / "db"),
        Dispatcher(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        allowed = await client.options(
            "/api/jobs",
            headers={
                "Origin": "https://one.test",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
            },
        )
        denied = await client.options(
            "/api/jobs",
            headers={
                "Origin": "https://evil.test",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://one.test"
    assert allowed.headers["access-control-allow-credentials"] == "true"
    assert "authorization" in allowed.headers["access-control-allow-headers"].lower()
    assert "idempotency-key" in allowed.headers["access-control-allow-headers"].lower()
    assert allowed.headers.get("access-control-allow-origin") != "*"
    assert "access-control-allow-origin" not in denied.headers


def test_settings_rejects_wildcard_credentialed_cors(tmp_path):
    with pytest.raises(ValueError, match="Wildcard CORS"):
        Settings(tmp_path, allowed_origins=("*",))


@pytest.mark.anyio
async def test_cors_failures_and_early_auth_use_structured_origin_aware_responses(
    tmp_path,
):
    from api.main import create_app

    app = create_app(
        Settings(
            tmp_path,
            admin_api_token="token",
            allowed_origins=("https://allowed.test",),
        ),
        OperationStore(tmp_path / "db"),
        Dispatcher(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        denied_method = await client.options(
            "/api/jobs",
            headers={
                "Origin": "https://allowed.test",
                "Access-Control-Request-Method": "DELETE",
            },
        )
        denied_header = await client.options(
            "/api/jobs",
            headers={
                "Origin": "https://allowed.test",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-forbidden",
            },
        )
        early_auth = await client.get(
            "/api/jobs", headers={"Origin": "https://allowed.test"}
        )
        disallowed_auth = await client.get(
            "/api/jobs", headers={"Origin": "https://denied.test"}
        )
    for response in (denied_method, denied_header):
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"
        assert response.json()["error"]["request_id"] == response.headers[
            "x-request-id"
        ]
        assert response.headers["access-control-allow-origin"] == "https://allowed.test"
    assert early_auth.status_code == 401
    assert early_auth.headers["access-control-allow-origin"] == "https://allowed.test"
    assert early_auth.headers["access-control-allow-credentials"] == "true"
    assert early_auth.headers["access-control-expose-headers"].lower() == "x-request-id"

    assert "access-control-allow-origin" not in disallowed_auth.headers
    assert "access-control-expose-headers" not in disallowed_auth.headers


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/api", "/api/"])
async def test_api_roots_are_authenticated_json_404_not_spa(tmp_path, path):
    from api.main import create_app

    app = create_app(
        Settings(tmp_path, admin_api_token="token"),
        OperationStore(tmp_path / "db"),
        Dispatcher(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        unauthenticated = await client.get(path)
        authenticated = await client.get(
            path, headers={"Authorization": "Bearer token"}
        )
    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 404
    assert authenticated.headers["content-type"].startswith("application/json")
    assert authenticated.json()["error"]["code"] == "not_found"


@pytest.mark.anyio
async def test_only_complete_cors_preflight_bypasses_auth(tmp_path):
    from api.main import create_app

    app = create_app(
        Settings(
            tmp_path,
            admin_api_token="token",
            allowed_origins=("https://allowed.test",),
        ),
        OperationStore(tmp_path / "db"),
        Dispatcher(),
    )
    malformed_headers = (
        {},
        {"Origin": "https://allowed.test"},
        {"Access-Control-Request-Method": "GET"},
        {
            "Origin": "",
            "Access-Control-Request-Method": "GET",
        },
        {
            "Origin": "https://allowed.test",
            "Access-Control-Request-Method": "",
        },
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        malformed = [
            await client.options("/api/jobs", headers=headers)
            for headers in malformed_headers
        ]
        allowed = await client.options(
            "/api/jobs",
            headers={
                "Origin": "https://allowed.test",
                "Access-Control-Request-Method": "GET",
            },
        )
        disallowed = await client.options(
            "/api/jobs",
            headers={
                "Origin": "https://denied.test",
                "Access-Control-Request-Method": "GET",
                "X-Request-ID": "attacker-preflight-id",
            },
        )
    for response in malformed:
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"
        assert response.json()["error"]["request_id"] == response.headers[
            "x-request-id"
        ]
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://allowed.test"
    assert disallowed.status_code == 400
    assert "access-control-allow-origin" not in disallowed.headers
    assert set(disallowed.json()) == {"error"}
    assert disallowed.json()["error"]["code"] == "bad_request"
    assert disallowed.json()["error"]["request_id"] == disallowed.headers[
        "x-request-id"
    ]
    assert disallowed.json()["error"]["request_id"].startswith("req_")
    assert disallowed.headers["x-request-id"] != "attacker-preflight-id"


@pytest.mark.anyio
async def test_common_error_envelopes_request_ids_and_redaction(tmp_path):
    from api.main import create_app

    secret = "super-secret-value"
    store = OperationStore(tmp_path / "db")
    app = create_app(Settings(tmp_path, admin_api_token=secret), store, Dispatcher())
    headers = {"Authorization": f"Bearer {secret}"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=headers,
        ) as client,
    ):
        responses = [
            await client.post("/api/jobs", json={}),
            await client.get("/api/jobs/not-an-id"),
            await client.get("/api/jobs?limit=0"),
            await client.get("/api/videos/not-an-id/segments/bad"),
            await client.get("/api/no-such-route"),
        ]
        # A client-supplied correlation value is never reflected.
        responses.append(
            await client.get(
                "/api/no-such-route", headers={**headers, "X-Request-ID": "attacker-id"}
            )
        )
    for response in responses:
        body = response.json()
        assert set(body) == {"error"}
        assert set(body["error"]) == {
            "code",
            "message",
            "retryable",
            "details",
            "request_id",
        }
        assert body["error"]["request_id"] == response.headers["x-request-id"]
        assert body["error"]["request_id"].startswith("req_")
        assert secret not in response.text
        assert str(tmp_path) not in response.text
    assert responses[-1].headers["x-request-id"] != "attacker-id"


@pytest.mark.anyio
async def test_unexpected_failure_is_safe_500(tmp_path):
    from api.main import create_app

    class ExplodingStore(OperationStore):
        def list_jobs(self, **_kwargs):
            raise RuntimeError("Bearer hidden /home/operator/private?token=hidden")

    app = create_app(
        Settings(tmp_path, admin_api_token="hidden"),
        ExplodingStore(tmp_path / "db"),
        Dispatcher(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer hidden"},
        ) as client,
    ):
        response = await client.get("/api/jobs")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "hidden" not in response.text
    assert "/home/operator" not in response.text


@pytest.mark.anyio
async def test_lifespan_orders_initialize_start_stop_and_propagates_start_failure(
    tmp_path,
):
    from api.main import create_app

    calls = []

    class Store(OperationStore):
        def initialize(self):
            calls.append("initialize")
            super().initialize()

    class OrderedDispatcher(Dispatcher):
        async def start(self):
            calls.append("start")
            await super().start()

        async def stop(self):
            calls.append("stop")
            await super().stop()

    dispatcher = OrderedDispatcher()
    app = create_app(Settings(tmp_path), Store(tmp_path / "db"), dispatcher)
    async with app.router.lifespan_context(app):
        assert calls == ["initialize", "start"]
    assert calls == ["initialize", "start", "stop"]

    failed = Dispatcher(fail_start=True)
    broken = create_app(Settings(tmp_path), Store(tmp_path / "broken"), failed)
    with pytest.raises(RuntimeError):
        async with broken.router.lifespan_context(broken):
            pass
    assert failed.started == 1 and failed.stopped == 0


def test_api_uses_one_lifespan_and_no_raw_background_tasks():
    from api import main

    source = inspect.getsource(main)
    assert ".on_event(" not in source
    assert "asyncio.create_task" not in source


@pytest.mark.anyio
async def test_runtime_construction_failure_happens_before_dispatcher_claims(
    tmp_path, monkeypatch
):
    from api import main
    from api.dispatcher import JobDispatcher

    store = OperationStore(tmp_path / "db")
    dispatcher = JobDispatcher(store, lambda: None)
    started = False

    async def start():
        nonlocal started
        started = True

    monkeypatch.setattr(dispatcher, "start", start)
    monkeypatch.setattr(
        main,
        "GenerationPipelineServices",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("construction failed")
        ),
    )
    app = main.create_app(Settings(tmp_path), store, dispatcher)
    with pytest.raises(RuntimeError, match="construction failed"):
        async with app.router.lifespan_context(app):
            pass
    assert started is False


@pytest.mark.anyio
async def test_lifespan_does_not_require_subtitle_provider_credentials(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from api import main
    from api.dispatcher import JobDispatcher

    store = OperationStore(tmp_path / "db")
    dispatcher = JobDispatcher(store, lambda: None)
    calls = []

    async def start():
        calls.append("start")

    async def stop():
        calls.append("stop")

    monkeypatch.setattr(dispatcher, "start", start)
    monkeypatch.setattr(dispatcher, "stop", stop)
    monkeypatch.setattr(
        main,
        "GenerationPipelineServices",
        lambda *_args, **_kwargs: SimpleNamespace(subtitle_service=None),
    )
    app = main.create_app(Settings(tmp_path), store, dispatcher)
    async with app.router.lifespan_context(app):
        assert calls == ["start"]
    assert calls == ["start", "stop"]


@pytest.mark.anyio
async def test_fake_dispatcher_starts_only_after_interrupted_upload_recovery(tmp_path):
    from api.main import create_app
    from api.subtitles import SubtitleService
    from src.data.opensubtitles import OpenSubtitlesClient, SubtitleCache

    settings = Settings(tmp_path, admin_api_token="token")
    store = OperationStore(tmp_path / "db")
    store.initialize()
    job, _ = store.create_or_get_active_job("", "startup recovery", "startup")
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
    calls = []

    class FakeDispatcher(Dispatcher):
        async def start(self):
            calls.append("start")
            assert store.get_candidate(candidate["id"])["status"] == "rejected"
            assert not generated.exists()

        async def stop(self):
            calls.append("stop")

    app = create_app(settings, store, FakeDispatcher())
    async with app.router.lifespan_context(app):
        assert calls == ["start"]
    assert calls == ["start", "stop"]
