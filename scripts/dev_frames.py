"""Quick frame preview tool — generates specific frames from existing job data.

Usage:
    uv run python dev_frames.py
    uv run python dev_frames.py --job cd205689 --frames 56,57
    uv run python dev_frames.py --job cd205689 --segment intro
    uv run python dev_frames.py --job cd205689 --segment intro --frames 0,30,60,90,119
"""

import argparse
import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
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
        sys.exit(f"No analysis found: {analysis_path}")

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

    # ── Intro segment ──────────────────────────────────────────────────────────
    if segment == "intro":
        fps     = cfg["video"]["fps"]
        n_intro = int(4.0 * fps)  # 120 frames at 30 fps

        # Decide which intro frame indices to preview
        if frame_indices:
            preview = [i for i in frame_indices if 0 <= i < n_intro]
        else:
            # 8 evenly spaced samples across the intro
            step    = max(1, n_intro // 8)
            preview = list(range(0, n_intro, step))

        # Compute which plotter frames are needed for those intro frames
        def _plotter_idx(intro_i):
            t = intro_i / max(n_intro - 1, 1)
            return min(int(t * total_frames * 0.8), total_frames - 1)

        needed_set    = sorted(set(_plotter_idx(i) for i in range(n_intro)))
        print(f"Generating {len(needed_set)} plotter frames for intro background…")
        pf_list       = plotter.generate_specific_frames(
            binned, graph_dir, frame_indices=needed_set,
            total_frames=total_frames, runtime_min=runtime_min,
        )
        pf_map = {idx: p for idx, p in zip(needed_set, pf_list)}

        # Build per-intro-frame plotter path list (nearest available)
        intro_pf = []
        for i in range(n_intro):
            fi = _plotter_idx(i)
            nearest = min(pf_map.keys(), key=lambda k: abs(k - fi))
            intro_pf.append(pf_map[nearest])

        print(f"Rendering {len(preview)} intro preview frames {preview}…")
        intro_frames = compositor.render_intro(
            title, year, poster_area,
            plotter_frames=intro_pf,
            poster_path=poster_path,
            movie_info=movie_info,
            duration=4.0,
        )
        for i in preview:
            out_path = out_dir / f"intro_{i:05d}.png"
            t = i / max(n_intro - 1, 1)
            print(f"  intro frame {i:3d}: graph progress={t * 0.8 * 100:.0f}%")
            Image.fromarray(intro_frames[i]).save(out_path)
            print(f"  Saved: {out_path}")

    # ── Graph segment (original behaviour) ─────────────────────────────────────
    else:
        indices = frame_indices if frame_indices else [56, 57]
        print(f"Generating {len(indices)} frames {indices} (as if from {total_frames} total)…")
        plotter_frames = plotter.generate_specific_frames(
            binned, graph_dir, frame_indices=indices, total_frames=total_frames,
            runtime_min=runtime_min,
        )
        for idx, frame_path in zip(indices, plotter_frames):
            frames = compositor.render_graph_segment(
                [frame_path], poster_area, poster_path, duration=1 / cfg["video"]["fps"]
            )
            out_path = out_dir / f"preview_{idx:05d}.png"
            runtime  = idx / 449 * 88
            print(f"  frame {idx}: progress={idx/449*100:.1f}%, cutoff={runtime:.1f}min")
            Image.fromarray(frames[0]).save(out_path)
            print(f"  Saved: {out_path}")

    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job",     default="cd205689")
    parser.add_argument("--segment", default="graph", choices=["graph", "intro"],
                        help="Which segment to preview")
    parser.add_argument("--frames",  default="",
                        help="Comma-separated frame indices (empty = auto sample)")
    args    = parser.parse_args()
    indices = [int(x.strip()) for x in args.frames.split(",") if x.strip()]
    run(args.job, indices, args.segment)
