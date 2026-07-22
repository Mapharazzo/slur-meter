"""Injected, durable, resumable pipeline stage orchestration."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import queue
import re
import shutil
import threading
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Protocol

import yaml

from api.artifacts import ArtifactManager
from api.domain import AttemptTrigger, JobState, StageState
from api.errors import (
    AttentionRequired,
    ConfigurationRequired,
    OperationalError,
    classify_exception,
    sanitize_text,
)
from api.retry import RetryContext, RetryPolicy, run_with_attempts, sanitize_value
from api.settings import DEFAULT_RETRY_DELAYS, Settings

GENERATION_STAGES = (
    "input_resolution",
    "subtitle_discovery",
    "metadata",
    "subtitle_selection",
    "analysis",
    "graph",
    "composite",
    "audio",
    "encode",
)


@dataclass(frozen=True)
class StageResult:
    """A validated stage's durable, operator-safe result."""

    output_manifest: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[Any, ...] = ()


class ProgressReporter(Protocol):
    def __call__(
        self, numerator: int, denominator: int, unit: str
    ) -> Awaitable[None]: ...


class PipelineServices(Protocol):
    """Task-4 boundary implemented by injected fakes now and real stages later."""

    def run_stage(
        self,
        stage_name: str,
        job_id: str,
        progress: ProgressReporter,
    ) -> (
        StageResult | Mapping[str, Any] | Awaitable[StageResult | Mapping[str, Any]]
    ): ...

    def validate_stage(
        self,
        stage_name: str,
        output_manifest: Mapping[str, Any],
    ) -> bool | Awaitable[bool]: ...

    def retry_policy(self, stage_name: str) -> RetryPolicy: ...


class UnavailablePipelineServices:
    """Safe lifecycle default until Task 5 injects real stage services."""

    async def run_stage(
        self,
        stage_name: str,
        job_id: str,
        progress: ProgressReporter,
    ) -> StageResult:
        raise ConfigurationRequired(
            "Pipeline stage services are not configured on this worker.",
            code="pipeline_services_unavailable",
            actions=("configure_pipeline_services",),
        )

    async def validate_stage(
        self, stage_name: str, output_manifest: Mapping[str, Any]
    ) -> bool:
        return False

    def retry_policy(self, stage_name: str) -> RetryPolicy:
        return RetryPolicy(max_attempts=1)


class GenerationPipelineServices:
    """Real generation handlers built around validated atomic artifacts."""

    COMPOSITE_CHILDREN = (
        "intro_hold",
        "intro_transition",
        "graph",
        "verdict",
    )

    def __init__(
        self,
        store: Any,
        settings: Settings,
        *,
        config: Mapping[str, Any] | None = None,
        artifacts: ArtifactManager | None = None,
        metadata_client: Any | None = None,
        plotter_factory: Callable[[dict[str, Any]], Any] | None = None,
        compositor_factory: Callable[[dict[str, Any]], Any] | None = None,
        audio_pipeline_factory: Callable[..., Any] | None = None,
        encoder: Any | None = None,
        subtitle_service: Any | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.config = dict(config) if config is not None else self._load_config()
        self._active_warnings: list[str] = []
        self.artifacts = artifacts or ArtifactManager(
            settings.output_dir,
            warning_callback=self._collect_warning,
        )
        if metadata_client is None:
            from src.data.movie_metadata import MovieMetadataClient

            metadata_client = MovieMetadataClient(
                tmdb_token=os.environ.get("TMDB_READ_TOKEN"),
                omdb_api_key=os.environ.get("OMDB_API_KEY"),
            )
        self.metadata_client = metadata_client
        if plotter_factory is None:
            from src.video.plotter import RagePlotter

            plotter_factory = RagePlotter
        if compositor_factory is None:
            from src.video.compositor import VideoCompositor

            compositor_factory = VideoCompositor
        if audio_pipeline_factory is None:
            from src.audio.pipeline import AudioPipeline

            audio_pipeline_factory = AudioPipeline
        self.plotter_factory = plotter_factory
        self.compositor_factory = compositor_factory
        self.audio_pipeline_factory = audio_pipeline_factory
        if encoder is None:
            from src.video.encoder import FFmpegEncoder

            video = self.config.get("video", {})
            encoding = video.get("encoding", {})
            encoder = FFmpegEncoder(
                fps=int(video.get("fps", 30)),
                preset=str(encoding.get("preset", "medium")),
                sanitize=lambda value: sanitize_text(value, settings),
            )
        self.encoder = encoder
        self.subtitle_service = (
            subtitle_service
            if subtitle_service is not None
            else self._default_subtitle_service()
        )

    async def run_stage(
        self,
        stage_name: str,
        job_id: str,
        progress: ProgressReporter,
    ) -> StageResult:
        self._active_warnings = []
        result = await _run_in_worker(
            self._run_stage_sync, stage_name, job_id, progress
        )
        return StageResult(
            output_manifest=result,
            warnings=tuple(self._active_warnings),
        )

    def validate_stage(
        self, stage_name: str, output_manifest: Mapping[str, Any]
    ) -> bool:
        if output_manifest.get("stage") != stage_name:
            return False
        if stage_name == "subtitle_selection":
            return self._selected_candidate(output_manifest.get("job_id")) is not None
        try:
            job_id = str(output_manifest["job_id"])
            return self.artifacts.validate(
                output_manifest,
                input_hashes=self._input_hashes(stage_name, job_id),
                config_hash=self._config_hash(stage_name),
            )
        except (KeyError, TypeError, ValueError):
            return False

    def retry_policy(self, stage_name: str) -> RetryPolicy:
        attempts = 3 if stage_name in {"subtitle_discovery", "metadata"} else 1
        return RetryPolicy(attempts, self.settings.retry_delays)

    def _run_stage_sync(
        self, stage_name: str, job_id: str, progress: ProgressReporter
    ) -> Mapping[str, Any]:
        handlers = {
            "input_resolution": self._input_resolution,
            "subtitle_discovery": self._subtitle_discovery,
            "subtitle_selection": self._subtitle_selection,
            "metadata": self._metadata,
            "analysis": self._analysis,
            "graph": self._graph,
            "composite": self._composite,
            "audio": self._audio,
            "encode": self._encode,
        }
        try:
            handler = handlers[stage_name]
        except KeyError as exc:
            raise ConfigurationRequired(
                "The requested generation stage is not configured.",
                code="unknown_generation_stage",
            ) from exc
        return handler(job_id, progress)

    def _input_resolution(
        self, job_id: str, _progress: ProgressReporter
    ) -> Mapping[str, Any]:
        job = self._job(job_id)
        identity = {
            "job_id": job_id,
            "source_imdb_id": job["source_imdb_id"],
            "query": job["query"],
            "label": job["label"],
        }
        return self.artifacts.write_json(
            job_id,
            "input_resolution",
            "input.json",
            identity,
            input_hashes=self._input_hashes("input_resolution", job_id),
            config_hash=self._config_hash("input_resolution"),
        )

    def _subtitle_discovery(
        self, job_id: str, progress: ProgressReporter
    ) -> Mapping[str, Any]:
        if self.subtitle_service is None:
            selected = self._selected_candidate(job_id)
            if selected is None:
                raise ConfigurationRequired(
                    "Subtitle discovery credentials are not configured.",
                    code="subtitle_provider_not_configured",
                    actions=("configure_subtitle_provider",),
                )
            candidates = [selected]
        else:
            candidates = self.subtitle_service.discover(job_id)
        if candidates:
            self._report(
                job_id, progress, len(candidates), len(candidates), "candidates"
            )
        safe_candidates = [
            {
                key: candidate.get(key)
                for key in ("id", "provider", "provider_id", "rank", "status")
            }
            for candidate in candidates
        ]
        return self.artifacts.write_json(
            job_id,
            "subtitle_discovery",
            "candidates.json",
            safe_candidates,
            input_hashes=self._input_hashes("subtitle_discovery", job_id),
            config_hash=self._config_hash("subtitle_discovery"),
        )

    def _subtitle_selection(
        self, job_id: str, progress: ProgressReporter
    ) -> Mapping[str, Any]:
        candidate = self._selected_candidate(job_id)
        if candidate is None and self.subtitle_service is not None:
            candidate = self.subtitle_service.select(job_id)
        if candidate is None:
            raise AttentionRequired(
                "A validated subtitle candidate must be selected.",
                code="subtitle_selection_required",
                actions=("select_subtitle", "upload_subtitle"),
            )
        # SubtitleService owns its completion transition. Only report here when
        # selection did not already move the stage out of the running state.
        stage = next(
            item
            for item in self.store.get_job_detail(job_id)["stages"]
            if item["name"] == "subtitle_selection"
        )
        if stage["state"] == StageState.RUNNING.value:
            self._report(job_id, progress, 1, 1, "candidates")
        # SubtitleService may already have completed its durable selection stage.
        return {
            "manifest_version": self.artifacts.manifest_version,
            "job_id": job_id,
            "stage": "subtitle_selection",
            "input_hashes": self._input_hashes("subtitle_selection", job_id),
            "config_hash": self._config_hash("subtitle_selection"),
            "artifact": {
                "kind": "selected_subtitle",
                "candidate_id": candidate["id"],
                "sha256": candidate["content_hash"],
            },
            "details": {},
        }

    def _metadata(self, job_id: str, _progress: ProgressReporter) -> Mapping[str, Any]:
        from src.data.movie_metadata import MovieMetadataResult

        job = self._job(job_id)
        if job["source_imdb_id"]:
            result = self.metadata_client.fetch(job["source_imdb_id"])
        else:
            result = MovieMetadataResult(
                configured=False,
                warnings=(
                    "Movie metadata enrichment requires an IMDb ID; continuing without it.",
                ),
            )
        if isinstance(result, Mapping):
            result = MovieMetadataResult(
                configured=True,
                metadata=dict(result),
            )
        self._active_warnings.extend(str(item) for item in result.warnings)
        metadata = dict(result.metadata)
        metadata.setdefault("Title", job["label"].split("(", 1)[0].strip())
        metadata.setdefault("Year", _label_year(job["label"]))
        metadata["configured"] = bool(result.configured)
        staging = self.artifacts.new_staging_directory(job_id, "metadata")
        try:
            _atomic_json_file(staging / "metadata.json", metadata)
            if result.poster_bytes:
                poster = staging / "poster.jpg"
                poster.write_bytes(result.poster_bytes)
                if poster.stat().st_size <= 0:
                    raise ValueError("Movie poster is empty")
            manifest = self.artifacts.promote_directory(
                job_id,
                "metadata",
                staging,
                final_name="bundle",
                input_hashes=self._input_hashes("metadata", job_id),
                config_hash=self._config_hash("metadata"),
                details={
                    "metadata_file": "metadata.json",
                    "poster_file": "poster.jpg" if result.poster_bytes else None,
                },
            )
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        self._legacy_update(job_id, movie_info=metadata)
        return manifest

    def _analysis(self, job_id: str, _progress: ProgressReporter) -> Mapping[str, Any]:
        from src.analysis.engine import ProfanityEngine

        candidate = self._selected_candidate(job_id)
        if candidate is None:
            raise AttentionRequired(
                "A validated subtitle candidate is required for analysis.",
                code="subtitle_selection_required",
                actions=("select_subtitle", "upload_subtitle"),
            )
        subtitle = Path(candidate["artifact_path"])
        if (
            not subtitle.is_file()
            or self.artifacts.hash_file(subtitle) != candidate["content_hash"]
        ):
            raise AttentionRequired(
                "The selected subtitle artifact no longer validates.",
                code="selected_subtitle_invalid",
                actions=("select_subtitle", "retry"),
            )
        analysis = ProfanityEngine(self.config).analyse_srt(subtitle)
        metadata = self._metadata_value(job_id)
        analysis["metadata"] = {
            "movie_title": metadata.get("Title", self._job(job_id)["label"]),
            "movie_year": metadata.get("Year", ""),
            "director": metadata.get("Director", ""),
            "imdb_rating": metadata.get("imdbRating", ""),
            "runtime": metadata.get("Runtime", ""),
            "awards": metadata.get("Awards", ""),
            "actors": metadata.get("Actors", ""),
            "imdb_id": self._job(job_id)["source_imdb_id"] or "",
        }
        manifest = self.artifacts.write_json(
            job_id,
            "analysis",
            "analysis.json",
            analysis,
            input_hashes=self._input_hashes("analysis", job_id),
            config_hash=self._config_hash("analysis"),
        )
        self._legacy_update(job_id, analysis_json=analysis)
        return manifest

    def _graph(self, job_id: str, progress: ProgressReporter) -> Mapping[str, Any]:
        analysis = self._analysis_value(job_id)
        plotter = self.plotter_factory(self.config)
        count = int(self.config.get("video", {}).get("graph_frames", 450))
        if count < 1:
            raise ValueError("Graph frame count must be positive")
        staging = self.artifacts.new_staging_directory(job_id, "graph")

        def report(_message: str, current: int, total: int) -> None:
            self._report(job_id, progress, current, total, "frames")

        try:
            paths = plotter.generate_frames(
                analysis.get("binned", []),
                staging,
                n_frames=count,
                runtime_min=_runtime_minutes(self._metadata_value(job_id)),
                progress_cb=report,
            )
            if len(paths) != count:
                raise ValueError("Plotter did not produce the requested frame count")
            return self.artifacts.promote_frame_directory(
                job_id,
                "graph",
                staging,
                final_name="frames",
                expected_count=count,
                dimensions=(int(plotter.W), int(plotter.H)),
                prefix="frame_",
                input_hashes=self._input_hashes("graph", job_id),
                config_hash=self._config_hash("graph"),
            )
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _composite(self, job_id: str, progress: ProgressReporter) -> Mapping[str, Any]:
        lease_owner = getattr(progress, "lease_owner", None)
        graph_manifest = self._stage_manifest(job_id, "graph")
        graph_dir = self.artifacts.artifact_path(graph_manifest)
        plotter_frames = sorted(graph_dir.glob("frame_*.png"))
        analysis = self._analysis_value(job_id)
        metadata = self._metadata_value(job_id)
        compositor = self.compositor_factory(self.config)
        staging = self.artifacts.new_staging_directory(job_id, "composite")
        children: dict[str, dict[str, Any]] = {}
        totals: dict[str, int] = {}
        current_child: str | None = None
        parent = next(
            stage
            for stage in self.store.get_job_detail(job_id)["stages"]
            if stage["name"] == "composite"
        )
        for index, name in enumerate(self.COMPOSITE_CHILDREN, 1):
            child_name = f"composite.{name}"
            child = self.store.ensure_stage(
                job_id,
                child_name,
                ordinal=parent["ordinal"] * 100 + index,
                parent_name="composite",
            )
            if child["state"] == StageState.PENDING.value:
                child = self.store.transition_stage(
                    job_id,
                    child_name,
                    StageState.QUEUED,
                    lease_owner=lease_owner,
                )
            if child["state"] == StageState.QUEUED.value:
                child = self.store.transition_stage(
                    job_id,
                    child_name,
                    StageState.RUNNING,
                    expected_state=StageState.QUEUED,
                    lease_owner=lease_owner,
                )
            children[name] = child

        def report(name: str, current: int, total: int) -> None:
            nonlocal current_child
            current_child = name
            totals[name] = total
            child_name = f"composite.{name}"
            child = children[name]
            if child["state"] == StageState.RUNNING.value:
                children[name] = self.store.transition_stage(
                    job_id,
                    child_name,
                    StageState.RUNNING,
                    expected_state=StageState.RUNNING,
                    progress_numerator=current,
                    progress_denominator=total,
                    progress_unit="frames",
                    lease_owner=lease_owner,
                )
            completed_before = sum(
                totals.get(previous, 0)
                for previous in self.COMPOSITE_CHILDREN
                if previous != name
                and self.COMPOSITE_CHILDREN.index(previous)
                < self.COMPOSITE_CHILDREN.index(name)
            )
            self._report(
                job_id,
                progress,
                completed_before + current,
                sum(totals.values()),
                "frames",
            )

        try:
            poster_path = None
            metadata_manifest = self._stage_manifest(job_id, "metadata")
            poster_name = metadata_manifest.get("details", {}).get("poster_file")
            if poster_name:
                poster_path = (
                    self.artifacts.artifact_path(metadata_manifest) / poster_name
                )
            result = compositor.render_all(
                staging,
                title=str(metadata.get("Title", self._job(job_id)["label"])),
                year=str(metadata.get("Year", "")),
                plotter_frames=plotter_frames,
                summary=analysis.get("summary", {}),
                poster_path=poster_path,
                movie_info=metadata,
                day_number=None,
                progress_cb=report,
            )
            timing = result["timing"]
            total_frames = int(result["total_frames"])
            dimensions = (int(compositor.width), int(compositor.height))
            self.artifacts.verify_frame_directory(
                staging / "concat",
                expected_count=total_frames,
                dimensions=dimensions,
            )
            for name in self.COMPOSITE_CHILDREN:
                self.artifacts.verify_frame_directory(
                    staging / name,
                    expected_count=int(timing[name]["num_frames"]),
                    dimensions=dimensions,
                )
            manifest = self.artifacts.promote_directory(
                job_id,
                "composite",
                staging,
                final_name="render",
                input_hashes=self._input_hashes("composite", job_id),
                config_hash=self._config_hash("composite"),
                details={
                    "timing": timing,
                    "total_frames": total_frames,
                    "width": dimensions[0],
                    "height": dimensions[1],
                    "fps": int(compositor.fps),
                },
            )
            render_relative = manifest["artifact"]["path"]
            for name in self.COMPOSITE_CHILDREN:
                child_name = f"composite.{name}"
                child = children[name]
                if child["state"] == StageState.RUNNING.value:
                    self.store.transition_stage(
                        job_id,
                        child_name,
                        StageState.COMPLETED,
                        expected_state=StageState.RUNNING,
                        progress_unit="frames",
                        output_manifest={
                            "manifest_version": self.artifacts.manifest_version,
                            "job_id": job_id,
                            "stage": child_name,
                            "artifact": {
                                "kind": "frames",
                                "path": f"{render_relative}/{name}",
                                "frame_count": timing[name]["num_frames"],
                            },
                        },
                        lease_owner=lease_owner,
                    )
            self._legacy_update(job_id, segment_timing=timing)
            return manifest
        except BaseException:
            if current_child is not None:
                child = children[current_child]
                if child["state"] == StageState.RUNNING.value:
                    self.store.transition_stage(
                        job_id,
                        f"composite.{current_child}",
                        StageState.NEEDS_ATTENTION,
                        expected_state=StageState.RUNNING,
                        safe_error_code="composite_child_failed",
                        safe_error_message="Composite child rendering failed.",
                        next_action="retry",
                        lease_owner=lease_owner,
                    )
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _audio(self, job_id: str, progress: ProgressReporter) -> Mapping[str, Any]:
        analysis = self._analysis_value(job_id)
        composite = self._stage_manifest(job_id, "composite")
        timing = composite.get("details", {}).get("timing", {})
        metadata = self._metadata_value(job_id)
        staging = self.artifacts.new_staging_directory(job_id, "audio")
        try:
            pipeline = self.audio_pipeline_factory(
                self.config,
                staging,
                timing,
                warning_callback=self._collect_warning,
            )
            pipeline.build_layers(
                str(metadata.get("Title", self._job(job_id)["label"])),
                str(metadata.get("Year", "")),
                analysis.get("summary", {}),
            )

            def report(current: int, total: int) -> None:
                self._report(job_id, progress, current, max(total, 1), "layers")

            pipeline.generate_all(progress_cb=report)
            mixed = staging / "mixed.m4a"
            pipeline.mix(mixed)
            return self.artifacts.promote_file(
                job_id,
                "audio",
                mixed,
                final_name="mixed.m4a",
                media_kind="audio",
                input_hashes=self._input_hashes("audio", job_id),
                config_hash=self._config_hash("audio"),
                details={"layer_count": len(pipeline.timeline.layers)},
            )
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _encode(self, job_id: str, progress: ProgressReporter) -> Mapping[str, Any]:
        composite = self._stage_manifest(job_id, "composite")
        audio = self._stage_manifest(job_id, "audio")
        frames = self.artifacts.artifact_path(composite) / "concat"
        audio_path = self.artifacts.artifact_path(audio)
        total = int(composite.get("details", {}).get("total_frames", 0))
        if total < 1:
            raise ValueError("Composite artifact has no frames")
        output = self.artifacts.path(job_id, "final.mp4")

        def report(frame: int) -> None:
            self._report(job_id, progress, min(frame, total), total, "frames")

        self.encoder.encode(
            frames,
            audio_path,
            output,
            report,
            lambda: self._cancel_requested(job_id),
        )
        manifest = self.artifacts.record_file(
            job_id,
            "encode",
            output,
            media_kind="video",
            input_hashes=self._input_hashes("encode", job_id),
            config_hash=self._config_hash("encode"),
            details={"frame_count": total},
        )
        self._legacy_update(job_id, video_path=manifest["artifact"]["path"])
        return manifest

    def _input_hashes(self, stage_name: str, job_id: str) -> dict[str, str]:
        job = self._job(job_id)
        if stage_name == "input_resolution":
            return {
                "submission": self.artifacts.fingerprint(
                    {
                        "source_imdb_id": job["source_imdb_id"],
                        "query": job["query"],
                        "label": job["label"],
                    }
                )
            }
        if stage_name == "subtitle_discovery":
            return {"input": self._artifact_hash(job_id, "input_resolution")}
        if stage_name == "subtitle_selection":
            candidates = self.store.list_candidates(job_id)
            return {"candidates": self.artifacts.fingerprint(candidates)}
        if stage_name == "metadata":
            return {
                "identity": self.artifacts.fingerprint(
                    {
                        "source_imdb_id": job["source_imdb_id"],
                        "label": job["label"],
                    }
                )
            }
        if stage_name == "analysis":
            candidate = self._selected_candidate(job_id)
            return {
                "subtitle": str(candidate["content_hash"]) if candidate else "missing",
                "metadata": self._artifact_hash(job_id, "metadata"),
            }
        if stage_name == "graph":
            return {"analysis": self._artifact_hash(job_id, "analysis")}
        if stage_name == "composite":
            return {
                "analysis": self._artifact_hash(job_id, "analysis"),
                "graph": self._artifact_hash(job_id, "graph"),
                "metadata": self._artifact_hash(job_id, "metadata"),
            }
        if stage_name == "audio":
            return {
                "analysis": self._artifact_hash(job_id, "analysis"),
                "composite": self._artifact_hash(job_id, "composite"),
            }
        if stage_name == "encode":
            return {
                "audio": self._artifact_hash(job_id, "audio"),
                "composite": self._artifact_hash(job_id, "composite"),
            }
        raise KeyError(stage_name)

    def _config_hash(self, stage_name: str) -> str:
        config: Any
        if stage_name == "analysis":
            config = self.config.get("categories", {})
        elif stage_name in {"graph", "composite"}:
            config = self.config.get("video", {})
        elif stage_name == "audio":
            config = {
                "audio": self.config.get("audio", {}),
                "fps": self.config.get("video", {}).get("fps", 30),
            }
        elif stage_name == "encode":
            video = self.config.get("video", {})
            config = {
                "fps": video.get("fps", 30),
                "encoding": video.get("encoding", {}),
            }
        elif stage_name == "metadata":
            config = {
                "client": type(self.metadata_client).__name__,
                "tmdb_configured": bool(
                    getattr(self.metadata_client, "tmdb_token", True)
                ),
                "omdb_configured": bool(
                    getattr(self.metadata_client, "omdb_api_key", False)
                ),
            }
        elif stage_name == "subtitle_discovery":
            config = {"language": "en", "limit": 20}
        elif stage_name == "subtitle_selection":
            config = {
                "coverage_threshold": self.settings.subtitle_coverage_threshold,
                "candidate_limit": self.settings.subtitle_candidates_per_cycle,
            }
        else:
            config = {}
        return self.artifacts.fingerprint(config)

    def _stage_manifest(self, job_id: str, stage_name: str) -> Mapping[str, Any]:
        detail = self.store.get_job_detail(job_id)
        if detail is None:
            raise KeyError("Run was not found")
        stage = next(
            (item for item in detail["stages"] if item["name"] == stage_name),
            None,
        )
        if stage is None or not stage["output_manifest"]:
            raise AttentionRequired(
                f"Upstream stage {stage_name} has no validated artifact.",
                code="upstream_artifact_missing",
                actions=("retry",),
            )
        return stage["output_manifest"]

    def _artifact_hash(self, job_id: str, stage_name: str) -> str:
        try:
            manifest = self._stage_manifest(job_id, stage_name)
        except AttentionRequired:
            if stage_name == "input_resolution":
                job = self._job(job_id)
                return self.artifacts.fingerprint(
                    {
                        "source_imdb_id": job["source_imdb_id"],
                        "query": job["query"],
                        "label": job["label"],
                    }
                )
            raise
        return str(manifest.get("artifact", {}).get("sha256", "missing"))

    def _analysis_value(self, job_id: str) -> dict[str, Any]:
        return dict(self.artifacts.load_json(self._stage_manifest(job_id, "analysis")))

    def _metadata_value(self, job_id: str) -> dict[str, Any]:
        manifest = self._stage_manifest(job_id, "metadata")
        root = self.artifacts.artifact_path(manifest)
        return json.loads((root / "metadata.json").read_text(encoding="utf-8"))

    def _selected_candidate(self, job_id: object) -> dict[str, Any] | None:
        if not isinstance(job_id, str):
            return None
        for candidate in self.store.list_candidates(job_id):
            if candidate["status"] != "selected":
                continue
            stored = self.store.get_candidate(candidate["id"], include_internal=True)
            if stored is None or not stored.get("artifact_path"):
                continue
            path = Path(stored["artifact_path"])
            if (
                path.is_file()
                and stored.get("content_hash")
                and self.artifacts.hash_file(path) == stored["content_hash"]
            ):
                return stored
        return None

    def _report(
        self,
        job_id: str,
        progress: ProgressReporter,
        numerator: int,
        denominator: int,
        unit: str,
    ) -> None:
        if self._cancel_requested(job_id):
            raise asyncio.CancelledError("Generation was cancelled")
        progress(int(numerator), int(denominator), unit)

    def _cancel_requested(self, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        return job is None or bool(job["cancel_requested"])

    def _job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job is None:
            raise KeyError("Run was not found")
        return job

    def _legacy_update(self, job_id: str, **fields: Any) -> None:
        updater = getattr(self.store, "compatibility_update_job", None)
        if updater is not None:
            updater(job_id, **fields)

    def _collect_warning(self, message: str) -> None:
        self._active_warnings.append(str(message))

    def _load_config(self) -> dict[str, Any]:
        path = self.settings.base_dir / "config.yaml"
        if not path.is_file():
            raise ConfigurationRequired(
                "Generation configuration file is missing.",
                code="generation_config_missing",
            )
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ConfigurationRequired(
                "Generation configuration file is invalid.",
                code="generation_config_invalid",
            )
        return value

    def _default_subtitle_service(self) -> Any | None:
        api_key = os.environ.get("OPENSUBTITLES_API_KEY")
        user_agent = os.environ.get("OPENSUBTITLES_USER_AGENT")
        if not api_key or not user_agent:
            return None
        from api.subtitles import SubtitleService
        from src.data.opensubtitles import OpenSubtitlesClient, SubtitleCache

        client = OpenSubtitlesClient(
            api_key=api_key,
            user_agent=user_agent,
            jwt=os.environ.get("OPENSUBTITLES_JWT"),
            username=os.environ.get("OPENSUBTITLES_USERNAME"),
            password=os.environ.get("OPENSUBTITLES_PASSWORD"),
        )
        return SubtitleService(
            self.store,
            client,
            SubtitleCache(self.settings.results_dir),
            self.settings,
        )


class PipelineStore(Protocol):
    def get_job(self, job_id: str) -> dict[str, Any] | None: ...

    def get_job_detail(self, job_id: str) -> dict[str, Any] | None: ...

    def ensure_stage(self, job_id: str, name: str, **fields: Any) -> dict[str, Any]: ...

    def transition_stage(
        self, job_id: str, stage_name: str, new_state: object, **fields: Any
    ) -> dict[str, Any] | None: ...

    def transition_job(
        self, job_id: str, new_state: object, **fields: Any
    ) -> dict[str, Any] | None: ...

    def transition_stage_and_job(
        self,
        job_id: str,
        stage_name: str,
        stage_state: object,
        job_state: object,
        **fields: Any,
    ) -> dict[str, Any] | None: ...

    def renew_lease(self, job_id: str, owner: str, *, lease_seconds: float) -> bool: ...

    def record_event(self, job_id: str, **fields: Any) -> object | None: ...


class PipelineRunner:
    """Run the first incomplete stage under an existing durable lease."""

    def __init__(
        self,
        store: PipelineStore,
        services: PipelineServices,
        *,
        stages: Sequence[str] = GENERATION_STAGES,
        lease_seconds: float = 30.0,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        settings: Settings | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("Lease duration must be positive")
        self.store = store
        self.services = services
        self.stages = tuple(stages)
        self.lease_seconds = float(lease_seconds)
        self.sleep = sleep
        self.settings = settings

    async def run(self, job_id: str, lease_owner: str) -> None:
        job = self.store.get_job(job_id)
        if job is None or job["state"] != JobState.RUNNING.value:
            return
        self._ensure_stages(job_id)

        for stage_index, stage_name in enumerate(self.stages):
            if self._cancel_requested(job_id):
                self._apply_cancellation(job_id, lease_owner)
                return
            if not self._renew(job_id, lease_owner):
                return
            stage = self._stage(job_id, stage_name)
            if stage["state"] == StageState.COMPLETED.value:
                try:
                    reusable = await self._validate(
                        stage_name, stage["output_manifest"]
                    )
                except Exception as exc:
                    error = classify_exception(
                        exc, f"{stage_name} validation", self.settings
                    )
                    self._invalid_completed_stage(
                        job_id,
                        stage_name,
                        lease_owner,
                        code=error.code,
                        message=error.message,
                    )
                    return
                if reusable:
                    reused_event = self.store.record_event(
                        job_id,
                        event_type="artifact_reused",
                        message=f"Validated output for stage {stage_name} was reused.",
                        stage_name=stage_name,
                        lease_owner=lease_owner,
                    )
                    if reused_event is None:
                        raise asyncio.CancelledError(
                            "The worker no longer owns the job lease"
                        )
                    continue
                self._invalid_completed_stage(job_id, stage_name, lease_owner)
                return
            if stage["state"] == StageState.PENDING.value:
                stage = self.store.transition_stage(
                    job_id,
                    stage_name,
                    StageState.QUEUED,
                    expected_state=StageState.PENDING,
                    lease_owner=lease_owner,
                )
            if stage is None or stage["state"] != StageState.QUEUED.value:
                return
            restart_recovery = (
                stage["retry_cycle"] > 1
                and (stage.get("safe_error") or {}).get("code") == "restart_recovery"
            )
            running = self.store.transition_stage(
                job_id,
                stage_name,
                StageState.RUNNING,
                expected_state=StageState.QUEUED,
                lease_owner=lease_owner,
            )
            if running is None:
                return

            policy = self._policy(stage_name)
            trigger = (
                AttemptTrigger.RESTART_RECOVERY
                if restart_recovery
                else AttemptTrigger.AUTOMATIC
            )
            try:
                result = await run_with_attempts(
                    partial(self._execute_stage, job_id, stage_name, lease_owner),
                    RetryContext(
                        job_id,
                        stage_name,
                        lease_owner=lease_owner,
                        trigger=trigger,
                        settings=self.settings,
                        cancel_requested=partial(self._cancel_requested, job_id),
                    ),
                    policy,
                    self.store,
                    self.sleep,
                )
            except asyncio.CancelledError:
                if self._cancel_requested(job_id):
                    self._apply_cancellation(job_id, lease_owner)
                    return
                raise
            except OperationalError as error:
                self._record_failure(job_id, stage_name, lease_owner, error)
                return

            # SubtitleService owns its selection transition for operator-driven
            # and automatic candidate flows. Persist the runner-validated
            # manifest even when that service has already completed the stage.
            current = self._stage(job_id, stage_name)
            if (
                stage_name == "subtitle_selection"
                and current["state"] == StageState.COMPLETED.value
            ):
                completed = self.store.transition_stage(
                    job_id,
                    stage_name,
                    StageState.COMPLETED,
                    expected_state=StageState.COMPLETED,
                    warnings=list(result.warnings),
                    output_manifest=result.output_manifest,
                    progress_unit=current["progress"]["unit"],
                    lease_owner=lease_owner,
                )
                if completed is None:
                    return
                continue

            progress_unit = self._stage(job_id, stage_name)["progress"]["unit"]
            if stage_index == len(self.stages) - 1:
                completed = self.store.transition_stage_and_job(
                    job_id,
                    stage_name,
                    StageState.COMPLETED,
                    JobState.COMPLETED,
                    warnings=list(result.warnings),
                    output_manifest=result.output_manifest,
                    progress_unit=progress_unit,
                    lease_owner=lease_owner,
                )
            else:
                completed = self.store.transition_stage(
                    job_id,
                    stage_name,
                    StageState.COMPLETED,
                    expected_state=StageState.RUNNING,
                    warnings=list(result.warnings),
                    output_manifest=result.output_manifest,
                    progress_unit=progress_unit,
                    lease_owner=lease_owner,
                )
            if completed is None:
                return

        job = self.store.get_job(job_id)
        if job is None or job["state"] == JobState.COMPLETED.value:
            return
        if self._cancel_requested(job_id):
            self._apply_cancellation(job_id, lease_owner)
            return
        if self._renew(job_id, lease_owner):
            completed_job = self.store.transition_job(
                job_id,
                JobState.COMPLETED,
                expected_state=JobState.RUNNING,
                lease_owner=lease_owner,
            )
            if completed_job is None:
                raise asyncio.CancelledError("The worker no longer owns the job lease")

    def _ensure_stages(self, job_id: str) -> None:
        for ordinal, stage_name in enumerate(self.stages, 1):
            policy = self._policy(stage_name)
            self.store.ensure_stage(
                job_id,
                stage_name,
                ordinal=ordinal,
                state=StageState.PENDING,
                max_auto_attempts=policy.max_attempts,
            )

    async def _execute_stage(
        self, job_id: str, stage_name: str, lease_owner: str
    ) -> StageResult:
        def progress(
            numerator: int, denominator: int, unit: str
        ) -> _CompletedAwaitable:
            if numerator < 0 or denominator < 1 or numerator > denominator:
                raise ValueError("Stage progress must be bounded by its denominator")
            if not self._renew(job_id, lease_owner):
                raise asyncio.CancelledError("The worker no longer owns the job lease")
            updated = self.store.transition_stage(
                job_id,
                stage_name,
                StageState.RUNNING,
                expected_state=StageState.RUNNING,
                progress_numerator=numerator,
                progress_denominator=denominator,
                progress_unit=unit,
                lease_owner=lease_owner,
            )
            if updated is None:
                raise asyncio.CancelledError("The worker no longer owns the job lease")
            return _CompletedAwaitable()

        progress.lease_owner = lease_owner  # type: ignore[attr-defined]

        value = self.services.run_stage(stage_name, job_id, progress)
        raw = await value if inspect.isawaitable(value) else value
        result = _stage_result(raw)
        if not await self._validate(stage_name, result.output_manifest):
            raise AttentionRequired(
                f"Output from stage {stage_name} did not pass artifact validation.",
                code="invalid_stage_output",
                actions=("retry",),
            )
        if not self._renew(job_id, lease_owner):
            raise asyncio.CancelledError("The worker no longer owns the job lease")
        return StageResult(
            output_manifest=sanitize_value(result.output_manifest, self.settings),
            warnings=tuple(sanitize_value(result.warnings, self.settings)),
        )

    async def _validate(self, stage_name: str, manifest: Mapping[str, Any]) -> bool:
        value = self.services.validate_stage(stage_name, manifest)
        return bool(await value if inspect.isawaitable(value) else value)

    def _policy(self, stage_name: str) -> RetryPolicy:
        provider = getattr(self.services, "retry_policy", None)
        if provider is not None:
            return provider(stage_name)
        attempts = 3 if stage_name in {"subtitle_discovery", "metadata"} else 1
        return RetryPolicy(attempts, DEFAULT_RETRY_DELAYS)

    def _record_failure(
        self,
        job_id: str,
        stage_name: str,
        lease_owner: str,
        error: OperationalError,
    ) -> None:
        state = StageState.FAILED if error.retryable else StageState.NEEDS_ATTENTION
        job_state = JobState.FAILED if error.retryable else JobState.NEEDS_ATTENTION
        next_action = (
            error.actions[0]
            if error.actions
            else ("retry" if error.retryable else None)
        )
        safe_code = sanitize_text(error.code, self.settings)
        safe_message = sanitize_text(error.message, self.settings)
        outcome = self.store.transition_stage_and_job(
            job_id,
            stage_name,
            state,
            job_state,
            safe_error_code=safe_code,
            safe_error_message=safe_message,
            retryable=error.retryable,
            next_action=next_action,
            lease_owner=lease_owner,
        )
        if outcome is None:
            raise asyncio.CancelledError("The worker no longer owns the job lease")

    def _invalid_completed_stage(
        self,
        job_id: str,
        stage_name: str,
        lease_owner: str,
        *,
        code: str = "invalid_completed_artifact",
        message: str | None = None,
    ) -> None:
        safe_message = sanitize_text(
            message or f"Completed output for stage {stage_name} no longer validates.",
            self.settings,
        )
        safe_code = sanitize_text(code, self.settings)
        outcome = self.store.transition_job(
            job_id,
            JobState.NEEDS_ATTENTION,
            expected_state=JobState.RUNNING,
            safe_error_code=safe_code,
            safe_error_message=safe_message,
            retryable=False,
            next_action="retry",
            lease_owner=lease_owner,
            additional_event_type="artifact_validation_failed",
            additional_event_message=safe_message,
            additional_event_stage_name=stage_name,
        )
        if outcome is None:
            raise asyncio.CancelledError("The worker no longer owns the job lease")

    def _cancel_requested(self, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        return job is None or bool(job["cancel_requested"])

    def _apply_cancellation(self, job_id: str, lease_owner: str) -> None:
        cancelled = self.store.transition_job(
            job_id,
            JobState.CANCELLED,
            expected_state=JobState.RUNNING,
            lease_owner=lease_owner,
        )
        if cancelled is None:
            raise asyncio.CancelledError("The worker no longer owns the job lease")

    def _renew(self, job_id: str, lease_owner: str) -> bool:
        return self.store.renew_lease(
            job_id, lease_owner, lease_seconds=self.lease_seconds
        )

    def _stage(self, job_id: str, stage_name: str) -> dict[str, Any]:
        detail = self.store.get_job_detail(job_id)
        if detail is None:
            raise KeyError("Run was not found")
        return next(stage for stage in detail["stages"] if stage["name"] == stage_name)


class _CompletedAwaitable:
    def __await__(self):
        if False:
            yield None
        return None


def _stage_result(value: StageResult | Mapping[str, Any] | None) -> StageResult:
    if isinstance(value, StageResult):
        return value
    if value is None:
        return StageResult()
    if "output_manifest" in value:
        return StageResult(
            output_manifest=value.get("output_manifest") or {},
            warnings=tuple(value.get("warnings") or ()),
        )
    return StageResult(output_manifest=value)


def _atomic_json_file(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    try:
        with partial.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)


def _label_year(label: str) -> str:
    match = re.search(r"\((\d{4})\)\s*$", label)
    return match.group(1) if match else ""


def _runtime_minutes(metadata: Mapping[str, Any]) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)", str(metadata.get("Runtime", "")))
    return float(match.group(1)) if match else None


async def _run_in_worker(function: Callable[..., Any], *args: Any) -> Any:
    """Run blocking generation off-loop and join it without executor leakage."""
    outcomes: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            outcomes.put((True, function(*args)))
        except BaseException as exc:
            outcomes.put((False, exc))

    worker = threading.Thread(
        target=run,
        name="generation-stage-worker",
        daemon=True,
    )
    worker.start()
    cancelled: asyncio.CancelledError | None = None
    while worker.is_alive():
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError as exc:
            # Retain ownership until the blocking call yields; callbacks observe
            # durable cancellation and stop render/encode loops promptly.
            cancelled = exc
    worker.join()
    if cancelled is not None:
        raise cancelled
    succeeded, value = outcomes.get_nowait()
    if succeeded:
        return value
    raise value


def get_client():
    """Legacy lazy constructor retained until submission routes adopt the dispatcher."""
    from src.data.opensubtitles import OpenSubtitlesClient

    return OpenSubtitlesClient(
        api_key=os.environ["OPENSUBTITLES_API_KEY"],
        user_agent=os.environ["OPENSUBTITLES_USER_AGENT"],
        jwt=os.environ.get("OPENSUBTITLES_JWT"),
        username=os.environ.get("OPENSUBTITLES_USERNAME"),
        password=os.environ.get("OPENSUBTITLES_PASSWORD"),
    )


async def run_pipeline(*_args: Any, **_kwargs: Any) -> None:
    """Reject legacy direct execution; durable jobs must run through JobDispatcher."""
    raise RuntimeError("Direct pipeline execution is disabled; enqueue the durable job")
