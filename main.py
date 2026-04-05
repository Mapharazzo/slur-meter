#!/usr/bin/env python3
"""Daily Slur Meter — CLI Orchestrator

Usage:
  # Fetch subtitles + analyse only (no video)
  python main.py --imdb tt0110912

  # Full pipeline: fetch → analyse → render video
  python main.py --imdb tt0110912 --render

  # Render from existing analysis JSON
  python main.py --render-only results/tt0110912.json

  # Use movie title (less precise than IMDB)
  python main.py --query "The Godfather" --render
"""

import argparse
import json
import os
import subprocess
import sys
import shutil
from pathlib import Path

import yaml
from dotenv import load_dotenv


def load_config(path: Path = Path("config.yaml")) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ══════════════════════════════════════════════
# Command: Fetch & Analyse
# ══════════════════════════════════════════════

def cmd_fetch_and_analyse(config: dict, imdb_id: str | None = None,
                          query: str | None = None) -> str:
    """Phase 1: OpenSubtitles → SRT → JSON analysis."""

    from src.data.opensubtitles import OpenSubtitlesClient, SubtitleCache
    from src.analysis.engine import ProfanityEngine
    import os

    client = OpenSubtitlesClient(
        api_key=os.environ["OPENSUBTITLES_API_KEY"],
        user_agent=os.environ["OPENSUBTITLES_USER_AGENT"],
        jwt=os.environ.get("OPENSUBTITLES_JWT"),
    )
    cache = SubtitleCache()

    movie_title = "Unknown"
    movie_year = ""
    srt_path = None

    if imdb_id:
        # Check cache
        cached = cache.has(imdb_id)
        if cached:
            print(f"📁 Cache hit: {cached}")
            srt_path = cached
            movie_title = imdb_id
        else:
            print(f"🔍 Searching OpenSubtitles for IMDB {imdb_id}...")
            results = client.search(imdb_id=imdb_id, language="en")
            if not results:
                print("❌ No subtitles found!")
                sys.exit(1)
            best = results[0]
            movie_title = best.movie_title
            movie_year = best.movie_year or ""
            print(f"🎬 Found: {movie_title} ({movie_year})")

            srt_path = client.download(best.file_id, dest_dir="tmp")
            srt_path = cache.store(imdb_id, srt_path)
    elif query:
        print(f"🔍 Searching for: {query}")
        results = client.search(query=query, language="en")
        if not results:
            print("❌ No subtitles found!")
            sys.exit(1)
        best = results[0]
        movie_title = best.movie_title
        movie_year = best.movie_year or ""
        print(f"🎬 Found: {movie_title} ({movie_year})")
        srt_path = client.download(best.file_id, dest_dir="tmp")

    # Analyse
    print("📊 Analysing profanity…")
    engine = ProfanityEngine(config)
    analysis = engine.analyse_srt(srt_path)

    # Attach metadata
    analysis["metadata"] = {
        "imdb_id": imdb_id or "",
        "movie_title": movie_title,
        "movie_year": movie_year,
    }

    # Save JSON
    results_dir = Path("results")
    results_dir.mkdir(exist_ok=True)
    out_file = results_dir / f"{imdb_id or 'query'}.json"
    with open(out_file, "w") as f:
        json.dump(analysis, f, indent=2, default=str)

    print(f"✅ Analysis saved → {out_file}")

    # Quick summary
    s = analysis.get("summary", {})
    print(f"\n📈 Quick Stats:")
    print(f"   🎬 {movie_title} ({movie_year})")
    print(f"   🔴 Hard slurs:  {s.get('total_hard', 0)}")
    print(f"   🟡 Soft slurs:  {s.get('total_soft', 0)}")
    print(f"   💣 F-Bombs:     {s.get('total_f_bombs', 0)}")
    print(f"   ⏱️ Runtime:      {s.get('runtime_minutes', 0)} min")
    print(f"   🏆 Peak:         Minute {s.get('peak_minute', 0)} "
          f"(score: {s.get('peak_score', 0)})")
    print(f"   📝 Rating:       {s.get('rating', '?')}")

    return str(out_file)


# ══════════════════════════════════════════════
# Command: Render Video
# ══════════════════════════════════════════════

def cmd_render(config: dict, analysis_json: str):
    """Phase 2 + 3: Plot graph → composite video via ffmpeg."""

    analysis_path = Path(analysis_json)
    with open(analysis_path) as f:
        analysis = json.load(f)

    summary  = analysis.get("summary", {})
    metadata = analysis.get("metadata", {})
    binned   = analysis.get("binned", [])
    title    = metadata.get("movie_title", "Unknown").split("(")[0].strip()
    year     = metadata.get("movie_year", "")

    # Infer job slug from the analysis file path (e.g. fixtures/pulp_fiction/analysis.json)
    job_slug = analysis_path.stem          # "analysis" if nested, else the id
    if job_slug == "analysis":
        job_slug = analysis_path.parent.name   # "pulp_fiction"

    # Look for poster in output/<job_slug>/poster.jpg
    poster_path = Path("output") / job_slug / "poster.jpg"
    if not poster_path.exists():
        poster_path = None

    # Movie info dict for the banner overlay
    movie_info = {
        "Director":   metadata.get("director", ""),
        "imdbRating": metadata.get("imdb_rating", ""),
        "Runtime":    metadata.get("runtime", ""),
        "Awards":     metadata.get("awards", ""),
        "Actors":     metadata.get("actors", ""),
    }

    day_number = len(list(Path("results").glob("*.json"))) if Path("results").exists() else None

    print(f"🎬 Rendering video: {title} ({year})")
    if poster_path:
        print(f"   🖼️  Poster: {poster_path}")

    # Step 1: Generate animated graph frames
    from src.video.plotter import RagePlotter
    plotter = RagePlotter(config)
    frames_dir = Path("output") / job_slug / "graph_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    print("📈 Generating rage graph frames…")
    plotter_frames = plotter.generate_frames(binned, frames_dir)
    print(f"   → {len(plotter_frames)} frames generated")

    # Step 2: Render all video segments as PNGs
    from src.video.compositor import VideoCompositor
    compositor = VideoCompositor(config)
    render_dir = Path("output") / job_slug / "render"
    print("🎞️ Rendering video segments…")
    render_result = compositor.render_all(
        output_dir=render_dir,
        title=title,
        year=year,
        plotter_frames=plotter_frames,
        summary=summary,
        poster_path=poster_path,
        movie_info=movie_info,
        day_number=day_number,
    )
    segment_timing = render_result["timing"]

    # Step 3: Generate & mix audio via the audio pipeline
    from src.audio.pipeline import AudioPipeline
    audio_dir = Path("output") / job_slug / "audio"
    print("🔊 Building audio layers…")
    pipeline = AudioPipeline(config, audio_dir, segment_timing)
    pipeline.build_layers(title, year, summary)
    print(f"   → {len(pipeline.timeline.layers)} audio layer(s)")
    pipeline.generate_all()
    mixed_audio = audio_dir / "mixed.m4a"
    pipeline.mix(mixed_audio)
    print(f"   → Mixed audio: {mixed_audio}")

    # Step 4: Concatenate all PNG frames via ffmpeg into final MP4
    concat_dir = render_dir / "concat"
    total_frames = render_result["total_frames"]
    if total_frames == 0:
        print("❌ No frames to render!")
        return

    fps = config.get("video", {}).get("fps", 30)
    output_mp4 = Path("output") / job_slug / f"{title.replace(' ', '_')}.mp4"

    print(f"🎬 Encoding final video ({total_frames} frames @ {fps}fps)…")
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
        str(output_mp4),
    ]

    if shutil.which("ffmpeg"):
        subprocess.run(ffmpeg_cmd, check=True)
        print(f"✅ Video saved → {output_mp4}")
    else:
        print("⚠️ ffmpeg not found — frames saved to output/render/")
        print("   Install ffmpeg to produce the final MP4")


# ══════════════════════════════════════════════
# Entry Point
# ══════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Daily Slur Meter")
    parser.add_argument("--imdb", type=str, help="IMDB ID (e.g. tt0110912)")
    parser.add_argument("--query", type=str, help="Movie title to search")
    parser.add_argument("--render", action="store_true",
                        help="Fetch + analyse + render video")
    parser.add_argument("--render-only", type=str,
                        help="Render video from existing analysis JSON")
    parser.add_argument("--config", type=str, default="config.yaml",
                        help="Path to config.yaml")
    args = parser.parse_args()

    load_dotenv()
    config = load_config(Path(args.config))

    if args.render_only:
        cmd_render(config, args.render_only)
    elif args.imdb or args.query:
        json_path = cmd_fetch_and_analyse(
            config, imdb_id=args.imdb, query=args.query
        )
        if args.render:
            cmd_render(config, json_path)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
