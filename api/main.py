"""FastAPI backend for Daily Slur Meter web portal."""

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
import uvicorn

# ─── Path setup ───────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

load_dotenv(BASE_DIR / ".env", override=True)

from src.data.opensubtitles import OpenSubtitlesClient, SubtitleCache
from src.analysis.engine import ProfanityEngine
from src.video.plotter import RagePlotter
from src.video.compositor import VideoCompositor
from src.video.tts import build_intro_audio_async
from src.publishing.metadata import generate_metadata

# ─── Config ────────────────────────────────────────────

def load_config():
    with open(BASE_DIR / "config.yaml") as f:
        return yaml.safe_load(f)

def get_client():
    import os
    return OpenSubtitlesClient(
        api_key=os.environ["OPENSUBTITLES_API_KEY"],
        user_agent=os.environ["OPENSUBTITLES_USER_AGENT"],
        jwt=os.environ.get("OPENSUBTITLES_JWT"),
    )

async def fetch_movie_info(imdb_id: str, output_dir: Path) -> tuple[dict, "Path | None"]:
    """Fetch movie metadata + poster from TMDB via IMDB ID."""
    import asyncio
    import requests as req

    token = os.environ.get("TMDB_READ_TOKEN", "")
    print(f"[TMDB] token present={bool(token)}, imdb_id={imdb_id!r}")
    if not token or not imdb_id:
        print("[TMDB] skipping — missing token or imdb_id")
        return {}, None

    tt_id   = imdb_id if imdb_id.startswith("tt") else f"tt{int(imdb_id):07d}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    loop    = asyncio.get_event_loop()

    def _get_info():
        try:
            url = f"https://api.themoviedb.org/3/find/{tt_id}"
            print(f"[TMDB] GET {url}")
            r = req.get(url, params={"external_source": "imdb_id"},
                        headers=headers, timeout=10)
            print(f"[TMDB] find status={r.status_code}")
            if not r.ok:
                print(f"[TMDB] find error body: {r.text[:200]}")
                return {}
            results = r.json().get("movie_results", [])
            print(f"[TMDB] movie_results count={len(results)}")
            if not results:
                return {}
            m = results[0]
            mid = m["id"]
            print(f"[TMDB] matched: {m.get('title')} (id={mid}), poster_path={m.get('poster_path')}")

            # Fetch full movie details (for runtime) and credits in parallel
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                fut_details = pool.submit(
                    req.get, f"https://api.themoviedb.org/3/movie/{mid}",
                    headers=headers, timeout=10
                )
                fut_credits = pool.submit(
                    req.get, f"https://api.themoviedb.org/3/movie/{mid}/credits",
                    headers=headers, timeout=10
                )
                details_r = fut_details.result()
                credits_r = fut_credits.result()

            details = details_r.json() if details_r.ok else {}
            credits = credits_r.json() if credits_r.ok else {}

            runtime_mins = details.get("runtime")
            runtime = f"{runtime_mins} min" if runtime_mins else ""

            director = next(
                (p["name"] for p in credits.get("crew", []) if p["job"] == "Director"), "")
            cast = [p["name"] for p in credits.get("cast", [])[:3]]

            # Optional awards from OMDb
            awards = ""
            omdb_key = os.environ.get("OMDB_API_KEY", "")
            if omdb_key and tt_id:
                try:
                    omdb_r = req.get("https://www.omdbapi.com/",
                                     params={"i": tt_id, "apikey": omdb_key}, timeout=8)
                    if omdb_r.ok:
                        awards = omdb_r.json().get("Awards", "")
                        if awards == "N/A":
                            awards = ""
                except Exception:
                    pass

            return {
                "Title":       m.get("title", ""),
                "Year":        (m.get("release_date") or "")[:4],
                "Director":    director,
                "Actors":      ", ".join(cast),
                "imdbRating":  str(round(m.get("vote_average", 0), 1)),
                "Runtime":     runtime,
                "Awards":      awards,
                "poster_path": m.get("poster_path", ""),
            }
        except Exception as e:
            print(f"[TMDB] _get_info exception: {e}")
            return {}

    info = await loop.run_in_executor(None, _get_info)
    print(f"[TMDB] info result: {bool(info)}, keys={list(info.keys()) if info else []}")
    if not info:
        return {}, None

    poster_path = None
    raw_poster  = info.get("poster_path", "")
    print(f"[TMDB] raw_poster={raw_poster!r}, output_dir={output_dir}")
    if raw_poster:
        poster_url = f"https://image.tmdb.org/t/p/w780{raw_poster}"
        def _get_poster():
            try:
                print(f"[TMDB] downloading poster from {poster_url}")
                r = req.get(poster_url, timeout=15)
                print(f"[TMDB] poster download status={r.status_code}, size={len(r.content)}")
                if r.ok:
                    output_dir.mkdir(parents=True, exist_ok=True)
                    p = output_dir / "poster.jpg"
                    p.write_bytes(r.content)
                    print(f"[TMDB] poster saved to {p}")
                    return p
            except Exception as e:
                print(f"[TMDB] poster download exception: {e}")
                return None
        poster_path = await loop.run_in_executor(None, _get_poster)

    print(f"[TMDB] final poster_path={poster_path}")
    return info, poster_path

# ─── App ───────────────────────────────────────────────

app = FastAPI(
    title="Daily Slur Meter API",
    description="Backend for the Slur Meter web portal",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── In-memory job store ───────────────────────────────

JOBS: dict[str, dict] = {}

# ─── Models ────────────────────────────────────────────

class SubmitRequest(BaseModel):
    """Submit a movie for analysis."""
    imdb_id: Optional[str] = None
    query: Optional[str] = None

# ─── Endpoints ─────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    """Serve the React SPA."""
    dist = BASE_DIR / "webui" / "dist" / "index.html"
    if dist.exists():
        return dist.read_text()
    return HTMLResponse(
        content="<h1>📉 Daily Slur Meter</h1><p>Run <code>cd webui && npm install && npm run build</code> first.</p>"
    )


# Serve React static assets
app.mount("/assets",
          StaticFiles(directory=str(BASE_DIR / "webui" / "dist" / "assets")),
          name="assets")


@app.get("/api/jobs")
async def list_jobs():
    """List all jobs, newest first."""
    return sorted(
        JOBS.values(),
        key=lambda j: j.get("created_at", ""),
        reverse=True,
    )


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    """Get a single job by ID."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/jobs")
async def submit_job(req: SubmitRequest):
    """Submit a movie for full pipeline processing."""

    if not req.imdb_id and not req.query:
        raise HTTPException(
            status_code=400,
            detail="Provide either imdb_id or query"
        )

    job_id = str(uuid.uuid4())[:8]
    label = req.query or req.imdb_id

    JOBS[job_id] = {
        "id": job_id,
        "label": label,
        "imdb_id": req.imdb_id or "",
        "query": req.query or "",
        "status": "queued",
        "progress": 0,
        "message": "Queued — starting pipeline…",
        "created_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "result": None,
        "video_url": None,
        "error": None,
    }

    # Fire off the pipeline in the background
    import asyncio
    asyncio.create_task(run_pipeline(job_id, req))
    return JOBS[job_id]


@app.get("/api/leaderboard")
async def leaderboard():
    """Rank completed jobs by total slur count."""
    completed = [
        j for j in JOBS.values()
        if j["status"] == "done" and j.get("result", {}).get("summary")
    ]
    ranked = sorted(
        completed,
        key=lambda j: (
            j["result"]["summary"].get("total_hard", 0),
            j["result"]["summary"].get("total_f_bombs", 0),
        ),
        reverse=True,
    )
    return [
        {
            "id": j["id"],
            "label": j["label"],
            "rating": j["result"]["summary"].get("rating", "N/A"),
            "hard": j["result"]["summary"].get("total_hard", 0),
            "soft": j["result"]["summary"].get("total_soft", 0),
            "f_bombs": j["result"]["summary"].get("total_f_bombs", 0),
            "peak_score": j["result"]["summary"].get("peak_score", 0),
            "peak_minute": j["result"]["summary"].get("peak_minute", 0),
        }
        for j in ranked
    ]


# ─── Pipeline Runner (background) ─────────────────────

async def run_pipeline(job_id: str, req: SubmitRequest):
    """Execute the full pipeline: fetch → analyse → render → metadata."""

    job = JOBS[job_id]
    tmp_dir = BASE_DIR / "tmp"
    output_dir = BASE_DIR / "output" / job_id
    results_dir = BASE_DIR / "results"

    try:
        cfg = load_config()
        client = get_client()
        cache = SubtitleCache(results_dir)

        # ── Step 1: Fetch ──
        job["status"] = "fetching"
        job["progress"] = 10
        job["message"] = "Searching OpenSubtitles…"

        sub_results = []   # all candidates for retry
        if req.imdb_id:
            cached = cache.has(req.imdb_id)
            if cached:
                job["message"] = "Using cached subtitle…"
                srt_path = cached
            else:
                sub_results = client.search(imdb_id=req.imdb_id, language="en", limit=8)
                if not sub_results:
                    job["status"] = "failed"
                    job["error"] = "No subtitles found!"
                    return
                best = sub_results[0]
                job["label"] = f"{best.movie_title} ({best.movie_year})"
                srt_path = client.download(best.file_id, dest_dir=str(tmp_dir))
                srt_path = cache.store(req.imdb_id, srt_path)
        else:
            sub_results = client.search(query=req.query, language="en", limit=8)
            if not sub_results:
                job["status"] = "failed"
                job["error"] = "No subtitles found!"
                return
            best = sub_results[0]
            job["label"] = f"{best.movie_title} ({best.movie_year})"
            job["imdb_id"] = best.imdb_id or ""
            cached = cache.has(best.imdb_id) if best.imdb_id else None
            if cached:
                job["message"] = "Using cached subtitle…"
                srt_path = cached
            else:
                srt_path = client.download(best.file_id, dest_dir=str(tmp_dir))
                if best.imdb_id:
                    srt_path = cache.store(best.imdb_id, srt_path)

        # ── Step 2: Analyse ──
        job["status"] = "analysing"
        job["progress"] = 35
        job["message"] = "Scanning for profanity…"

        engine = ProfanityEngine(cfg)
        analysis = engine.analyse_srt(srt_path)
        metadata = generate_metadata(
            job["label"], analysis.get("summary", {})
        )
        analysis["metadata"] = {
            "movie_title": job["label"],
            "imdb_id": job["imdb_id"],
            "metadata_tags": metadata,
        }

        # Save analysis JSON
        results_dir.mkdir(parents=True, exist_ok=True)
        analysis_file = results_dir / f"{job_id}.json"
        with open(analysis_file, "w") as f:
            json.dump(analysis, f, indent=2, default=str)

        job["progress"] = 55
        job["message"] = "Analysis complete — generating graph…"

        # ── Fetch poster + movie info (optional) ──
        movie_info, poster_path = await fetch_movie_info(
            job.get("imdb_id", ""), output_dir
        )

        # ── Coverage check: retry subtitle if it covers < 70% of runtime ──
        import re as _re
        _rt_match = _re.search(r"(\d+)", movie_info.get("Runtime", ""))
        _runtime_min = float(_rt_match.group(1)) if _rt_match else None
        if _runtime_min and sub_results:
            _binned = analysis.get("binned", [])
            _sub_dur = max((b["minute"] for b in _binned), default=0.0)
            if _sub_dur < _runtime_min * 0.70:
                print(f"[coverage] subtitle covers {_sub_dur:.0f} / {_runtime_min:.0f} min "
                      f"({_sub_dur/_runtime_min*100:.0f}%) — trying alternatives")
                for _candidate in sub_results[1:]:
                    job["message"] = (f"Subtitle too short ({_sub_dur:.0f}/{_runtime_min:.0f} min) "
                                      f"— trying alternative…")
                    try:
                        _new_srt = client.download(_candidate.file_id, dest_dir=str(tmp_dir))
                        _new_analysis = engine.analyse_srt(_new_srt)
                        _new_binned = _new_analysis.get("binned", [])
                        _new_dur = max((b["minute"] for b in _new_binned), default=0.0)
                        print(f"[coverage] candidate covers {_new_dur:.0f} min")
                        if _new_dur > _sub_dur:
                            analysis = _new_analysis
                            srt_path = _new_srt
                            _sub_dur = _new_dur
                            analysis["metadata"] = {
                                "movie_title": job["label"],
                                "imdb_id": job["imdb_id"],
                                "metadata_tags": generate_metadata(
                                    job["label"], analysis.get("summary", {})),
                            }
                            if _sub_dur >= _runtime_min * 0.70:
                                print(f"[coverage] good enough at {_sub_dur:.0f} min")
                                break
                    except Exception as _e:
                        print(f"[coverage] retry failed: {_e}")
                        continue

        # Enrich stored metadata with movie info fields
        analysis["metadata"].update({
            "director":   movie_info.get("Director", ""),
            "imdb_rating": movie_info.get("imdbRating", ""),
            "runtime":    movie_info.get("Runtime", ""),
            "awards":     movie_info.get("Awards", ""),
            "actors":     movie_info.get("Actors", ""),
        })
        with open(analysis_file, "w") as f:
            json.dump(analysis, f, indent=2, default=str)

        # ── Step 3: Generate graph frames ──
        job["status"] = "rendering"
        job["progress"] = 60
        job["message"] = "Drawing rage graph…"

        plotter = RagePlotter(cfg)
        frames_dir = output_dir / "graph_frames"
        runtime_str = movie_info.get("Runtime", "")
        runtime_min = None
        if runtime_str:
            import re as _re
            _m = _re.search(r"(\d+)", runtime_str)
            if _m:
                runtime_min = float(_m.group(1))
        frames = plotter.generate_frames(
            analysis.get("binned", []),
            frames_dir,
            n_frames=450,
            runtime_min=runtime_min,
        )

        # ── Step 4: Compose video segments ──
        job["progress"] = 75
        job["message"] = "Compositing video…"

        title = job["label"].split("(")[0].strip()
        year = ""
        if "(" in job["label"] and ")" in job["label"]:
            year = job["label"].split("(")[1].rstrip(")")

        day_number = len(list(results_dir.glob("*.json")))

        compositor = VideoCompositor(cfg)
        compositor.render_all(
            output_dir=output_dir / "render",
            title=title,
            year=year,
            plotter_frames=frames,
            summary=analysis.get("summary", {}),
            poster_path=poster_path,
            movie_info=movie_info,
            day_number=day_number,
        )

        # ── Step 5: TTS ──
        job["progress"] = 85
        job["message"] = "Generating TTS voiceover…"

        audio_dir = output_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        intro_audio = audio_dir / "intro.mp3"
        await build_intro_audio_async(title, year, intro_audio, cfg)

        # ── Step 6: ffmpeg encode ──
        job["progress"] = 90
        job["message"] = "Encoding final video…"

        import shutil
        concat_dir = output_dir / "render" / "concat"
        total_frames = len(list(concat_dir.glob("*.png")))

        if total_frames == 0:
            job["status"] = "failed"
            job["error"] = "No frames to encode!"
            return

        fps = cfg.get("video", {}).get("fps", 30)
        video_path = output_dir / "final.mp4"

        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-i", str(concat_dir / "%05d.png"),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "fast",
            str(video_path),
        ]

        if shutil.which("ffmpeg"):
            import subprocess
            subprocess.run(
                ffmpeg_cmd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            # No ffmpeg — copy the graph frames as a fallback
            job["progress"] = 95
            job["message"] = "⚠️ ffmpeg missing — graph frames saved. Install ffmpeg for MP4."

        # ── Done ──
        job["status"] = "done"
        job["progress"] = 100
        job["message"] = "🎬 Video Ready!"
        job["result"] = {
            "summary": analysis.get("summary", {}),
            "metadata": metadata,
        }
        if video_path.exists():
            job["video_url"] = f"/api/videos/{job_id}"

    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        job["message"] = f"💥 {str(e)}"


@app.get("/api/videos/{job_id}")
async def serve_video(job_id: str):
    """Serve the rendered MP4."""
    video_path = BASE_DIR / "output" / job_id / "final.mp4"
    if video_path.exists():
        return FileResponse(
            str(video_path),
            media_type="video/mp4",
            filename=f"slur-meter-{job_id}.mp4",
        )
    raise HTTPException(
        status_code=404,
        detail="Video not found. Rendering may still be in progress."
    )


@app.get("/api/analysis/{job_id}")
async def serve_analysis(job_id: str):
    """Serve the analysis JSON."""
    from fastapi.responses import JSONResponse
    json_path = BASE_DIR / "results" / f"{job_id}.json"
    if json_path.exists():
        with open(json_path) as f:
            return JSONResponse(json.load(f))
    raise HTTPException(status_code=404, detail="Analysis not found")


# ─── Run ──────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8001,
        reload=True,
    )
