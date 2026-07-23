"""Authenticated operational API; construction is explicit and startup is lifespan-only."""
# ruff: noqa: B008, E701, SIM102

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match

from api.auth import authorized
from api.database import DB_PATH, OperationStore
from api.dispatcher import JobDispatcher
from api.errors import OperationalError, error_payload, sanitize_text
from api.pipeline import GenerationPipelineServices, PipelineRunner, PipelineServices
from api.schemas import (
    ActionRequest,
    ActionResponse,
    AlertPageResponse,
    AnalysisResponse,
    CostAggregateResponse,
    CostPageResponse,
    DetailResponse,
    EventPageResponse,
    HealthResponse,
    JobPageResponse,
    JobResponse,
    LeaderboardResponse,
    PlatformStatPageResponse,
    PublishResponse,
    ReleasePageResponse,
    RevenuePageResponse,
    SegmentInfoResponse,
    SubmitRequest,
    SummaryResponse,
    UploadResponse,
)
from api.settings import (
    Settings,
    canonical_imdb_id,
    validate_candidate_id,
    validate_job_id,
)
from api.subtitles import generated_upload_path, recover_interrupted_uploads
from src.data.opensubtitles import OpenSubtitlesClient
from src.publishing.metadata import generate_metadata

BASE_DIR = Path(__file__).resolve().parent.parent
_PLATFORMS = frozenset({"youtube", "tiktok", "instagram"})
_CORS_METHODS = frozenset({"GET", "POST", "OPTIONS"})
_CORS_HEADERS = frozenset(
    {"authorization", "content-type", "idempotency-key", "x-request-id"}
)


async def _file_chunks(path: Path, chunk_size: int = 1024 * 1024):
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            yield chunk


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", f"req_{uuid.uuid4().hex}")


def _error(
    request: Request,
    status: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": False,
                "details": details or {},
                "request_id": _request_id(request),
            }
        },
    )


def _allow_origin(response: Response, origin: str, settings: Settings) -> Response:
    if origin in settings.allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Expose-Headers"] = "X-Request-ID"
        response.headers.add_vary_header("Origin")
    return response


def _public_job(
    value: dict[str, Any], settings: Settings | None = None
) -> dict[str, Any]:
    """Defence-in-depth: database DTOs are reduced to safe JSON only."""
    hidden = {
        "artifact_path",
        "lease_owner",
        "lease_expires_at",
        "technical_detail",
        "diagnostics",
        "idempotency_key",
        "content_hash",
    }
    return {
        key: _public_value(item, settings)
        for key, item in value.items()
        if key not in hidden
    }


def _public_value(value: Any, settings: Settings | None = None) -> Any:
    if isinstance(value, dict):
        return _public_job(value, settings)
    if isinstance(value, list):
        return [_public_value(item, settings) for item in value]
    if isinstance(value, str):
        return sanitize_text(value, settings)
    return value


def _strict_job(
    store: OperationStore, value: str, *, aliases: bool = False
) -> dict[str, Any] | None:
    try:
        identifier = validate_job_id(value)
    except ValueError:
        if not aliases:
            raise ValueError("Invalid job ID") from None
        try:
            identifier = canonical_imdb_id(value)
        except ValueError:
            raise ValueError("Invalid job ID") from None
    return store.get_job(identifier)


def create_app(settings: Settings, store: OperationStore, dispatcher: Any) -> FastAPI:
    """Create a fully injected app; no database or worker work happens on import."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.initialize()
        # Construct all runtime dependencies before allowing the dispatcher to claim work.
        if isinstance(dispatcher, JobDispatcher):
            services: PipelineServices = GenerationPipelineServices(store, settings)
            dispatcher.runner_factory = lambda: PipelineRunner(
                store, services, settings=settings
            )
            app.state.pipeline_services = services
        recover_interrupted_uploads(store, settings.results_dir / "subtitle-candidates")
        await dispatcher.start()
        try:
            yield
        finally:
            await dispatcher.stop()

    app = FastAPI(
        title="Daily Slur Meter — Admin API", version="2.0.0", lifespan=lifespan
    )
    app.state.settings, app.state.store, app.state.dispatcher = (
        settings,
        store,
        dispatcher,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=True,
        allow_methods=sorted(_CORS_METHODS),
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "X-Request-ID",
        ],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def correlation_and_auth(request: Request, call_next):
        request.state.request_id = f"req_{uuid.uuid4().hex}"
        public_health = (
            request.method == "GET" and request.url.path == "/api/health"
        )
        origin = request.headers.get("origin", "").strip()
        cors_preflight = (
            request.method == "OPTIONS"
            and bool(origin)
            and bool(
                request.headers.get("access-control-request-method", "").strip()
            )
        )
        if cors_preflight:
            requested_method = request.headers[
                "access-control-request-method"
            ].strip().upper()
            requested_headers = {
                item.strip().lower()
                for item in request.headers.get(
                    "access-control-request-headers", ""
                ).split(",")
                if item.strip()
            }
            if (
                origin not in settings.allowed_origins
                or requested_method not in _CORS_METHODS
                or not requested_headers.issubset(_CORS_HEADERS)
            ):
                response = _error(
                    request,
                    400,
                    "bad_request",
                    "CORS preflight request is not allowed.",
                )
                response.headers["X-Request-ID"] = request.state.request_id
                return _allow_origin(response, origin, settings)
        api_path = request.url.path in {"/api", "/api/"} or request.url.path.startswith(
            "/api/"
        )
        if (
            api_path
            and not public_health
            and not cors_preflight
        ):
            if not authorized(
                request, settings.admin_api_token, settings.allow_local_development_auth
            ):
                response = _error(
                    request,
                    401,
                    "unauthorized",
                    "Valid operator authentication is required.",
                )
                response.headers["X-Request-ID"] = request.state.request_id
                return _allow_origin(response, origin, settings)
        if request.url.path in {"/api", "/api/"}:
            response = _error(request, 404, "not_found", "API route was not found")
            response.headers["X-Request-ID"] = request.state.request_id
            return _allow_origin(response, origin, settings)
        try:
            response = await call_next(request)
        except Exception:
            response = _error(
                request, 500, "internal_error", "The operation could not be completed."
            )
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _exc: RequestValidationError):
        return _error(request, 422, "validation_error", "Request validation failed.")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        code = {
            400: "bad_request",
            401: "unauthorized",
            404: "not_found",
            409: "conflict",
            413: "payload_too_large",
            405: "method_not_allowed",
            422: "validation_error",
        }.get(exc.status_code, "request_error")
        return _error(
            request, exc.status_code, code, sanitize_text(exc.detail, settings)
        )

    @app.exception_handler(OperationalError)
    async def operational_error(request: Request, exc: OperationalError):
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc, _request_id(request)),
        )

    @app.get("/api/health", response_model=HealthResponse)
    async def health() -> dict[str, Any]:
        return {"status": "ok", "dispatcher_ready": True}

    @app.get("/api/operations/summary", response_model=SummaryResponse)
    async def summary() -> dict[str, Any]:
        states = store.job_state_counts()
        return {
            "total": sum(states.values()),
            "states": states,
        }

    @app.post("/api/jobs", status_code=201, response_model=JobResponse)
    async def submit(req: SubmitRequest, request: Request) -> dict[str, Any]:
        imdb = canonical_imdb_id(req.imdb_id) if req.imdb_id else ""
        query = (req.query or "").strip()
        job, created = store.create_or_get_active_job(imdb, query, query or imdb)
        if created:
            dispatcher.wake()
        return _public_job(job, settings)

    @app.get("/api/jobs", response_model=JobPageResponse)
    async def jobs(
        state: str | None = None,
        limit: int = 100,
        offset: int = 0,
        query: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 500 or offset < 0:
            raise HTTPException(422, "Pagination bounds are invalid")
        result = store.list_jobs(state=state, limit=limit, offset=offset, query=query)
        result["items"] = [_public_job(item, settings) for item in result["items"]]
        return result

    @app.get("/api/jobs/{job_id}", response_model=DetailResponse)
    async def detail(job_id: str) -> dict[str, Any]:
        try:
            job = _strict_job(store, job_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        if job is None:
            raise HTTPException(404, "Run was not found")
        result = store.get_job_detail(job["id"])
        assert result is not None
        result["last_event_id"] = max(
            (item["id"] for item in result["events"]), default=0
        )
        result["available_actions"] = _available_actions(result)
        return _public_value(result, settings)

    @app.get("/api/jobs/{job_id}/events", response_model=EventPageResponse)
    async def events(job_id: str, after: int = 0) -> dict[str, Any]:
        if after < 0:
            raise HTTPException(422, "after must be non-negative")
        try:
            job = _strict_job(store, job_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        if job is None:
            raise HTTPException(404, "Run was not found")
        snapshot = store.list_events(job["id"], after=after)
        return {
            "items": _public_value(snapshot, settings),
            "last_event_id": max(
                (item["id"] for item in snapshot),
                default=after,
            ),
        }

    @app.post("/api/jobs/{job_id}/actions/cancel", response_model=ActionResponse)
    async def cancel(job_id: str, request: Request) -> dict[str, Any]:
        return _cancel(
            store, dispatcher, settings, job_id, request.headers.get("Idempotency-Key")
        )

    @app.post("/api/jobs/{job_id}/actions/resume", response_model=ActionResponse)
    async def resume(job_id: str, request: Request) -> dict[str, Any]:
        return _transition_action(
            store,
            dispatcher,
            settings,
            job_id,
            "resume",
            "queued",
            request.headers.get("Idempotency-Key"),
        )

    @app.post("/api/jobs/{job_id}/stages/{stage}/retry", response_model=ActionResponse)
    async def retry_stage(job_id: str, stage: str, request: Request) -> dict[str, Any]:
        return _stage_action(
            store,
            dispatcher,
            settings,
            job_id,
            stage,
            "retry_stage",
            request.headers.get("Idempotency-Key"),
        )

    @app.post("/api/jobs/{job_id}/subtitles/rediscover", response_model=ActionResponse)
    async def rediscover(job_id: str, request: Request) -> dict[str, Any]:
        return _stage_action(
            store,
            dispatcher,
            settings,
            job_id,
            "subtitles",
            "rediscover_subtitles",
            request.headers.get("Idempotency-Key"),
        )

    @app.post(
        "/api/jobs/{job_id}/subtitle-candidates/{candidate_id}/select",
        response_model=ActionResponse,
    )
    async def select_candidate(
        job_id: str, candidate_id: str, request: Request
    ) -> dict[str, Any]:
        job = _require_opaque(store, job_id)
        try:
            candidate_id = validate_candidate_id(candidate_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        candidate = store.get_candidate(candidate_id)
        if candidate is None or candidate["job_id"] != job["id"]:
            raise HTTPException(404, "Subtitle candidate was not found")
        return _decision_action(
            store,
            dispatcher,
            settings,
            job_id,
            "select_subtitle",
            request.headers.get("Idempotency-Key"),
            candidate_id=candidate_id,
        )

    @app.post("/api/jobs/{job_id}/subtitles/upload", response_model=UploadResponse)
    async def upload_subtitle(
        job_id: str, request: Request, file: UploadFile = File(...)
    ) -> dict[str, Any]:
        job = _require_opaque(store, job_id)
        key = request.headers.get("Idempotency-Key")
        if key:
            existing = next(
                (
                    row
                    for row in store.list_decisions(job["id"])
                    if row.get("idempotency_key") == key
                ),
                None,
            )
            if existing is not None and existing.get("action") != "upload_subtitle":
                store.reject_idempotency_reuse(job["id"], "upload_subtitle")
                raise HTTPException(409, "Idempotency key conflicts with another action")
            if existing is not None:
                candidate = store.get_candidate(existing["candidate_id"])
                return {
                    "candidate": _public_job(candidate or {}, settings),
                    "decision": _public_job(existing, settings),
                }
        limit = OpenSubtitlesClient.MAX_DOWNLOAD_BYTES
        parts: list[bytes] = []
        total = 0
        while True:
            chunk = await file.read(min(1024 * 1024, limit + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise HTTPException(413, "Uploaded subtitle exceeds the size limit")
            parts.append(chunk)
        if not (file.filename or "").lower().endswith(".srt"):
            raise HTTPException(422, "Uploaded subtitle must be an SRT file")
        service = getattr(
            getattr(app.state, "pipeline_services", None), "subtitle_service", None
        )
        if service is None:
            raise HTTPException(409, "Subtitle upload service is unavailable")
        candidate: dict[str, Any] | None = None
        try:
            candidate = service.upload(
                job["id"], Path(file.filename or "subtitle.srt").name, b"".join(parts)
            )
            if candidate.get("status") == "rejected":
                _discard_upload(store, settings, candidate)
                raise HTTPException(422, "Uploaded subtitle could not be parsed")
            decision = store.finalize_uploaded_candidate(
                job["id"],
                candidate["id"],
                idempotency_key=key,
            )
            if not decision[3]:
                _discard_upload(store, settings, candidate)
                raise HTTPException(409, "Idempotency key conflicts with another action")
        except HTTPException:
            raise
        except ValueError as exc:
            if candidate is not None:
                _discard_upload(store, settings, candidate)
            raise HTTPException(422, "Uploaded subtitle could not be parsed") from exc
        except Exception:
            if candidate is not None:
                _discard_upload(store, settings, candidate)
            raise
        if decision[2]:
            dispatcher.wake()
        return {
            "candidate": _public_job(candidate, settings),
            "decision": _public_job(decision[0], settings),
        }

    @app.post(
        "/api/jobs/{job_id}/publish/{platform}", response_model=PublishResponse
    )
    @app.post(
        "/api/jobs/{job_id}/publish/{platform}/retry", response_model=PublishResponse
    )
    async def publish(
        job_id: str, platform: str, request: Request, _body: ActionRequest | None = None
    ) -> dict[str, Any]:
        if platform not in _PLATFORMS:
            raise HTTPException(422, "Unknown platform")
        if _body and _body.reconciliation:
            job = _require_opaque(store, job_id)
            try:
                release, decision, changed, accepted = store.reconcile_publication_request(
                    job["id"],
                    platform,
                    outcome=_body.reconciliation,
                    remote_id=_body.remote_id,
                    idempotency_key=request.headers.get("Idempotency-Key"),
                )
            except (KeyError, ValueError) as exc:
                raise HTTPException(
                    409, "Publishing reconciliation conflicts with durable state"
                ) from exc
            if not accepted:
                raise HTTPException(
                    409, "Publishing reconciliation conflicts with durable state"
                )
            if changed:
                dispatcher.wake()
            return {
                "run": _public_job(store.get_job(job["id"]) or job, settings),
                "release": _public_job(release, settings),
                "decision": _public_job(decision, settings),
                "changed": changed,
            }
        job = _require_opaque(store, job_id)
        analysis = store.compatibility_analysis(job["id"]) or {}
        summary = analysis.get("summary", {}) if isinstance(analysis, dict) else {}
        metadata = dict(generate_metadata(job["label"], summary))
        if platform == "youtube":
            metadata["privacy_status"] = "private"
        return _decision_action(
            store,
            dispatcher,
            settings,
            job_id,
            "publish",
            request.headers.get("Idempotency-Key"),
            platform=platform,
            metadata=metadata,
        )

    @app.post("/api/jobs/{job_id}/stats/refresh", response_model=ActionResponse)
    async def refresh(job_id: str, request: Request) -> dict[str, Any]:
        return _decision_action(
            store,
            dispatcher,
            settings,
            job_id,
            "refresh_stats",
            request.headers.get("Idempotency-Key"),
        )

    @app.get("/api/jobs/{job_id}/costs", response_model=CostPageResponse)
    async def job_costs(job_id: str) -> dict[str, Any]:
        job = _require_opaque(store, job_id)
        items = _public_value(store.list_costs(job["id"]), settings)
        return {"items": items, "total": len(items)}

    @app.get("/api/costs", response_model=list[CostAggregateResponse])
    async def costs(
        start: str | None = None,
        end: str | None = None,
        group_by: str = "category",
    ) -> list[dict[str, Any]]:
        if group_by not in {"category", "day", "week", "month"}:
            raise HTTPException(422, "Invalid cost grouping")
        return _public_value(
            store.aggregate_costs(start=start, end=end, group_by=group_by), settings
        )

    @app.get("/api/releases", response_model=ReleasePageResponse)
    async def releases() -> dict[str, Any]:
        items = _public_value(store.list_releases(), settings)
        return {"items": items, "total": len(items)}

    @app.get("/api/releases/{identifier}", response_model=ReleasePageResponse)
    async def job_releases(identifier: str) -> dict[str, Any]:
        job = _require_alias(store, identifier)
        items = _public_value(store.list_releases(job["id"]), settings)
        return {"items": items, "total": len(items)}

    @app.get(
        "/api/jobs/{identifier}/platform-stats",
        response_model=PlatformStatPageResponse,
    )
    async def platform_stats(identifier: str) -> dict[str, Any]:
        job = _require_alias(store, identifier)
        items = _public_value(store.platform_stats(job["id"]), settings)
        return {"items": items, "total": len(items)}

    @app.get("/api/revenue", response_model=RevenuePageResponse)
    async def revenue(identifier: str | None = None) -> dict[str, Any]:
        job = _require_alias(store, identifier) if identifier else None
        items = _public_value(store.list_revenue(job["id"] if job else None), settings)
        return {"items": items, "total": len(items)}

    @app.get("/api/alerts", response_model=AlertPageResponse)
    async def alerts(limit: int = 50) -> dict[str, Any]:
        if not 1 <= limit <= 200:
            raise HTTPException(422, "Invalid alert limit")
        page = store.list_attention_jobs(limit=limit)
        items = [
            {
                "job_id": row["id"],
                "state": row["state"],
                "message": (row.get("safe_error") or {}).get("message")
                or "Run needs operator attention.",
                "created_at": row["updated_at"],
            }
            for row in page["items"]
            if row["state"] in {"failed", "needs_attention"}
        ]
        return {"items": _public_value(items, settings), "total": page["total"]}

    @app.get("/api/leaderboard", response_model=LeaderboardResponse)
    async def leaderboard() -> dict[str, Any]:
        ranked = []
        for row in store.list_completed_jobs():
            summary = (
                row.get("artifact_summary", {}).get("analysis", {}).get("summary", {})
            )
            if not summary:
                continue
            stats = store.platform_stats(row["id"])
            ranked.append(
                {
                    "job_id": row["id"],
                    "source_imdb_id": row["source_imdb_id"],
                    "label": row["label"],
                    "hard": int(summary.get("total_hard", 0)),
                    "soft": int(summary.get("total_soft", 0)),
                    "f_bombs": int(summary.get("total_f_bombs", 0)),
                    "total_views": sum(int(item.get("views", 0)) for item in stats),
                }
            )
        ranked.sort(key=lambda row: (row["hard"], row["f_bombs"]), reverse=True)
        return {"items": _public_value(ranked, settings), "total": len(ranked)}

    @app.get("/api/analysis/{identifier}", response_model=AnalysisResponse)
    async def analysis(identifier: str) -> dict[str, Any]:
        job = _require_alias(store, identifier)
        payload = store.compatibility_analysis(job["id"])
        if payload is None:
            raise HTTPException(404, "Analysis was not found")
        return _public_value(
            {
                "events": payload.get("events", []),
                "binned": payload.get("binned", []),
                "summary": payload.get("summary", {}),
            },
            settings,
        )

    def current_artifact(identifier: str, stage_name: str):
        job = _require_alias(store, identifier)
        detail = store.get_job_detail(job["id"])
        services = getattr(app.state, "pipeline_services", None)
        artifacts = getattr(services, "artifacts", None)
        if detail is None or artifacts is None:
            raise HTTPException(404, "Artifact was not found")
        stage = next(
            (row for row in detail["stages"] if row["name"] == stage_name), None
        )
        if stage is None or not stage.get("output_manifest"):
            raise HTTPException(404, "Artifact was not found")
        try:
            path = artifacts.artifact_path(stage["output_manifest"])
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise HTTPException(404, "Artifact was not found") from exc
        return job, stage["output_manifest"], path

    @app.get("/api/videos/{identifier}")
    async def video(identifier: str):
        job, _manifest, path = current_artifact(identifier, "encode")
        if not path.is_file():
            raise HTTPException(404, "Video was not found")
        return StreamingResponse(
            _file_chunks(path),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f'attachment; filename="slur-meter-{job["id"]}.mp4"'
            },
        )

    @app.get("/api/jobs/{identifier}/preview")
    async def preview(identifier: str):
        _job, manifest, directory = current_artifact(identifier, "graph")
        if manifest.get("details", {}).get("preview_file") != "preview.png":
            raise HTTPException(404, "Preview was not found")
        path = directory / "preview.png"
        if not path.is_file():
            raise HTTPException(404, "Preview was not found")
        return Response(
            path.read_bytes(),
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/api/videos/{identifier}/segments/{segment}",
        response_model=SegmentInfoResponse,
    )
    async def segment(identifier: str, segment: str) -> dict[str, Any]:
        if segment not in {"intro_hold", "intro_transition", "graph", "verdict"}:
            raise HTTPException(400, "Invalid segment")
        _job, manifest, directory = current_artifact(identifier, "composite")
        path = directory / segment
        if not path.is_dir():
            raise HTTPException(404, "Segment was not found")
        raw_timing = manifest.get("details", {}).get("timing", {}).get(segment, {})
        timing_fields = {
            key: raw_timing[key]
            for key in (
                "start_frame",
                "end_frame",
                "start_time",
                "end_time",
                "num_frames",
            )
            if key in raw_timing
        }
        return {
            "segment": segment,
            "frame_count": len(list(path.glob("*.png"))),
            "fps": manifest.get("details", {}).get("fps"),
            "timing": timing_fields,
        }

    @app.get("/api/videos/{identifier}/frames/{segment}/{frame_num}")
    async def frame(identifier: str, segment: str, frame_num: int):
        if (
            segment not in {"intro_hold", "intro_transition", "graph", "verdict"}
            or frame_num < 0
        ):
            raise HTTPException(400, "Invalid frame")
        _job, _manifest, directory = current_artifact(identifier, "composite")
        path = directory / segment / f"{frame_num:05d}.png"
        if not path.is_file():
            raise HTTPException(404, "Frame was not found")
        return StreamingResponse(_file_chunks(path), media_type="image/png")

    @app.api_route(
        "/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
    )
    async def unknown_api(request: Request, path: str):
        if any(
            getattr(route, "path", "").startswith("/api/")
            and getattr(route, "path", "") != "/api/{path:path}"
            and route.matches(request.scope)[0] is Match.PARTIAL
            for route in app.router.routes
        ):
            return _error(
                request, 405, "method_not_allowed", "Method is not allowed"
            )
        return _error(request, 404, "not_found", "API route was not found")

    dist = BASE_DIR / "webui" / "dist"
    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/{path:path}")
    async def spa(path: str):
        index = dist / "index.html"
        return HTMLResponse(
            index.read_text() if index.exists() else "<h1>Daily Slur Meter</h1>"
        )

    return app


def _available_actions(detail: dict[str, Any]) -> list[str]:
    run = detail["run"]
    state = run.get("state")
    actions: list[str] = []
    if state in {"queued", "running", "needs_attention", "failed"}:
        actions.append("cancel")
    if state in {"cancelled", "failed", "needs_attention"}:
        actions.append("resume")
    for stage in detail.get("stages", []):
        if stage.get("state") in {"failed", "needs_attention", "cancelled"}:
            actions.append(f"retry_stage:{stage['name']}")
    if state in {"queued", "cancelled", "failed", "needs_attention"}:
        actions.append("rediscover_subtitles")
    # The operator is authoritative: any discovered candidate may be selected,
    # including one automatically rejected during acceptance ranking. Manual
    # selection records a threshold override, so a rejected candidate must
    # still offer the select action.
    actions.extend(
        f"select_subtitle:{candidate['id']}"
        for candidate in detail.get("candidates", [])
        if candidate.get("status")
        in {"discovered", "uploaded", "validated", "rejected"}
    )
    releases = {row["platform"]: row for row in detail.get("releases", [])}
    if state == "completed":
        for platform in sorted(_PLATFORMS):
            release = releases.get(platform)
            if release is None:
                actions.append(f"publish:{platform}")
            elif release.get("status") == "failed":
                actions.append(f"retry_publish:{platform}")
            elif release.get("status") == "needs_attention":
                actions.append(f"reconcile_publish:{platform}")
            elif release.get("status") == "uploaded" and release.get("remote_id"):
                actions.append("refresh_stats")
    return list(dict.fromkeys(actions))


def _require_opaque(store: OperationStore, job_id: str) -> dict[str, Any]:
    try:
        job = _strict_job(store, job_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    if job is None:
        raise HTTPException(404, "Run was not found")
    return job


def _require_alias(store: OperationStore, identifier: str) -> dict[str, Any]:
    try:
        job = _strict_job(store, identifier, aliases=True)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    if job is None:
        raise HTTPException(404, "Run was not found")
    return job


def _discard_upload(
    store: OperationStore, settings: Settings, candidate: dict[str, Any]
) -> None:
    try:
        path = generated_upload_path(
            settings.results_dir / "subtitle-candidates",
            candidate["job_id"],
            candidate["id"],
        )
        path.unlink(missing_ok=True)
        path.parent.rmdir()
    except (KeyError, OSError, ValueError):
        pass
    store.discard_candidate(candidate["id"])


def _decision_action(
    store: OperationStore,
    dispatcher: Any,
    settings: Settings,
    job_id: str,
    action: str,
    key: str | None,
    **fields: Any,
) -> dict[str, Any]:
    job = _require_opaque(store, job_id)
    decision, run, changed, accepted = store.apply_admin_action(
        job["id"], action, idempotency_key=key, **fields
    )
    if not accepted:
        raise HTTPException(409, "Action conflicts with current run state")
    if changed:
        dispatcher.wake()
    return {
        "run": _public_job(run, settings),
        "decision": _public_job(decision, settings),
        "changed": changed,
    }


def _cancel(
    store: OperationStore,
    dispatcher: Any,
    settings: Settings,
    job_id: str,
    key: str | None,
) -> dict[str, Any]:
    job = _require_opaque(store, job_id)
    decision, updated, changed, accepted = store.apply_admin_action(
        job["id"], "cancel", idempotency_key=key
    )
    if not accepted:
        raise HTTPException(409, "Action conflicts with current run state")
    if changed:
        dispatcher.wake()
    return {
        "run": _public_job(updated, settings),
        "decision": _public_job(decision, settings),
        "changed": changed,
    }


def _transition_action(
    store: OperationStore,
    dispatcher: Any,
    settings: Settings,
    job_id: str,
    action: str,
    state: str,
    key: str | None,
) -> dict[str, Any]:
    result = _decision_action(store, dispatcher, settings, job_id, action, key)
    return result


def _stage_action(
    store: OperationStore,
    dispatcher: Any,
    settings: Settings,
    job_id: str,
    stage: str,
    action: str,
    key: str | None,
) -> dict[str, Any]:
    return _decision_action(
        store, dispatcher, settings, job_id, action, key, target_stage=stage
    )


def _default_dispatcher(settings: Settings, store: OperationStore) -> JobDispatcher:
    def unavailable() -> PipelineRunner:
        raise RuntimeError(
            "Runtime services are initialized in the application lifespan"
        )

    return JobDispatcher(store, unavailable)


runtime_settings = Settings.from_env(BASE_DIR)
operation_store = OperationStore(runtime_settings.data_dir / DB_PATH.name)
job_dispatcher = _default_dispatcher(runtime_settings, operation_store)
app = create_app(runtime_settings, operation_store, job_dispatcher)

# Kept as thin, non-route compatibility helpers for pre-control-panel callers.
_pipeline_services: PipelineServices | None = None


def pipeline_services_factory() -> PipelineServices:
    return GenerationPipelineServices(operation_store, runtime_settings)


async def startup() -> None:
    global _pipeline_services
    operation_store.initialize()
    _pipeline_services = pipeline_services_factory()
    await job_dispatcher.start()


async def shutdown() -> None:
    global _pipeline_services
    await job_dispatcher.stop()
    _pipeline_services = None


async def submit_job(req: SubmitRequest) -> dict[str, Any]:
    imdb = canonical_imdb_id(req.imdb_id) if req.imdb_id else ""
    query = (req.query or "").strip()
    job, created = operation_store.create_or_get_active_job(imdb, query, query or imdb)
    if created:
        job_dispatcher.wake()
    return job


def _current_artifact(identifier: str, stage_name: str):
    job = operation_store.get_job(identifier)
    if job is None:
        return None
    detail = operation_store.get_job_detail(job["id"])
    artifacts = getattr(_pipeline_services, "artifacts", None)
    if detail is None or artifacts is None:
        return None
    stage = next(
        (item for item in detail["stages"] if item["name"] == stage_name), None
    )
    if stage is None or not stage.get("output_manifest"):
        return None
    try:
        return (
            job,
            stage["output_manifest"],
            artifacts.artifact_path(stage["output_manifest"]),
        )
    except (KeyError, OSError, TypeError, ValueError):
        return None


async def serve_video(identifier: str):
    from fastapi.responses import FileResponse

    current = _current_artifact(identifier, "encode")
    if current is None:
        raise HTTPException(404, "Video not found")
    job, _manifest, path = current
    return FileResponse(
        str(path), media_type="video/mp4", filename=f"slur-meter-{job['id']}.mp4"
    )


async def serve_segment_info(identifier: str, segment: str) -> dict[str, Any]:
    if segment not in {"intro_hold", "intro_transition", "graph", "verdict"}:
        raise HTTPException(400, "Invalid segment")
    current = _current_artifact(identifier, "composite")
    if current is None:
        raise HTTPException(404, "Segment not found")
    _job, manifest, render = current
    directory = render / segment
    if not directory.is_dir():
        raise HTTPException(404, "Segment not found")
    return {
        "segment": segment,
        "frame_count": len(list(directory.glob("*.png"))),
        "timing": manifest.get("details", {}).get("timing", {}).get(segment, {}),
    }


async def serve_frame(identifier: str, segment: str, frame_num: int):
    from fastapi.responses import FileResponse

    if segment not in {"intro_hold", "intro_transition", "graph", "verdict"}:
        raise HTTPException(400, "Invalid segment")
    current = _current_artifact(identifier, "composite")
    if current is None:
        raise HTTPException(404, "Frame not found")
    path = current[2] / segment / f"{frame_num:05d}.png"
    if not path.is_file():
        raise HTTPException(404, "Frame not found")
    return FileResponse(str(path), media_type="image/png")


async def serve_preview_frame(identifier: str):
    current = _current_artifact(identifier, "graph")
    if current is None:
        raise HTTPException(404, "Preview not ready")
    _job, manifest, graph = current
    if manifest.get("details", {}).get("preview_file") != "preview.png":
        raise HTTPException(404, "Preview not ready")
    path = graph / "preview.png"
    if not path.is_file():
        raise HTTPException(404, "Preview not ready")
    return Response(
        content=path.read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


async def _do_publish(
    job_id: str, platform: str, video_path: Path, metadata: dict[str, Any]
) -> None:
    """Removed legacy worker hook; operational routes only record durable work."""
    return None


async def publish_video(identifier: str, platform: str) -> dict[str, Any]:
    if platform not in _PLATFORMS:
        raise HTTPException(400, "Unknown platform")
    job = operation_store.get_job(identifier)
    if job is None:
        raise HTTPException(404, "Job not found")
    current = _current_artifact(identifier, "encode")
    if current is None:
        raise HTTPException(404, "Video not found")
    # This non-route legacy helper is retained for in-process callers only; API routes
    # record durable work and never invoke it.
    operation_store.upsert_release(job["id"], platform, status="pending", metadata={})
    await _do_publish(job["id"], platform, current[2], {})
    decision = operation_store.record_decision(
        job["id"], "publish", platform=platform, accepted=True
    )
    if decision is not None and decision[1]:
        job_dispatcher.wake()
    return {"status": "publishing", "platform": platform, "imdb_id": identifier}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
