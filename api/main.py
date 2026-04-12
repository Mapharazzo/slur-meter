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
    get_aggregate_costs,
    get_alerts,
    get_costs,
    get_job,
    get_platform_stats,
    get_releases,
    get_revenue,
    get_steps,
    init_db,
    list_jobs,
    upsert_job,
    upsert_release,
    upsert_revenue,
)
from api.pipeline import get_client, run_pipeline  # noqa: E402
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


@app.on_event("startup")
def startup():
    init_db()


# ─── Models ────────────────────────────────────────────

class SubmitRequest(BaseModel):
    imdb_id: str | None = None
    query: str | None = None


# ─── Jobs ──────────────────────────────────────────────

@app.post("/api/jobs")
async def submit_job(req: SubmitRequest):
    """Submit a movie for full pipeline processing.

    If imdb_id is provided, the job is keyed on it.
    If only query is given, a subtitle search is done first to resolve the IMDB ID.
    """
    if not req.imdb_id and not req.query:
        raise HTTPException(status_code=400, detail="Provide either imdb_id or query")

    imdb_id = safe_imdb_id(req.imdb_id) if req.imdb_id else ""
    resolved_imdb_id: str | None = req.imdb_id or None  # passed to pipeline as imdb_id_input
    label = req.query or imdb_id

    if not imdb_id and req.query:
        # Resolve query → real IMDB ID via subtitle search
        try:
            client = get_client()
            results = await asyncio.to_thread(client.search, query=req.query, language="en", limit=1)
            if results and results[0].imdb_id:
                best = results[0]
                imdb_id = safe_imdb_id(best.imdb_id)
                resolved_imdb_id = best.imdb_id
                label = f"{best.movie_title} ({best.movie_year})"
        except Exception:
            pass

        if not imdb_id:
            # Fallback if search failed
            import hashlib
            imdb_id = "q_" + hashlib.md5(req.query.encode()).hexdigest()[:10]

    existing = get_job(imdb_id)
    if existing and existing.get("status") in ["queued", "fetching", "analysing", "rendering", "encoding"]:
        return existing

    job = upsert_job(imdb_id=imdb_id, label=label, query=req.query or "")

    asyncio.create_task(
        run_pipeline(imdb_id, query=req.query, imdb_id_input=resolved_imdb_id)
    )
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

@app.get("/api/videos/{imdb_id}")
async def serve_video(imdb_id: str):
    video_path = BASE_DIR / "output" / imdb_id / "final.mp4"
    if video_path.exists():
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
    seg_dir = BASE_DIR / "output" / imdb_id / "render" / segment
    if not seg_dir.exists():
        raise HTTPException(status_code=404, detail="Segment not found")
    frame_count = len(list(seg_dir.glob("*.png")))
    job = get_job(imdb_id)
    timing = (job.get("segment_timing") or {}).get(segment, {}) if job else {}
    return {"segment": segment, "frame_count": frame_count, "timing": timing}


@app.get("/api/videos/{imdb_id}/frames/{segment}/{frame_num}")
async def serve_frame(imdb_id: str, segment: str, frame_num: int):
    """Serve an individual PNG frame from a segment."""
    if segment not in VALID_SEGMENTS:
        raise HTTPException(status_code=400, detail=f"Invalid segment: {segment}")
    frame_path = BASE_DIR / "output" / imdb_id / "render" / segment / f"{frame_num:05d}.png"
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

    job = get_job(imdb_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") != "done":
        raise HTTPException(400, "Job must be completed (status=done) before publishing")

    video_path = BASE_DIR / "output" / imdb_id / "final.mp4"
    if not video_path.exists():
        raise HTTPException(404, "Video file not found")

    analysis = job.get("analysis_json") or {}
    summary = analysis.get("summary", {}) if isinstance(analysis, dict) else {}
    from src.publishing.metadata import generate_metadata
    meta = generate_metadata(job["label"], summary)

    upsert_release(imdb_id, platform, status="pending", metadata=meta)
    asyncio.create_task(_do_publish(imdb_id, platform, video_path, meta))
    return {"status": "publishing", "platform": platform, "imdb_id": imdb_id}


async def _do_publish(imdb_id: str, platform: str, video_path: Path, meta: dict):
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
        upsert_release(imdb_id, platform, status="uploaded", platform_id=vid_id, metadata=meta)
    except Exception as exc:
        upsert_release(imdb_id, platform, status="failed", error=str(exc))


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
