"""Bounded automatic retries with durable attempt and event recording."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Protocol, TypeVar

from api.domain import AttemptTrigger
from api.errors import OperationalError, classify_exception, sanitize_text
from api.settings import Settings


@dataclass(frozen=True)
class RetryPolicy:
    """Maximum automatic attempts and the delays between them."""

    max_attempts: int
    delays: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("Retry policy requires at least one attempt")
        if any(delay < 0 for delay in self.delays):
            raise ValueError("Retry delays must be non-negative")

    def delay_after(self, attempt_number: int) -> float:
        if not self.delays:
            return 0.0
        index = min(max(attempt_number - 1, 0), len(self.delays) - 1)
        return float(self.delays[index])


@dataclass(frozen=True)
class RetryContext:
    """Durable identifiers needed to record one stage retry cycle."""

    job_id: str
    stage_name: str
    lease_owner: str | None = None
    trigger: AttemptTrigger = AttemptTrigger.AUTOMATIC
    settings: Settings | None = None
    cancel_requested: Callable[[], bool] | None = None


class RetryStore(Protocol):
    def start_attempt(self, job_id: str, stage_name: str, **fields: Any) -> dict[str, Any] | None: ...

    def finish_attempt(
        self, attempt_id: int, outcome: str, **fields: Any
    ) -> object | None: ...

    def record_event(self, job_id: str, **fields: Any) -> object | None: ...


T = TypeVar("T")


async def run_with_attempts(
    operation: Callable[[], T | Awaitable[T]],
    context: RetryContext | Mapping[str, Any],
    policy: RetryPolicy,
    store: RetryStore,
    sleep: Callable[[float], Awaitable[object]],
) -> T:
    """Run an operation under a bounded policy and persist every outcome."""
    resolved = _context(context)
    for sequence in range(1, policy.max_attempts + 1):
        if resolved.cancel_requested is not None and resolved.cancel_requested():
            raise asyncio.CancelledError("Cancellation was requested between attempts")
        attempt = store.start_attempt(
            resolved.job_id,
            resolved.stage_name,
            trigger=resolved.trigger,
            max_attempts=policy.max_attempts,
            lease_owner=resolved.lease_owner,
        )
        if attempt is None:
            raise asyncio.CancelledError("The worker no longer owns the job lease")
        try:
            value = operation()
            result = await value if inspect.isawaitable(value) else value
        except asyncio.CancelledError:
            finished = store.finish_attempt(
                attempt["id"],
                "cancelled",
                diagnostics={"code": "execution_interrupted"},
                lease_owner=resolved.lease_owner,
            )
            if finished is None:
                raise asyncio.CancelledError(
                    "The worker no longer owns the job lease"
                ) from None
            raise
        except Exception as exc:
            error = classify_exception(exc, resolved.stage_name, resolved.settings)
            diagnostics = _error_diagnostics(error, exc, resolved.settings)
            finished = store.finish_attempt(
                attempt["id"],
                "failed",
                retryable=error.retryable,
                diagnostics=diagnostics,
                lease_owner=resolved.lease_owner,
            )
            if finished is None:
                raise asyncio.CancelledError(
                    "The worker no longer owns the job lease"
                ) from None
            if not error.retryable or sequence >= policy.max_attempts:
                raise error from None
            delay = policy.delay_after(sequence)
            event = store.record_event(
                resolved.job_id,
                event_type="retry_scheduled",
                severity="warning",
                message=f"Stage {resolved.stage_name} will retry after a temporary failure.",
                stage_name=resolved.stage_name,
                attempt_id=attempt["id"],
                data={
                    "attempt": sequence,
                    "next_attempt": sequence + 1,
                    "delay_seconds": delay,
                },
                lease_owner=resolved.lease_owner,
            )
            if event is None:
                raise asyncio.CancelledError(
                    "The worker no longer owns the job lease"
                ) from None
            await sleep(delay)
            continue

        finished = store.finish_attempt(
            attempt["id"],
            "completed",
            output=sanitize_value(result, resolved.settings),
            lease_owner=resolved.lease_owner,
        )
        if finished is None:
            raise asyncio.CancelledError("The worker no longer owns the job lease")
        return result
    raise RuntimeError("Retry policy exhausted without an outcome")  # pragma: no cover


def _context(value: RetryContext | Mapping[str, Any]) -> RetryContext:
    if isinstance(value, RetryContext):
        return value
    return RetryContext(
        job_id=str(value["job_id"]),
        stage_name=str(value["stage_name"]),
        lease_owner=value.get("lease_owner"),
        trigger=AttemptTrigger(value.get("trigger", AttemptTrigger.AUTOMATIC)),
        settings=value.get("settings"),
        cancel_requested=value.get("cancel_requested"),
    )


def _error_diagnostics(
    error: OperationalError,
    source: Exception,
    settings: Settings | None,
) -> dict[str, Any]:
    return {
        "code": sanitize_text(error.code, settings),
        "category": error.category.value,
        "exception_type": type(source).__name__,
    }


def sanitize_value(value: Any, settings: Settings | None = None) -> Any:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {
            sanitize_text(key, settings): sanitize_value(item, settings)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item, settings) for item in value]
    if isinstance(value, str):
        return sanitize_text(value, settings)
    if isinstance(value, (bool, int, float)):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return sanitize_value(asdict(value), settings)
    return sanitize_text(value, settings)
