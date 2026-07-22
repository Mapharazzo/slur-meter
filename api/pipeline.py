"""Injected, durable, resumable pipeline stage orchestration."""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Protocol

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
    def __call__(self, numerator: int, denominator: int, unit: str) -> Awaitable[None]: ...


class PipelineServices(Protocol):
    """Task-4 boundary implemented by injected fakes now and real stages later."""

    def run_stage(
        self,
        stage_name: str,
        job_id: str,
        progress: ProgressReporter,
    ) -> StageResult | Mapping[str, Any] | Awaitable[StageResult | Mapping[str, Any]]: ...

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


class PipelineStore(Protocol):
    def get_job(self, job_id: str) -> dict[str, Any] | None: ...

    def get_job_detail(self, job_id: str) -> dict[str, Any] | None: ...

    def ensure_stage(self, job_id: str, name: str, **fields: Any) -> dict[str, Any]: ...

    def transition_stage(self, job_id: str, stage_name: str, new_state: object, **fields: Any) -> dict[str, Any] | None: ...

    def transition_job(self, job_id: str, new_state: object, **fields: Any) -> dict[str, Any] | None: ...

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
                    error = classify_exception(exc, f"{stage_name} validation", self.settings)
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
                raise asyncio.CancelledError(
                    "The worker no longer owns the job lease"
                )

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
        def progress(numerator: int, denominator: int, unit: str) -> _CompletedAwaitable:
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
        next_action = error.actions[0] if error.actions else ("retry" if error.retryable else None)
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
