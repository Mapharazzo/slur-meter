#!/usr/bin/env python3
"""Thin synchronous console adapter for the durable generation pipeline."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

import yaml

from api.database import OperationStore
from api.domain import JobState, StageState
from api.errors import AttentionRequired, classify_exception, sanitize_text
from api.pipeline import GENERATION_STAGES, GenerationPipelineServices, PipelineRunner
from api.settings import Settings, canonical_imdb_id, confined_path, validate_job_id

BASE_DIR = Path(__file__).resolve().parent
ANALYSIS_STAGES = GENERATION_STAGES[: GENERATION_STAGES.index("analysis") + 1]
RENDER_STAGES = GENERATION_STAGES[GENERATION_STAGES.index("graph") :]


def _console_error(line: str) -> None:
    print(line, file=sys.stderr)


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(description="Daily Slur Meter")
    parser.add_argument("--imdb", help="IMDb ID (for example tt0110912)")
    parser.add_argument("--query", help="Movie title to search")
    parser.add_argument("--render", action="store_true", help="Run through video encode")
    parser.add_argument(
        "--render-only",
        metavar="JOB_ID",
        help="Render an existing run with validated analysis",
    )
    parser.add_argument("--config", default="config.yaml", help="Project configuration file")
    return parser


def _default_store(settings: Settings) -> OperationStore:
    return OperationStore(settings.data_dir / "slur_meter.db")


def _default_services(
    store: OperationStore, settings: Settings, config: Mapping[str, Any]
) -> GenerationPipelineServices:
    return GenerationPipelineServices(store, settings, config=config)


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    base_dir: str | Path = BASE_DIR,
    settings_factory: Callable[[Path], Settings] = Settings.from_env,
    store_factory: Callable[[Settings], OperationStore] = _default_store,
    services_factory: Callable[
        [OperationStore, Settings, Mapping[str, Any]], Any
    ] = _default_services,
    lease_seconds: float = 30.0,
    heartbeat_interval: float | None = None,
    stdout: Callable[[str], Any] = print,
    stderr: Callable[[str], Any] | None = None,
) -> int:
    """Run one durable job synchronously and return a deliberate process status."""
    if stderr is None:
        stderr = _console_error
    settings: Settings | None = None
    try:
        args = _parser().parse_args(list(argv) if argv is not None else None)
        _validate_identity_args(args)

        root = Path(base_dir).resolve()
        settings = settings_factory(root)
        config_path = confined_path(root, args.config)
        if not config_path.is_file():
            raise ValueError("Configuration file was not found")
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(config, Mapping):
            raise ValueError("Configuration must contain a mapping")

        store = store_factory(settings)
        store.initialize()
        services = services_factory(store, settings, config)
        terminal_stage, stages, job = _prepare_job(args, store, services, settings)
        owner = f"cli-{uuid.uuid4().hex}"
        if lease_seconds <= 0:
            raise ValueError("CLI lease duration must be positive")
        interval = (
            min(10.0, lease_seconds / 3)
            if heartbeat_interval is None
            else float(heartbeat_interval)
        )
        if interval <= 0 or interval >= lease_seconds:
            raise ValueError("CLI heartbeat interval must be positive and below its lease")
        claimed = store.claim_job(job["id"], owner, lease_seconds=lease_seconds)
        if claimed is None:
            raise AttentionRequired(
                "The requested run is already owned or is not ready to run.",
                code="run_not_claimable",
                actions=("retry",),
            )

        runner = PipelineRunner(
                store,
                services,
                stages=stages,
                lease_seconds=lease_seconds,
                settings=settings,
            )
        asyncio.run(
            _run_claimed_job(
                runner,
                store,
                job["id"],
                owner,
                lease_seconds=lease_seconds,
                heartbeat_interval=interval,
            )
        )
        final_job = store.get_job(job["id"])
        if final_job is None or final_job["state"] != JobState.COMPLETED.value:
            safe_error = (final_job or {}).get("safe_error") or {}
            message = (
                safe_error.get("message")
                or "The requested pipeline result was not produced."
            )
            raise AttentionRequired(
                message,
                code="pipeline_incomplete",
                actions=("inspect_run",),
            )

        artifact_path = asyncio.run(
            _validated_stage_path(
                store, services, settings, job["id"], terminal_stage
            )
        )
        stdout(f"Job: {job['id']}")
        stdout(f"Artifact: {_display_path(artifact_path, settings)}")
        return 0
    except asyncio.CancelledError:
        stderr("Error: The requested run lost its worker lease.")
        return 2
    except Exception as exc:
        error = classify_exception(exc, "CLI execution", settings)
        message = sanitize_text(error.message, settings)
        stderr(f"Error: {message}")
        return 2


async def _run_claimed_job(
    runner: PipelineRunner,
    store: OperationStore,
    job_id: str,
    owner: str,
    *,
    lease_seconds: float,
    heartbeat_interval: float,
) -> None:
    runner_task = asyncio.create_task(runner.run(job_id, owner))

    async def heartbeat() -> bool:
        while not runner_task.done():
            await asyncio.sleep(heartbeat_interval)
            if runner_task.done():
                return True
            if not store.renew_lease(job_id, owner, lease_seconds=lease_seconds):
                return False
        return True

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        done, _pending = await asyncio.wait(
            {runner_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if heartbeat_task in done and not heartbeat_task.result():
            runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await runner_task
            raise asyncio.CancelledError("The CLI lost its worker lease")
        await runner_task
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        if not runner_task.done():
            runner_task.cancel()
            await asyncio.gather(runner_task, return_exceptions=True)
        store.release_job_lease(job_id, owner)


def _validate_identity_args(args: argparse.Namespace) -> None:
    supplied = [args.imdb is not None, args.query is not None, args.render_only is not None]
    if sum(supplied) != 1:
        raise ValueError("Exactly one of --imdb, --query, or --render-only is required")
    if args.render_only is not None and args.render:
        raise ValueError("--render cannot be combined with --render-only")
    if args.imdb is not None:
        args.imdb = canonical_imdb_id(args.imdb)
    if args.query is not None:
        args.query = str(args.query).strip()
        if not args.query:
            raise ValueError("Query text cannot be blank")
    if args.render_only is not None:
        args.render_only = validate_job_id(args.render_only)


def _prepare_job(
    args: argparse.Namespace,
    store: OperationStore,
    services: Any,
    settings: Settings,
):
    if args.render_only is None:
        job, _created = store.create_or_get_active_job(
            args.imdb or "", args.query or "", args.query or args.imdb
        )
        return (
            ("encode", GENERATION_STAGES) if args.render else ("analysis", ANALYSIS_STAGES)
        ) + (job,)

    job = store.get_job(args.render_only)
    if job is None:
        raise AttentionRequired(
            "The requested run was not found.",
            code="run_not_found",
            actions=("check_job_id",),
        )
    asyncio.run(
        _validated_stage_path(store, services, settings, job["id"], "analysis")
    )
    if job["state"] == JobState.COMPLETED.value:
        queued = store.transition_job(
            job["id"], JobState.QUEUED, expected_state=JobState.COMPLETED
        )
    elif job["state"] in {JobState.FAILED.value, JobState.NEEDS_ATTENTION.value}:
        detail = store.get_job_detail(job["id"])
        failed_stage = next(
            (
                stage
                for stage_name in RENDER_STAGES
                for stage in (detail or {}).get("stages", [])
                if stage["name"] == stage_name
                and stage["state"]
                in {StageState.FAILED.value, StageState.NEEDS_ATTENTION.value}
            ),
            None,
        )
        if failed_stage is None:
            queued = None
            accepted = False
        else:
            _decision, queued, _changed, accepted = store.apply_admin_action(
                job["id"], "retry_stage", target_stage=failed_stage["name"]
            )
        if not accepted:
            queued = None
    elif job["state"] == JobState.QUEUED.value:
        queued = job
    else:
        queued = None
    if queued is None:
        raise AttentionRequired(
            "The requested run cannot be resumed in its current state.",
            code="run_not_resumable",
            actions=("inspect_run",),
        )
    return "encode", RENDER_STAGES, queued


async def _validated_stage_path(
    store: OperationStore,
    services: Any,
    settings: Settings,
    job_id: str,
    stage_name: str,
) -> Path:
    detail = store.get_job_detail(job_id)
    stage = next(
        (item for item in (detail or {}).get("stages", []) if item["name"] == stage_name),
        None,
    )
    if stage is None or stage["state"] != StageState.COMPLETED.value:
        raise AttentionRequired(
            f"A completed {stage_name} artifact is required.",
            code=f"{stage_name}_required",
            actions=("inspect_run",),
        )
    manifest = stage.get("output_manifest") or {}
    validation = services.validate_stage(stage_name, job_id, manifest)
    valid = await validation if inspect.isawaitable(validation) else validation
    if not valid:
        raise AttentionRequired(
            f"The {stage_name} artifact did not pass validation.",
            code=f"invalid_{stage_name}_artifact",
            actions=("retry",),
        )
    artifacts = getattr(services, "artifacts", None)
    if artifacts is None:
        raise AttentionRequired(
            "The artifact service is unavailable.",
            code="artifact_service_unavailable",
            actions=("inspect_run",),
        )
    resolved = Path(artifacts.artifact_path(manifest)).resolve()
    run_root = confined_path(settings.output_dir, job_id)
    try:
        resolved.relative_to(run_root)
    except ValueError:
        raise AttentionRequired(
            f"The {stage_name} artifact is outside its confined run.",
            code=f"unconfined_{stage_name}_artifact",
            actions=("inspect_run",),
        ) from None
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise AttentionRequired(
            f"The {stage_name} artifact was not produced.",
            code=f"missing_{stage_name}_artifact",
            actions=("retry",),
        )
    return resolved


def _display_path(path: Path, settings: Settings) -> str:
    try:
        return path.relative_to(settings.base_dir).as_posix()
    except ValueError:
        try:
            relative = path.relative_to(settings.output_dir)
        except ValueError:
            return path.name
        return (Path(settings.output_dir.name) / relative).as_posix()


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
