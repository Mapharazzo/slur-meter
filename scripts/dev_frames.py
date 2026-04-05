"""Quick frame preview tool — generates specific frames from existing job data.

Usage:
    uv run python scripts/dev_frames.py
    uv run python scripts/dev_frames.py --job cd205689 --frames 56,57
    uv run python scripts/dev_frames.py --job cd205689 --segment intro_hold
    uv run python scripts/dev_frames.py --job cd205689 --segment intro_transition --frames 0,15,30,45,59
    uv run python scripts/dev_frames.py --job cd205689 --segment graph --frames 56,57

Or via make:
    make preview JOB=cd205689 SEG=intro_hold
    make preview JOB=cd205689 SEG=intro_transition
    make preview JOB=cd205689 SEG=graph FRAMES=56,57
"""

import argparse
import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent   # scripts/ → project root
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env", override=True)

from src.video.plotter import RagePlotter
from src.video.compositor import VideoCompositor
from PIL import Image
import numpy as np
import yaml


def run(job_id: str, frame_indices: list[int], segment: str):
    cfg = yaml.safe_load(open(BASE_DIR / "config.yaml"))

    analysis_path = BASE_DIR / "results" / f"{job_id}.json"
    if not analysis_path.exists():
        # Fall back to fixtures directory
        analysis_path = BASE_DIR / "fixtures" / job_id / "analysis.json"
    if not analysis_path.exists():
        sys.exit(f"No analysis found for job '{job_id}' in results/ or fixtures/")

    analysis   = json.loads(analysis_path.read_text())
    binned     = analysis.get("binned", [])
    metadata   = analysis.get("metadata", {})
    title      = metadata.get("movie_title", "Unknown").split("(")[0].strip()
    year_raw   = metadata.get("movie_title", "")
    year       = year_raw[year_raw.find("(")+1:year_raw.find(")")] if "(" in year_raw else ""
    movie_info = {
        "Director":   metadata.get("director", ""),
        "imdbRating": metadata.get("imdb_rating", ""),
        "Runtime":    metadata.get("runtime", ""),
        "Awards":     metadata.get("awards", ""),
        "Actors":     metadata.get("actors", ""),
    }

    poster_path = BASE_DIR / "output" / job_id / "poster.jpg"
    if not poster_path.exists():
        poster_path = None
        print("No poster found — using blank")
    else:
        print(f"Poster: {poster_path}")

    out_dir   = BASE_DIR / "output" / job_id / "dev_frames"
    out_dir.mkdir(parents=True, exist_ok=True)
    graph_dir = out_dir / "graph"

    total_frames = 450

    runtime_str = metadata.get("runtime", "")
    runtime_min = None
    if runtime_str:
        m = re.search(r"(\d+)", runtime_str)
        if m:
            runtime_min = float(m.group(1))

    day_number  = len(list((BASE_DIR / "results").glob("*.json")))
    plotter     = RagePlotter(cfg)
    compositor  = VideoCompositor(cfg)
    poster_area = compositor.render_poster_area(title, year, poster_path, movie_info, day_number)

    run_all = (segment == "all")

    # ── Intro hold ─────────────────────────────────────────────────────────────
    if segment == "intro_hold" or run_all:
        fps    = cfg["video"]["fps"]
        n      = int(2.5 * fps)
        sample = frame_indices if frame_indices else [0, n // 4, n // 2, n - 1]
        sample = [i for i in sample if 0 <= i < n]
        print(f"Rendering intro_hold, previewing frames {sample}…")
        hold_frames = compositor.render_intro_hold(title, poster_path, day_number)
        for i in sample:
            out_path = out_dir / f"intro_hold_{i:05d}.png"
            Image.fromarray(hold_frames[i]).save(out_path)
            print(f"  Saved: {out_path}")

    # ── Intro transition ────────────────────────────────────────────────────────
    if segment == "intro_transition" or run_all:
        fps  = cfg["video"]["fps"]
        n    = int(2.0 * fps)
        if frame_indices:
            sample = [i for i in frame_indices if 0 <= i < n]
        else:
            step   = max(1, n // 6)
            sample = list(range(0, n, step))
        print("Generating plotter frame 0 for transition fade-in…")
        pf_list = plotter.generate_specific_frames(
            binned, graph_dir, frame_indices=[0],
            total_frames=total_frames, runtime_min=runtime_min,
        )
        print(f"Rendering intro_transition, previewing frames {sample}…")
        trans_frames = compositor.render_intro_transition(poster_path, poster_area, pf_list)
        for i in sample:
            t  = i / max(n - 1, 1)
            te = t * t * (3 - 2 * t)
            out_path = out_dir / f"intro_transition_{i:05d}.png"
            Image.fromarray(trans_frames[i]).save(out_path)
            print(f"  frame {i:2d}  t={t:.2f}  te={te:.2f}  blur={14*te:.1f}px  saved: {out_path}")

    # ── Graph ──────────────────────────────────────────────────────────────────
    if segment == "graph" or run_all:
        indices = frame_indices if frame_indices else [0, 112, 224, 337, 449]
        print(f"Generating graph frames {indices}…")
        pf_list = plotter.generate_specific_frames(
            binned, graph_dir, frame_indices=indices, total_frames=total_frames,
            runtime_min=runtime_min,
        )
        for idx, frame_path in zip(indices, pf_list):
            frames = compositor.render_graph_segment(
                [frame_path], poster_area, poster_path, duration=1 / cfg["video"]["fps"]
            )
            out_path = out_dir / f"graph_{idx:05d}.png"
            print(f"  frame {idx}: progress={idx / (total_frames - 1) * 100:.0f}%")
            Image.fromarray(frames[0]).save(out_path)
            print(f"  Saved: {out_path}")

    # ── Verdict / outro ────────────────────────────────────────────────────────
    if segment == "verdict" or run_all:
        summary = analysis.get("summary", {})
        fps = cfg["video"]["fps"]
        n   = int(9.0 * fps)
        if frame_indices:
            sample = [i for i in frame_indices if 0 <= i < n]
        else:
            # Sample across the slam animation (first 5s) + hold
            sample = [0, 15, 30, 50, 75, 100, 150, n - 1]
        print(f"Rendering verdict ({n} frames), previewing {sample}…")
        verdict_frames = compositor.render_verdict(title, summary, poster_area)
        for i in sample:
            out_path = out_dir / f"verdict_{i:05d}.png"
            Image.fromarray(verdict_frames[i]).save(out_path)
            print(f"  Saved: {out_path}")

    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job",     default="cd205689")
    parser.add_argument("--segment", default="all",
                        choices=["all", "intro_hold", "intro_transition", "graph", "verdict"],
                        help="Which segment to preview (default: all)")
    parser.add_argument("--frames",  default="",
                        help="Comma-separated frame indices (empty = auto sample)")
    args    = parser.parse_args()
    indices = [int(x.strip()) for x in args.frames.split(",") if x.strip()]
    run(args.job, indices, args.segment)
