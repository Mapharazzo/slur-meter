"""Pipeline runner — extracted from api/main.py and fixed to match CLI.

Fixes vs the old inline run_pipeline():
  1. Captures compositor timing and passes it to AudioPipeline
  2. Uses AudioPipeline instead of old build_intro_audio_async
  3. ffmpeg muxes audio (mixed.m4a) into final MP4
  4. Output dir keyed on IMDB ID, not random UUID
  5. Records per-step timing and costs to SQLite
"""

import json
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=True)

from api.database import record_cost, record_step, update_job  # noqa: E402
from src.analysis.engine import ProfanityEngine  # noqa: E402
from src.audio.pipeline import AudioPipeline  # noqa: E402
from src.data.opensubtitles import (  # noqa: E402
    OpenSubtitlesClient,
    SubtitleCache,
    safe_imdb_id,
)
from src.publishing.metadata import generate_metadata  # noqa: E402
from src.video.compositor import VideoCompositor  # noqa: E402
from src.video.plotter import RagePlotter  # noqa: E402


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _step(imdb_id: str, name: str, status: str, message: str = "",
          started_at: str | None = None, finished_at: str | None = None,
          duration_ms: int | None = None, warnings: list[str] | None = None):
    record_step(imdb_id, name, status=status, message=message,
                started_at=started_at, finished_at=finished_at,
                duration_ms=duration_ms, warnings=warnings)


def load_config():
    with open(BASE_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def get_client():
    return OpenSubtitlesClient(
        api_key=os.environ["OPENSUBTITLES_API_KEY"],
        user_agent=os.environ["OPENSUBTITLES_USER_AGENT"],
        jwt=os.environ.get("OPENSUBTITLES_JWT"),
        username=os.environ.get("OPENSUBTITLES_USERNAME"),
        password=os.environ.get("OPENSUBTITLES_PASSWORD"),
    )


async def fetch_movie_info(imdb_id: str, output_dir: Path) -> tuple[dict, "Path | None"]:
    """Fetch movie metadata + poster from TMDB via IMDB ID."""
    import asyncio

    import requests as req

    token = os.environ.get("TMDB_READ_TOKEN", "")
    if not token or not imdb_id:
        return {}, None

    tt_id = safe_imdb_id(imdb_id)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    loop = asyncio.get_event_loop()

    def _get_info():
        try:
            url = f"https://api.themoviedb.org/3/find/{tt_id}"
            r = req.get(url, params={"external_source": "imdb_id"},
                        headers=headers, timeout=10)
            if not r.ok:
                return {}
            results = r.json().get("movie_results", [])
            if not results:
                return {}
            m = results[0]
            mid = m["id"]

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
                "Title": m.get("title", ""),
                "Year": (m.get("release_date") or "")[:4],
                "Director": director,
                "Actors": ", ".join(cast),
                "imdbRating": str(round(m.get("vote_average", 0), 1)),
                "Runtime": runtime,
                "Awards": awards,
                "poster_path": m.get("poster_path", ""),
            }
        except Exception:
            return {}

    info = await loop.run_in_executor(None, _get_info)
    if not info:
        return {}, None

    poster_path = None
    raw_poster = info.get("poster_path", "")
    if raw_poster:
        poster_url = f"https://image.tmdb.org/t/p/w780{raw_poster}"

        def _get_poster():
            try:
                r = req.get(poster_url, timeout=15)
                if r.ok:
                    output_dir.mkdir(parents=True, exist_ok=True)
                    p = output_dir / "poster.jpg"
                    p.write_bytes(r.content)
                    return p
            except Exception:
                return None

        poster_path = await loop.run_in_executor(None, _get_poster)

    return info, poster_path


async def run_pipeline(imdb_id: str, query: str | None = None, imdb_id_input: str | None = None):
    """Execute the full pipeline: fetch -> analyse -> render -> audio -> encode.

    Mirrors the CLI flow in main.py:cmd_render() with step tracking and cost recording.
    """
    tmp_dir = BASE_DIR / "tmp"
    output_dir = BASE_DIR / "output" / imdb_id
    results_dir = BASE_DIR / "results"

    try:
        cfg = load_config()
        client = get_client()
        cache = SubtitleCache(results_dir)

        # ── Step 1: Fetch subtitles ──
        t0 = time.monotonic()
        _step(imdb_id, "fetch", "running", "Searching OpenSubtitles…")
        update_job(imdb_id, status="fetching", progress=10, message="Searching OpenSubtitles…")

        sub_results = []
        srt_path = None

        if imdb_id_input:
            cached = cache.has(imdb_id_input)
            if cached:
                srt_path = cached
            else:
                sub_results = client.search(imdb_id=imdb_id_input, language="en", limit=8)
                if not sub_results:
                    _step(imdb_id, "fetch", "failed", "No subtitles found")
                    update_job(imdb_id, status="failed", error="No subtitles found!")
                    return
                best = sub_results[0]
                update_job(imdb_id, label=f"{best.movie_title} ({best.movie_year})")
                srt_path = client.download(best.file_id, dest_dir=str(tmp_dir))
                srt_path = cache.store(imdb_id_input, srt_path)
        elif query:
            sub_results = client.search(query=query, language="en", limit=8)
            if not sub_results:
                _step(imdb_id, "fetch", "failed", "No subtitles found")
                update_job(imdb_id, status="failed", error="No subtitles found!")
                return
            best = sub_results[0]
            update_job(imdb_id, label=f"{best.movie_title} ({best.movie_year})")
            cached = cache.has(best.imdb_id) if best.imdb_id else None
            if cached:
                srt_path = cached
            else:
                srt_path = client.download(best.file_id, dest_dir=str(tmp_dir))
                if best.imdb_id:
                    srt_path = cache.store(best.imdb_id, srt_path)

        record_cost(imdb_id, "api_opensubtitles", "opensubtitles",
                    amount_usd=0.0, units=1 + len(sub_results),
                    detail={"action": "search+download"})

        dt_fetch = int((time.monotonic() - t0) * 1000)
        _step(imdb_id, "fetch", "done", "Subtitles acquired",
              finished_at=_now(), duration_ms=dt_fetch)

        # ── Step 2: Analyse ──
        t0 = time.monotonic()
        _step(imdb_id, "analyse", "running", "Scanning for profanity…")
        update_job(imdb_id, status="analysing", progress=35, message="Scanning for profanity…")

        engine = ProfanityEngine(cfg)
        analysis = engine.analyse_srt(srt_path)

        job = update_job(imdb_id, progress=35)
        label = job["label"] if job else imdb_id

        metadata = generate_metadata(label, analysis.get("summary", {}))
        analysis["metadata"] = {
            "movie_title": label,
            "imdb_id": imdb_id,
            "metadata_tags": metadata,
        }

        # Save analysis JSON
        results_dir.mkdir(parents=True, exist_ok=True)
        analysis_file = results_dir / f"{imdb_id}.json"
        with open(analysis_file, "w") as f:
            json.dump(analysis, f, indent=2, default=str)

        update_job(imdb_id, progress=55, message="Analysis complete — fetching movie info…")

        # ── Fetch poster + movie info ──
        movie_info, poster_path = await fetch_movie_info(imdb_id, output_dir)

        tmdb_calls = 3 if movie_info else 1  # find + details + credits
        record_cost(imdb_id, "api_tmdb", "tmdb", amount_usd=0.0, units=tmdb_calls,
                    detail={"action": "find+details+credits"})
        if os.environ.get("OMDB_API_KEY"):
            record_cost(imdb_id, "api_omdb", "omdb", amount_usd=0.0, units=1,
                        detail={"action": "awards_lookup"})

        # ── Coverage check: retry subtitle if < 70% of runtime ──
        rt_match = re.search(r"(\d+)", movie_info.get("Runtime", ""))
        runtime_min = float(rt_match.group(1)) if rt_match else None
        warnings = []
        if runtime_min and sub_results:
            binned = analysis.get("binned", [])
            sub_dur = max((b["minute"] for b in binned), default=0.0)
            if sub_dur < runtime_min * 0.70:
                warnings.append(
                    f"Subtitle covers {sub_dur:.0f}/{runtime_min:.0f} min "
                    f"({sub_dur / runtime_min * 100:.0f}%)"
                )
                for candidate in sub_results[1:]:
                    try:
                        new_srt = client.download(candidate.file_id, dest_dir=str(tmp_dir))
                        new_analysis = engine.analyse_srt(new_srt)
                        new_binned = new_analysis.get("binned", [])
                        new_dur = max((b["minute"] for b in new_binned), default=0.0)
                        if new_dur > sub_dur:
                            analysis = new_analysis
                            srt_path = new_srt
                            sub_dur = new_dur
                            analysis["metadata"] = {
                                "movie_title": label,
                                "imdb_id": imdb_id,
                                "metadata_tags": generate_metadata(
                                    label, analysis.get("summary", {})),
                            }
                            if sub_dur >= runtime_min * 0.70:
                                break
                    except Exception:
                        continue

        # Enrich metadata
        analysis["metadata"].update({
            "director": movie_info.get("Director", ""),
            "imdb_rating": movie_info.get("imdbRating", ""),
            "runtime": movie_info.get("Runtime", ""),
            "awards": movie_info.get("Awards", ""),
            "actors": movie_info.get("Actors", ""),
        })
        with open(analysis_file, "w") as f:
            json.dump(analysis, f, indent=2, default=str)

        update_job(imdb_id, analysis_json=analysis, movie_info=movie_info)

        dt_analyse = int((time.monotonic() - t0) * 1000)
        _step(imdb_id, "analyse", "done", "Analysis complete",
              finished_at=_now(), duration_ms=dt_analyse, warnings=warnings or None)

        # ── Step 3: Generate graph frames ──
        t0 = time.monotonic()
        _step(imdb_id, "graph", "running", "Generating rage graph frames…")
        update_job(imdb_id, status="rendering", progress=60,
                   message="Drawing rage graph…")

        plotter = RagePlotter(cfg)
        frames_dir = output_dir / "graph_frames"
        runtime_str = movie_info.get("Runtime", "")
        plot_runtime = None
        if runtime_str:
            m = re.search(r"(\d+)", runtime_str)
            if m:
                plot_runtime = float(m.group(1))
        frames = plotter.generate_frames(
            analysis.get("binned", []),
            frames_dir,
            n_frames=450,
            runtime_min=plot_runtime,
        )

        dt_graph = int((time.monotonic() - t0) * 1000)
        _step(imdb_id, "graph", "done", f"{len(frames)} frames generated",
              finished_at=_now(), duration_ms=dt_graph)

        # ── Step 4: Composite video segments ──
        t0 = time.monotonic()
        _step(imdb_id, "composite", "running", "Compositing video segments…")
        update_job(imdb_id, progress=75, message="Compositing video…")

        title = label.split("(")[0].strip()
        year = ""
        if "(" in label and ")" in label:
            year = label.split("(")[1].rstrip(")")

        day_number = len(list(results_dir.glob("*.json")))

        compositor = VideoCompositor(cfg)
        render_result = compositor.render_all(
            output_dir=output_dir / "render",
            title=title,
            year=year,
            plotter_frames=frames,
            summary=analysis.get("summary", {}),
            poster_path=poster_path,
            movie_info=movie_info,
            day_number=day_number,
        )
        segment_timing = render_result["timing"]
        total_frames = render_result["total_frames"]

        update_job(imdb_id, segment_timing=segment_timing)

        dt_composite = int((time.monotonic() - t0) * 1000)
        _step(imdb_id, "composite", "done",
              f"{total_frames} frames composited",
              finished_at=_now(), duration_ms=dt_composite)

        # ── Step 5: Audio pipeline ──
        t0 = time.monotonic()
        _step(imdb_id, "audio", "running", "Building audio layers…")
        update_job(imdb_id, progress=85, message="Generating audio…")

        audio_dir = output_dir / "audio"
        summary = analysis.get("summary", {})

        pipeline = AudioPipeline(cfg, audio_dir, segment_timing)
        pipeline.build_layers(title, year, summary)
        pipeline.generate_all()
        mixed_audio = audio_dir / "mixed.m4a"
        pipeline.mix(mixed_audio)

        # Track TTS cost
        for layer in pipeline.timeline.layers:
            if layer.role == "tts" and layer.text:
                record_cost(imdb_id, f"tts_{layer.provider_name}", layer.provider_name,
                            amount_usd=0.0, units=len(layer.text),
                            detail={"layer": layer.name, "chars": len(layer.text)})
            elif layer.role == "music":
                record_cost(imdb_id, f"music_{layer.provider_name}", layer.provider_name,
                            amount_usd=0.0, units=1,
                            detail={"layer": layer.name})

        dt_audio = int((time.monotonic() - t0) * 1000)
        _step(imdb_id, "audio", "done",
              f"{len(pipeline.timeline.layers)} layer(s) mixed",
              finished_at=_now(), duration_ms=dt_audio)

        # ── Step 6: ffmpeg encode ──
        t0 = time.monotonic()
        _step(imdb_id, "encode", "running", "Encoding final video…")
        update_job(imdb_id, progress=90, message="Encoding final video…")

        concat_dir = output_dir / "render" / "concat"

        if total_frames == 0:
            _step(imdb_id, "encode", "failed", "No frames to encode")
            update_job(imdb_id, status="failed", error="No frames to encode!")
            return

        fps = cfg.get("video", {}).get("fps", 30)
        video_path = output_dir / "final.mp4"

        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-i", str(concat_dir / "%05d.png"),
            "-i", str(mixed_audio),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "medium",
            "-c:a", "copy",
            "-shortest",
            str(video_path),
        ]

        if shutil.which("ffmpeg"):
            subprocess.run(
                ffmpeg_cmd, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            _step(imdb_id, "encode", "failed", "ffmpeg not found")
            update_job(imdb_id, status="failed",
                       error="ffmpeg not installed — cannot encode video")
            return

        dt_encode = int((time.monotonic() - t0) * 1000)
        _step(imdb_id, "encode", "done", "Video encoded",
              finished_at=_now(), duration_ms=dt_encode)

        # ── Done ──
        update_job(
            imdb_id,
            status="done",
            progress=100,
            message="Video ready",
            video_path=str(video_path.relative_to(BASE_DIR)),
            analysis_json=analysis,
        )

    except Exception as e:
        update_job(imdb_id, status="failed", error=str(e), message=f"Pipeline error: {e}")
        # Try to record which step failed
        import traceback
        traceback.print_exc()
