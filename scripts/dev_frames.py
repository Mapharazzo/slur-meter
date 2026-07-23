"""Quick frame preview tool — generates specific frames from existing job data.

Usage:
    uv run python scripts/dev_frames.py --job job_0123456789abcdef --frames 56,57
    uv run python scripts/dev_frames.py --job job_0123456789abcdef --segment intro_hold
    uv run python scripts/dev_frames.py --job job_0123456789abcdef --segment graph --frames 56,57

Or via make:
    make preview JOB=job_0123456789abcdef SEG=intro_hold
    make preview JOB=job_0123456789abcdef SEG=graph FRAMES=56,57
"""

import argparse
import asyncio
import inspect
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent.parent   # scripts/ → project root
sys.path.insert(0, str(BASE_DIR))

from api.database import OperationStore  # noqa: E402
from api.errors import classify_exception, sanitize_text  # noqa: E402
from api.pipeline import GenerationPipelineServices  # noqa: E402
from api.settings import Settings, confined_path, validate_job_id  # noqa: E402
from src.video.compositor import VideoCompositor  # noqa: E402
from src.video.plotter import RagePlotter  # noqa: E402


@dataclass(frozen=True)
class PreviewContext:
    analysis_path: Path
    poster_path: Path | None
    output_dir: Path


def preview_settings(base_dir: str | Path = BASE_DIR) -> Settings:
    """Load preview settings without overriding injected deployment values."""
    return Settings.from_env(base_dir)


def load_preview_context(job_id, settings, store, services) -> PreviewContext:
    """Resolve only current validated artifacts below an opaque run directory."""
    job_id = validate_job_id(job_id)
    if store.get_job(job_id) is None:
        raise ValueError("Unknown job ID")
    detail = store.get_job_detail(job_id)
    analysis_stage = next(
        (
            stage
            for stage in (detail or {}).get("stages", [])
            if stage["name"] == "analysis"
        ),
        None,
    )
    if analysis_stage is None or analysis_stage["state"] != "completed":
        raise RuntimeError("A completed analysis artifact is required")
    manifest = analysis_stage.get("output_manifest") or {}
    validation = services.validate_stage("analysis", job_id, manifest)
    valid = asyncio.run(validation) if inspect.isawaitable(validation) else validation
    if not valid:
        raise RuntimeError("The analysis artifact did not pass validation")
    analysis_path = Path(services.artifacts.artifact_path(manifest)).resolve()
    run_root = confined_path(settings.output_dir, job_id)
    try:
        analysis_path.relative_to(run_root)
    except ValueError:
        raise RuntimeError("The analysis artifact is not confined to its run") from None
    if not analysis_path.is_file() or analysis_path.stat().st_size <= 0:
        raise RuntimeError("The analysis artifact is missing")

    poster_path = None
    metadata_stage = next(
        (
            stage
            for stage in (detail or {}).get("stages", [])
            if stage["name"] == "metadata" and stage["state"] == "completed"
        ),
        None,
    )
    if metadata_stage:
        metadata_manifest = metadata_stage.get("output_manifest") or {}
        validation = services.validate_stage("metadata", job_id, metadata_manifest)
        metadata_valid = (
            asyncio.run(validation) if inspect.isawaitable(validation) else validation
        )
        poster_name = metadata_manifest.get("details", {}).get("poster_file")
        if metadata_valid and poster_name:
            bundle = Path(services.artifacts.artifact_path(metadata_manifest))
            try:
                bundle.resolve().relative_to(run_root)
            except ValueError:
                raise RuntimeError(
                    "The metadata artifact is not confined to its run"
                ) from None
            candidate = confined_path(bundle, poster_name)
            if candidate.is_file() and candidate.stat().st_size > 0:
                poster_path = candidate

    output_dir = confined_path(settings.output_dir, job_id, "dev_frames")
    return PreviewContext(analysis_path, poster_path, output_dir)


def run(
    job_id: str,
    frame_indices: list[int],
    segment: str,
    *,
    settings: Settings | None = None,
    store: OperationStore | None = None,
    services=None,
    stdout=print,
):
    settings = settings or preview_settings(BASE_DIR)
    store = store or OperationStore(settings.data_dir / "slur_meter.db")
    store.initialize()
    cfg = yaml.safe_load((settings.base_dir / "config.yaml").read_text(encoding="utf-8"))
    services = services or GenerationPipelineServices(store, settings, config=cfg)
    context = load_preview_context(job_id, settings, store, services)

    analysis   = json.loads(context.analysis_path.read_text())
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

    poster_path = context.poster_path
    if poster_path is None:
        stdout("No poster found — using blank")
    else:
        stdout(f"Poster: {_display_path(poster_path, settings)}")

    out_dir = context.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    graph_dir = out_dir / "graph"

    total_frames = 450

    runtime_str = metadata.get("runtime", "")
    runtime_min = None
    if runtime_str:
        m = re.search(r"(\d+)", runtime_str)
        if m:
            runtime_min = float(m.group(1))

    day_number  = len(store.list_completed_jobs())
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
        stdout(f"Rendering intro_hold, previewing frames {sample}…")
        hold_frames = compositor.render_intro_hold(title, poster_path, day_number)
        for i in sample:
            out_path = out_dir / f"intro_hold_{i:05d}.png"
            Image.fromarray(hold_frames[i]).save(out_path)
            stdout(f"  Saved: {_display_path(out_path, settings)}")

    # ── Intro transition ────────────────────────────────────────────────────────
    if segment == "intro_transition" or run_all:
        fps  = cfg["video"]["fps"]
        n    = int(2.0 * fps)
        if frame_indices:
            sample = [i for i in frame_indices if 0 <= i < n]
        else:
            step   = max(1, n // 6)
            sample = list(range(0, n, step))
        stdout("Generating plotter frame 0 for transition fade-in…")
        pf_list = plotter.generate_specific_frames(
            binned, graph_dir, frame_indices=[0],
            total_frames=total_frames, runtime_min=runtime_min,
        )
        stdout(f"Rendering intro_transition, previewing frames {sample}…")
        trans_frames = compositor.render_intro_transition(poster_path, poster_area, pf_list)
        for i in sample:
            t  = i / max(n - 1, 1)
            te = t * t * (3 - 2 * t)
            out_path = out_dir / f"intro_transition_{i:05d}.png"
            Image.fromarray(trans_frames[i]).save(out_path)
            stdout(
                f"  frame {i:2d}  t={t:.2f}  te={te:.2f} "
                f"blur={14 * te:.1f}px  saved: {_display_path(out_path, settings)}"
            )

    # ── Graph ──────────────────────────────────────────────────────────────────
    if segment == "graph" or run_all:
        indices = frame_indices if frame_indices else [0, 112, 224, 337, 449]
        stdout(f"Generating graph frames {indices}…")
        pf_list = plotter.generate_specific_frames(
            binned, graph_dir, frame_indices=indices, total_frames=total_frames,
            runtime_min=runtime_min,
        )
        for idx, frame_path in zip(indices, pf_list, strict=True):
            frames = compositor.render_graph_segment(
                [frame_path], poster_area, poster_path, duration=1 / cfg["video"]["fps"]
            )
            out_path = out_dir / f"graph_{idx:05d}.png"
            stdout(f"  frame {idx}: progress={idx / (total_frames - 1) * 100:.0f}%")
            Image.fromarray(frames[0]).save(out_path)
            stdout(f"  Saved: {_display_path(out_path, settings)}")

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
        stdout(f"Rendering verdict ({n} frames), previewing {sample}…")
        verdict_frames = compositor.render_verdict(title, summary, poster_area)
        for i in sample:
            out_path = out_dir / f"verdict_{i:05d}.png"
            Image.fromarray(verdict_frames[i]).save(out_path)
            stdout(f"  Saved: {_display_path(out_path, settings)}")

    stdout("Done.")


class _PreviewArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _preview_parser():
    parser = _PreviewArgumentParser()
    parser.add_argument("--job", required=True, help="Strict generated job ID")
    parser.add_argument(
        "--segment",
        default="all",
        choices=["all", "intro_hold", "intro_transition", "graph", "verdict"],
        help="Which segment to preview (default: all)",
    )
    parser.add_argument(
        "--frames",
        default="",
        help="Comma-separated frame indices (empty = auto sample)",
    )
    return parser


def _display_path(path, settings):
    try:
        return Path(path).resolve().relative_to(settings.base_dir).as_posix()
    except ValueError:
        return Path(path).name


def _preview_error(line):
    print(line, file=sys.stderr)


def run_preview_cli(
    argv=None,
    *,
    base_dir=BASE_DIR,
    settings_factory=preview_settings,
    store_factory=lambda settings: OperationStore(settings.data_dir / "slur_meter.db"),
    services_factory=lambda store, settings, config: GenerationPipelineServices(
        store, settings, config=config
    ),
    stdout=print,
    stderr=None,
):
    if stderr is None:
        stderr = _preview_error
    settings = None
    try:
        args = _preview_parser().parse_args(argv)
        settings = settings_factory(Path(base_dir).resolve())
        store = store_factory(settings)
        store.initialize()
        config = yaml.safe_load(
            (settings.base_dir / "config.yaml").read_text(encoding="utf-8")
        ) or {}
        services = services_factory(store, settings, config)
        indices = [int(value.strip()) for value in args.frames.split(",") if value.strip()]
        run(
            args.job,
            indices,
            args.segment,
            settings=settings,
            store=store,
            services=services,
            stdout=stdout,
        )
        return 0
    except asyncio.CancelledError:
        stderr("Error: Preview execution was interrupted safely.")
        return 2
    except Exception as exc:
        error = classify_exception(exc, "preview execution", settings)
        stderr(f"Error: {sanitize_text(error.message, settings)}")
        return 2


def main():
    raise SystemExit(run_preview_cli())


if __name__ == "__main__":
    main()
