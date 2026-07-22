"""FastAPI backend for Daily Slur Meter — Admin Dashboard."""

import asyncio
import json
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ─── Path setup ───────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
load_dotenv(BASE_DIR / ".env", override=True)

from api.database import (  # noqa: E402
    DB_PATH,
    OperationStore,
    get_aggregate_costs,
    get_alerts,
    get_costs,
    get_job,
    get_platform_stats,
    get_releases,
    get_revenue,
    get_steps,
    list_jobs,
    upsert_revenue,
)
from api.dispatcher import JobDispatcher  # noqa: E402
from api.pipeline import (  # noqa: E402
    GenerationPipelineServices,
    PipelineRunner,
    PipelineServices,
)
from api.settings import Settings  # noqa: E402
from src.data.opensubtitles import safe_imdb_id  # noqa: E402

# ─── App ───────────────────────────────────────────────

app = FastAPI(
    title="Daily Slur Meter — Admin API",
    description="Backend for the Slur Meter admin dashboard",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

runtime_settings = Settings.from_env(BASE_DIR)
operation_store = OperationStore(DB_PATH)
_pipeline_services: PipelineServices | None = None


def pipeline_services_factory() -> PipelineServices:
    return GenerationPipelineServices(operation_store, runtime_settings)


def _runner_factory() -> PipelineRunner:
    if _pipeline_services is None:
        raise RuntimeError("Pipeline services were not initialized at startup")
    return PipelineRunner(
        operation_store,
        _pipeline_services,
        settings=runtime_settings,
    )


job_dispatcher = JobDispatcher(operation_store, _runner_factory)


@app.on_event("startup")
async def startup():
    global _pipeline_services
    operation_store.initialize()
    _pipeline_services = pipeline_services_factory()
    await job_dispatcher.start()


@app.on_event("shutdown")
async def shutdown():
    global _pipeline_services
    await job_dispatcher.stop()
    _pipeline_services = None


# ─── Models ────────────────────────────────────────────

class SubmitRequest(BaseModel):
    imdb_id: str | None = None
    query: str | None = None


# ─── Jobs ──────────────────────────────────────────────

@app.post("/api/jobs")
async def submit_job(req: SubmitRequest):
    """Durably enqueue generation work and wake the retained dispatcher."""
    if not req.imdb_id and not req.query:
        raise HTTPException(status_code=400, detail="Provide either imdb_id or query")

    imdb_id = safe_imdb_id(req.imdb_id) if req.imdb_id else ""
    query = (req.query or "").strip()
    label = query or imdb_id
    job, _created = operation_store.create_or_get_active_job(
        imdb_id or "",
        query,
        label,
    )
    job_dispatcher.wake()
    return job


@app.get("/api/jobs")
async def list_all_jobs(
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return list_jobs(limit=limit, offset=offset, status=status)


@app.get("/api/jobs/{imdb_id}")
async def get_single_job(imdb_id: str):
    job = get_job(imdb_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job["steps"] = get_steps(imdb_id)
    return job


@app.get("/api/jobs/{imdb_id}/steps")
async def get_job_steps(imdb_id: str):
    job = get_job(imdb_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return get_steps(imdb_id)


@app.get("/api/jobs/{imdb_id}/costs")
async def get_job_costs(imdb_id: str):
    job = get_job(imdb_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return get_costs(imdb_id)


# ─── Videos & Frames ──────────────────────────────────

def _current_artifact(identifier: str, stage_name: str):
    job = operation_store.get_job(identifier)
    if job is None:
        return None
    detail = operation_store.get_job_detail(job["id"])
    if detail is None:
        return None
    stage = next(
        (item for item in detail["stages"] if item["name"] == stage_name),
        None,
    )
    artifacts = getattr(_pipeline_services, "artifacts", None)
    if stage is None or not stage["output_manifest"] or artifacts is None:
        return None
    try:
        path = artifacts.artifact_path(stage["output_manifest"])
    except (KeyError, OSError, TypeError, ValueError):
        return None
    return job, stage["output_manifest"], path


@app.get("/api/videos/{imdb_id}")
async def serve_video(imdb_id: str):
    current = _current_artifact(imdb_id, "encode")
    if current is not None:
        _job, _manifest, video_path = current
        return FileResponse(
            str(video_path),
            media_type="video/mp4",
            filename=f"slur-meter-{imdb_id}.mp4",
        )
    raise HTTPException(status_code=404, detail="Video not found")


VALID_SEGMENTS = {"intro_hold", "intro_transition", "graph", "verdict"}


@app.get("/api/videos/{imdb_id}/segments/{segment}")
async def serve_segment_info(imdb_id: str, segment: str):
    """Return segment metadata and frame count."""
    if segment not in VALID_SEGMENTS:
        raise HTTPException(status_code=400, detail=f"Invalid segment: {segment}")
    current = _current_artifact(imdb_id, "composite")
    if current is None:
        raise HTTPException(status_code=404, detail="Segment not found")
    _job, manifest, render = current
    seg_dir = render / segment
    if not seg_dir.is_dir():
        raise HTTPException(status_code=404, detail="Segment not found")
    frame_count = len(list(seg_dir.glob("*.png")))
    timing = (manifest.get("details", {}).get("timing", {})).get(segment, {})
    return {"segment": segment, "frame_count": frame_count, "timing": timing}


@app.get("/api/videos/{imdb_id}/frames/{segment}/{frame_num}")
async def serve_frame(imdb_id: str, segment: str, frame_num: int):
    """Serve an individual PNG frame from a segment."""
    if segment not in VALID_SEGMENTS:
        raise HTTPException(status_code=400, detail=f"Invalid segment: {segment}")
    current = _current_artifact(imdb_id, "composite")
    if current is None:
        raise HTTPException(status_code=404, detail=f"Frame {frame_num} not found")
    _job, _manifest, render = current
    frame_path = render / segment / f"{frame_num:05d}.png"
    if not frame_path.exists():
        raise HTTPException(status_code=404, detail=f"Frame {frame_num} not found")
    return FileResponse(str(frame_path), media_type="image/png")


# ─── Costs ─────────────────────────────────────────────

@app.get("/api/jobs/{imdb_id}/preview")
async def serve_preview_frame(imdb_id: str):
    from fastapi.responses import Response
    preview_path = BASE_DIR / "output" / imdb_id / "preview.png"
    if preview_path.exists():
        data = preview_path.read_bytes()
        return Response(content=data, media_type="image/png",
                        headers={"Cache-Control": "no-store"})
    raise HTTPException(status_code=404, detail="Preview not ready")

@app.get("/api/costs")
async def aggregate_costs(
    start: str | None = Query(None),
    end: str | None = Query(None),
    group_by: str = Query("category"),
):
    return get_aggregate_costs(start=start, end=end, group_by=group_by)


# ─── Releases ─────────────────────────────────────────

@app.get("/api/releases")
async def list_releases(imdb_id: str | None = Query(None)):
    return get_releases(imdb_id=imdb_id)


@app.get("/api/releases/{imdb_id}")
async def get_job_releases(imdb_id: str):
    return get_releases(imdb_id=imdb_id)


# ─── Leaderboard ──────────────────────────────────────

@app.get("/api/leaderboard")
async def leaderboard():
    jobs = list_jobs(limit=500, status="done")

    # Batch-fetch all releases so we don't hit the DB per-job
    all_releases = get_releases()
    releases_by_imdb: dict[str, list] = {}
    for r in all_releases:
        releases_by_imdb.setdefault(r["imdb_id"], []).append(r)

    ranked = []
    for j in jobs:
        analysis = j.get("analysis_json")
        if not analysis:
            continue
        summary = analysis.get("summary") if isinstance(analysis, dict) else {}
        if not summary:
            continue

        imdb_id = j["imdb_id"]

        # Build per-platform release info
        platforms: dict[str, dict] = {}
        for rel in releases_by_imdb.get(imdb_id, []):
            platforms[rel["platform"]] = {
                "status": rel["status"],
                "platform_id": rel.get("platform_id"),
                "uploaded_at": rel.get("uploaded_at"),
                "views": 0,
                "likes": 0,
                "comments": 0,
                "shares": 0,
                "revenue_usd": 0.0,
            }

        # Overlay latest revenue stats
        for stat in get_platform_stats(imdb_id):
            p = stat["platform"]
            if p in platforms:
                platforms[p].update({
                    "views": stat.get("views", 0),
                    "likes": stat.get("likes", 0),
                    "comments": stat.get("comments", 0),
                    "shares": stat.get("shares", 0),
                    "revenue_usd": stat.get("revenue_usd", 0.0),
                })

        total_views = sum(p.get("views", 0) for p in platforms.values())

        ranked.append({
            "imdb_id": imdb_id,
            "label": j["label"],
            "rating": summary.get("rating", "N/A"),
            "hard": summary.get("total_hard", 0),
            "soft": summary.get("total_soft", 0),
            "f_bombs": summary.get("total_f_bombs", 0),
            "peak_score": summary.get("peak_score", 0),
            "peak_minute": summary.get("peak_minute", 0),
            "platforms": platforms,
            "total_views": total_views,
        })

    ranked.sort(
        key=lambda j: (j.get("hard", 0), j.get("f_bombs", 0)),
        reverse=True,
    )
    return ranked


# ─── Publishing ───────────────────────────────────────

SUPPORTED_PLATFORMS = {"youtube", "tiktok", "instagram"}


@app.post("/api/jobs/{imdb_id}/publish/{platform}")
async def publish_video(imdb_id: str, platform: str):
    """Trigger upload of a completed job's video to a platform."""
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(400, f"platform must be one of: {', '.join(sorted(SUPPORTED_PLATFORMS))}")

    current = _current_artifact(imdb_id, "encode")
    if current is None:
        job = operation_store.get_job(imdb_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        raise HTTPException(404, "Video file not found")
    job, _manifest, video_path = current
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("state") != "completed":
        raise HTTPException(400, "Job must be completed (status=done) before publishing")

    analysis = job.get("analysis_json") or {}
    summary = analysis.get("summary", {}) if isinstance(analysis, dict) else {}
    from src.publishing.metadata import generate_metadata
    meta = generate_metadata(job["label"], summary)

    operation_store.upsert_release(
        job["id"], platform, status="pending", metadata=meta
    )
    asyncio.create_task(_do_publish(job["id"], platform, video_path, meta))
    return {"status": "publishing", "platform": platform, "imdb_id": imdb_id}


async def _do_publish(job_id: str, platform: str, video_path: Path, meta: dict):
    try:
        if platform == "youtube":
            from src.publishing.youtube import YouTubeClient
            client = YouTubeClient()
            vid_id = await asyncio.to_thread(
                client.upload, video_path,
                meta["video_title"], meta["description"], meta["tags"],
            )
        elif platform == "tiktok":
            from src.publishing.tiktok import TikTokClient
            client = TikTokClient()
            vid_id = await asyncio.to_thread(
                client.upload, video_path,
                meta["video_title"], " ".join(meta["hashtags"]),
            )
        else:  # instagram
            from src.publishing.instagram import InstagramClient
            client = InstagramClient()
            vid_id = await asyncio.to_thread(
                client.upload, video_path,
                meta["video_title"], " ".join(meta["hashtags"]),
            )
        operation_store.upsert_release(
            job_id,
            platform,
            status="uploaded",
            remote_id=vid_id,
            metadata=meta,
        )
    except Exception as exc:
        operation_store.upsert_release(
            job_id,
            platform,
            status="failed",
            safe_error_message=exc,
        )


# ─── Stats Refresh ────────────────────────────────────

@app.post("/api/jobs/{imdb_id}/stats/refresh")
async def refresh_stats(imdb_id: str):
    """Pull latest stats from every platform this video has been published on."""
    job = get_job(imdb_id)
    if not job:
        raise HTTPException(404, "Job not found")

    releases = get_releases(imdb_id=imdb_id)
    published = [r for r in releases if r.get("status") == "uploaded" and r.get("platform_id")]
    if not published:
        raise HTTPException(400, "No uploaded releases found for this job")

    asyncio.create_task(_do_refresh_stats(imdb_id, published))
    return {"status": "refreshing", "platforms": [r["platform"] for r in published]}


async def _do_refresh_stats(imdb_id: str, releases: list[dict]):
    from datetime import date as _date
    today = _date.today().isoformat()

    for release in releases:
        platform = release["platform"]
        platform_id = release["platform_id"]
        try:
            if platform == "youtube":
                from src.publishing.youtube import YouTubeClient
                stats = await asyncio.to_thread(YouTubeClient().get_video_stats, platform_id)
            elif platform == "tiktok":
                from src.publishing.tiktok import TikTokClient
                stats = await asyncio.to_thread(TikTokClient().get_video_stats, platform_id)
            elif platform == "instagram":
                from src.publishing.instagram import InstagramClient
                stats = await asyncio.to_thread(InstagramClient().get_video_stats, platform_id)
            else:
                continue
            upsert_revenue(imdb_id, platform, today, **stats)
        except Exception:
            pass


# ─── Per-job Platform Stats ───────────────────────────

@app.get("/api/jobs/{imdb_id}/platform-stats")
async def job_platform_stats(imdb_id: str):
    """Return latest stats snapshot per platform for a job."""
    job = get_job(imdb_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return get_platform_stats(imdb_id)


# ─── Alerts ────────────────────────────────────────────

@app.get("/api/alerts")
async def alerts(limit: int = Query(50, ge=1, le=200)):
    return get_alerts(limit=limit)


# ─── Revenue (stubbed) ────────────────────────────────

@app.get("/api/revenue")
async def revenue(imdb_id: str | None = Query(None)):
    return get_revenue(imdb_id=imdb_id)


# ─── Analysis JSON ────────────────────────────────────

@app.get("/api/analysis/{imdb_id}")
async def serve_analysis(imdb_id: str):
    json_path = BASE_DIR / "results" / f"{imdb_id}.json"
    if json_path.exists():
        with open(json_path) as f:
            return JSONResponse(json.load(f))
    raise HTTPException(status_code=404, detail="Analysis not found")


# ─── Frontend SPA ─────────────────────────────────────

# Serve React static assets (must be after API routes)
dist_dir = BASE_DIR / "webui" / "dist"
assets_dir = dist_dir / "assets"
if assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")


@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    """Serve the React SPA for all non-API routes (client-side routing)."""
    index = dist_dir / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return HTMLResponse(
        "<h1>Daily Slur Meter</h1>"
        "<p>Run <code>cd webui && npm install && npm run build</code> first.</p>"
    )


# ─── Run ──────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
