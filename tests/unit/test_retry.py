import asyncio

import pytest

from api.domain import FailureCategory
from api.errors import AttentionRequired, TransientFailure
from api.retry import RetryContext, RetryPolicy, run_with_attempts


@pytest.fixture
def anyio_backend():
    return "asyncio"


class RecordingStore:
    def __init__(self):
        self.attempts = []
        self.finished = []
        self.events = []

    def start_attempt(self, job_id, stage_name, **fields):
        attempt = {
            "id": len(self.attempts) + 1,
            "attempt_number": len(self.attempts) + 1,
            **fields,
        }
        self.attempts.append(attempt)
        return attempt

    def finish_attempt(self, attempt_id, outcome, **fields):
        self.finished.append((attempt_id, outcome, fields))
        return {"id": attempt_id, "outcome": outcome}

    def record_event(self, job_id, **fields):
        self.events.append(fields)
        return fields


@pytest.mark.anyio
async def test_transient_operation_succeeds_on_attempt_three():
    store = RecordingStore()
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TransientFailure("Provider is temporarily unavailable")
        return {"provider_id": "safe-42"}

    result = await run_with_attempts(
        operation,
        RetryContext("job_1", "metadata"),
        RetryPolicy(max_attempts=3, delays=(0, 0)),
        store,
        asyncio.sleep,
    )

    assert result == {"provider_id": "safe-42"}
    assert [row[1] for row in store.finished] == ["failed", "failed", "completed"]
    assert [row["attempt_number"] for row in store.attempts] == [1, 2, 3]


@pytest.mark.anyio
async def test_transient_exhaustion_raises_the_sanitized_operational_error():
    store = RecordingStore()

    async def operation():
        raise TransientFailure(
            "Provider is temporarily unavailable",
            technical_detail="Bearer secret-token at /home/operator/private.py",
        )

    with pytest.raises(TransientFailure) as raised:
        await run_with_attempts(
            operation,
            RetryContext("job_1", "metadata"),
            RetryPolicy(max_attempts=3, delays=(0, 0)),
            store,
            asyncio.sleep,
        )

    assert raised.value.retryable is True
    assert len(store.attempts) == 3
    assert all(row[1] == "failed" for row in store.finished)
    assert "secret-token" not in repr(store.finished)
    assert "/home/operator" not in repr(store.finished)


@pytest.mark.anyio
async def test_attempt_diagnostics_allowlist_excludes_exception_and_upstream_text():
    store = RecordingStore()

    async def operation():
        raise RuntimeError("private upstream response body")

    with pytest.raises(AttentionRequired):
        await run_with_attempts(
            operation,
            RetryContext("job_1", "analysis"),
            RetryPolicy(max_attempts=1),
            store,
            asyncio.sleep,
        )

    assert store.finished[0][2]["diagnostics"] == {
        "code": "unexpected_operation_error",
        "category": "unexpected",
        "exception_type": "RuntimeError",
    }
    assert "private upstream response body" not in repr(store.finished)


@pytest.mark.anyio
async def test_retry_stops_when_fenced_event_write_loses_ownership():
    store = RecordingStore()
    store.record_event = lambda *args, **kwargs: None

    async def operation():
        raise TimeoutError("temporary")

    with pytest.raises(asyncio.CancelledError):
        await run_with_attempts(
            operation,
            RetryContext("job_1", "metadata", lease_owner="stale-owner"),
            RetryPolicy(max_attempts=2, delays=(0,)),
            store,
            asyncio.sleep,
        )

    assert len(store.attempts) == 1


@pytest.mark.anyio
async def test_deterministic_failure_is_attempted_once_even_with_retry_budget():
    store = RecordingStore()

    async def operation():
        raise AttentionRequired(
            "The input is invalid",
            category=FailureCategory.VALIDATION,
        )

    with pytest.raises(AttentionRequired):
        await run_with_attempts(
            operation,
            RetryContext("job_1", "analysis"),
            RetryPolicy(max_attempts=3, delays=(0, 0)),
            store,
            asyncio.sleep,
        )

    assert len(store.attempts) == 1
    assert len(store.finished) == 1


@pytest.mark.anyio
async def test_retry_scheduling_records_delay_and_next_attempt():
    store = RecordingStore()
    sleeps = []

    async def operation():
        if len(store.attempts) == 1:
            raise TimeoutError("temporary")
        return {}

    async def sleep(delay):
        sleeps.append(delay)

    await run_with_attempts(
        operation,
        RetryContext("job_1", "subtitle_discovery"),
        RetryPolicy(max_attempts=2, delays=(2.5,)),
        store,
        sleep,
    )

    retry_events = [event for event in store.events if event["event_type"] == "retry_scheduled"]
    assert sleeps == [2.5]
    assert retry_events[0]["data"] == {"attempt": 1, "next_attempt": 2, "delay_seconds": 2.5}


@pytest.mark.anyio
async def test_cancellation_between_retry_attempts_stops_before_new_attempt():
    store = RecordingStore()
    cancelled = False

    async def operation():
        raise TimeoutError("temporary")

    async def sleep(_delay):
        nonlocal cancelled
        cancelled = True

    with pytest.raises(asyncio.CancelledError):
        await run_with_attempts(
            operation,
            RetryContext(
                "job_1",
                "metadata",
                cancel_requested=lambda: cancelled,
            ),
            RetryPolicy(max_attempts=3, delays=(0, 0)),
            store,
            sleep,
        )

    assert len(store.attempts) == 1
