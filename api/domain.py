"""Durable state values and central transition validation for operations."""

from __future__ import annotations

from enum import StrEnum
from typing import TypeVar


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class StageState(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class AttemptTrigger(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL_RETRY = "manual_retry"
    RESUME = "resume"
    RESTART_RECOVERY = "restart_recovery"
    ARTIFACT_INVALIDATION = "artifact_invalidation"


class FailureCategory(StrEnum):
    TRANSIENT = "transient"
    VALIDATION = "validation"
    CONFIGURATION = "configuration"
    DETERMINISTIC = "deterministic"
    AMBIGUOUS_PUBLISH = "ambiguous_publish"
    UNEXPECTED = "unexpected"


class InvalidTransition(ValueError):  # noqa: N818 - public domain interface
    """Raised when a state move is not part of the durable workflow."""


_JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELLED}),
    JobState.RUNNING: frozenset(
        {
            JobState.NEEDS_ATTENTION,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.COMPLETED,
        }
    ),
    JobState.NEEDS_ATTENTION: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.FAILED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.CANCELLED: frozenset({JobState.QUEUED}),
    JobState.COMPLETED: frozenset({JobState.QUEUED}),
}

_STAGE_TRANSITIONS: dict[StageState, frozenset[StageState]] = {
    StageState.PENDING: frozenset(
        {StageState.QUEUED, StageState.CANCELLED, StageState.SKIPPED}
    ),
    StageState.QUEUED: frozenset(
        {StageState.RUNNING, StageState.CANCELLED, StageState.SKIPPED}
    ),
    StageState.RUNNING: frozenset(
        {
            StageState.NEEDS_ATTENTION,
            StageState.FAILED,
            StageState.CANCELLED,
            StageState.COMPLETED,
        }
    ),
    StageState.NEEDS_ATTENTION: frozenset(
        {StageState.QUEUED, StageState.CANCELLED, StageState.SKIPPED}
    ),
    StageState.FAILED: frozenset(
        {StageState.QUEUED, StageState.CANCELLED, StageState.SKIPPED}
    ),
    StageState.CANCELLED: frozenset({StageState.QUEUED, StageState.SKIPPED}),
    StageState.COMPLETED: frozenset(),
    StageState.SKIPPED: frozenset(),
}


def assert_job_transition(
    old: JobState,
    new: JobState,
    trigger: AttemptTrigger | None = None,
) -> None:
    """Assert that a job state update is one of the allowed durable moves."""
    if (
        old is JobState.RUNNING
        and new is JobState.QUEUED
        and trigger is AttemptTrigger.RESTART_RECOVERY
    ):
        return
    _assert_transition(old, new, _JOB_TRANSITIONS, "job")


def assert_stage_transition(
    old: StageState,
    new: StageState,
    trigger: AttemptTrigger | None = None,
    stage_name: str | None = None,
) -> None:
    """Assert that a stage state update is one of the allowed durable moves."""
    if (
        old is StageState.RUNNING
        and new is StageState.QUEUED
        and trigger is AttemptTrigger.RESTART_RECOVERY
    ):
        return
    if (
        old is StageState.COMPLETED
        and new is StageState.QUEUED
        and trigger is AttemptTrigger.ARTIFACT_INVALIDATION
        and bool(stage_name)
    ):
        return
    _assert_transition(old, new, _STAGE_TRANSITIONS, "stage")


_State = TypeVar("_State", bound=StrEnum)


def _assert_transition(
    old: _State,
    new: _State,
    transitions: dict[_State, frozenset[_State]],
    kind: str,
) -> None:
    try:
        allowed = transitions[old]
    except KeyError as exc:
        raise InvalidTransition(f"Unknown {kind} state: {old!r}") from exc
    if new not in allowed:
        raise InvalidTransition(f"Cannot transition {kind} from {old.value!r} to {new.value!r}")
