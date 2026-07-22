import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from api.database import OperationStore
from api.dispatcher import JobDispatcher


@pytest.fixture
def anyio_backend():
    return "asyncio"


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 7, 21, 10, 0, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def store(tmp_path):
    result = OperationStore(tmp_path / "dispatcher.db")
    result.initialize()
    return result


class RecordingRunner:
    def __init__(self, calls, *, gate=None):
        self.calls = calls
        self.gate = gate

    async def run(self, job_id, lease_owner):
        self.calls.append((job_id, lease_owner))
        if self.gate is not None:
            await self.gate.wait()


class LeaseRecordingStore:
    def __init__(self):
        self.renewals = 0
        self.released = []
        self.claimed = False

    def recover_expired_leases(self):
        return []

    def claim_next_job(self, owner, *, lease_seconds):
        if self.claimed:
            return None
        self.claimed = True
        return {"id": "job_heartbeat"}

    def renew_lease(self, job_id, owner, *, lease_seconds):
        self.renewals += 1
        return True

    def release_job_lease(self, job_id, owner):
        self.released.append((job_id, owner))
        return True


async def eventually(predicate, attempts=100):
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached")


@pytest.mark.anyio
async def test_durable_enqueue_before_dispatcher_start_is_executed(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    calls = []
    dispatcher = JobDispatcher(store, lambda: RecordingRunner(calls), poll_interval=0.01)

    await dispatcher.start()
    await eventually(lambda: len(calls) == 1)
    await dispatcher.stop()

    assert calls[0][0] == job["id"]


@pytest.mark.anyio
async def test_two_dispatchers_do_not_execute_one_job_twice(store):
    store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    gate = asyncio.Event()
    calls = []
    first = JobDispatcher(store, lambda: RecordingRunner(calls, gate=gate), poll_interval=0.01)
    second = JobDispatcher(store, lambda: RecordingRunner(calls, gate=gate), poll_interval=0.01)

    await asyncio.gather(first.start(), second.start())
    await eventually(lambda: len(calls) == 1)
    gate.set()
    await asyncio.gather(first.stop(), second.stop())

    assert len(calls) == 1
    assert calls[0][1]


@pytest.mark.anyio
async def test_repeated_wakes_are_coalesced(store):
    dispatcher = JobDispatcher(store, lambda: RecordingRunner([]), poll_interval=60)
    await dispatcher.start()

    dispatcher.wake()
    dispatcher.wake()
    dispatcher.wake()

    assert dispatcher.pending_wakes == 1
    await dispatcher.stop()


@pytest.mark.anyio
async def test_stop_waits_for_owned_runner_and_retains_task(store):
    store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    gate = asyncio.Event()
    calls = []
    dispatcher = JobDispatcher(store, lambda: RecordingRunner(calls, gate=gate), poll_interval=0.01)
    await dispatcher.start()
    await eventually(lambda: len(calls) == 1)

    stopping = asyncio.create_task(dispatcher.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    assert dispatcher.active_count == 1

    gate.set()
    await stopping
    assert dispatcher.active_count == 0


@pytest.mark.anyio
async def test_start_recovers_expired_lease_before_claiming(tmp_path):
    clock = MutableClock()
    store = OperationStore(tmp_path / "recovery.db", clock=clock)
    store.initialize()
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.claim_next_job("dead-worker", lease_seconds=1)
    clock.advance(2)
    calls = []
    dispatcher = JobDispatcher(store, lambda: RecordingRunner(calls), poll_interval=0.01)

    await dispatcher.start()
    await eventually(lambda: len(calls) == 1)
    await dispatcher.stop()

    assert calls[0][0] == job["id"]
    assert any(event["type"] == "restart_recovery" for event in store.list_events(job["id"]))


@pytest.mark.anyio
async def test_long_runner_gets_periodic_lease_heartbeat_without_progress():
    store = LeaseRecordingStore()
    gate = asyncio.Event()
    dispatcher = JobDispatcher(
        store,
        lambda: RecordingRunner([], gate=gate),
        poll_interval=0.01,
        lease_seconds=0.06,
    )

    await dispatcher.start()
    await eventually(lambda: store.renewals >= 2)
    gate.set()
    await dispatcher.stop()

    assert store.renewals >= 2
    assert store.released == []


@pytest.mark.anyio
async def test_heartbeat_renews_immediately_at_claimed_runner_boundary():
    store = LeaseRecordingStore()
    started = asyncio.Event()
    gate = asyncio.Event()

    class ClaimedRunner:
        async def run(self, job_id, lease_owner):
            started.set()
            await gate.wait()

    dispatcher = JobDispatcher(
        store,
        ClaimedRunner,
        poll_interval=0.01,
        lease_seconds=0.06,
    )

    await dispatcher.start()
    await started.wait()
    renewals_at_boundary = store.renewals
    gate.set()
    await dispatcher.stop()

    assert renewals_at_boundary == 1


@pytest.mark.anyio
async def test_bounded_stop_cancels_runner_and_requeues_owned_work(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.ensure_stage(job["id"], "analysis", state="queued")
    started = asyncio.Event()

    class BlockingRunner:
        async def run(self, job_id, lease_owner):
            store.transition_stage(job_id, "analysis", "running", lease_owner=lease_owner)
            store.start_attempt(job_id, "analysis", lease_owner=lease_owner)
            started.set()
            await asyncio.Event().wait()

    dispatcher = JobDispatcher(
        store,
        BlockingRunner,
        poll_interval=0.01,
        lease_seconds=30,
        shutdown_timeout=0.01,
    )
    await dispatcher.start()
    await started.wait()

    await dispatcher.stop()

    detail = store.get_job_detail(job["id"])
    assert detail["run"]["state"] == "queued"
    assert detail["stages"][0]["state"] == "queued"
    assert detail["attempts"][0]["finished_at"] is not None
    assert dispatcher.active_count == 0


@pytest.mark.anyio
async def test_heartbeat_error_cancels_nested_runner_without_orphaning_it():
    store = LeaseRecordingStore()
    cleaned_up = asyncio.Event()

    def fail_renewal(job_id, owner, *, lease_seconds):
        raise RuntimeError("database unavailable")

    store.renew_lease = fail_renewal

    class CleanupRunner:
        async def run(self, job_id, lease_owner):
            try:
                await asyncio.Event().wait()
            finally:
                cleaned_up.set()

    dispatcher = JobDispatcher(
        store,
        CleanupRunner,
        poll_interval=0.01,
        lease_seconds=0.03,
    )
    await dispatcher.start()

    await eventually(cleaned_up.is_set)
    await dispatcher.stop()

    assert dispatcher.active_count == 0


@pytest.mark.anyio
async def test_stop_returns_at_deadline_without_releasing_cancellation_resistant_work():
    store = LeaseRecordingStore()
    started = asyncio.Event()
    suppressed = asyncio.Event()
    finish = asyncio.Event()

    class ResistantRunner:
        async def run(self, job_id, lease_owner):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                suppressed.set()
                await finish.wait()

    dispatcher = JobDispatcher(
        store,
        ResistantRunner,
        poll_interval=0.01,
        lease_seconds=0.06,
        shutdown_timeout=0.04,
    )
    await dispatcher.start()
    await started.wait()
    stopping = asyncio.create_task(dispatcher.stop())
    try:
        await asyncio.sleep(0.12)
        assert stopping.done()
        assert suppressed.is_set()
        assert dispatcher.active_count == 1
        assert store.released == []
        with pytest.raises(RuntimeError, match="stopping"):
            await dispatcher.start()
        stopping_again = asyncio.create_task(dispatcher.stop())
        await asyncio.sleep(0.12)
        assert stopping_again.done()
        assert dispatcher.active_count == 1
        assert store.released == []
        await stopping_again
    finally:
        finish.set()
        await stopping
    await eventually(lambda: dispatcher.active_count == 0)
    assert len(store.released) == 1
    await dispatcher.stop()


@pytest.mark.anyio
async def test_supervisor_error_cancels_and_observes_active_runner():
    store = LeaseRecordingStore()
    cleaned_up = asyncio.Event()
    recoveries = 0

    def recover_then_fail():
        nonlocal recoveries
        recoveries += 1
        if recoveries >= 3:
            raise RuntimeError("recovery failed")
        return []

    store.recover_expired_leases = recover_then_fail

    class CleanupRunner:
        async def run(self, job_id, lease_owner):
            try:
                await asyncio.Event().wait()
            finally:
                cleaned_up.set()

    dispatcher = JobDispatcher(
        store,
        CleanupRunner,
        concurrency=2,
        poll_interval=0.01,
        lease_seconds=1,
    )
    await dispatcher.start()
    await eventually(cleaned_up.is_set)

    assert dispatcher.active_count == 0
    assert len(store.released) == 1
    await dispatcher.stop()


@pytest.mark.anyio
async def test_runner_factory_failure_occurs_before_job_claim(store):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")

    def fail_factory():
        raise RuntimeError("invalid runner configuration")

    dispatcher = JobDispatcher(store, fail_factory, poll_interval=0.01)
    await dispatcher.start()
    await eventually(lambda: dispatcher._supervisor is not None and dispatcher._supervisor.done())
    await dispatcher.stop()

    assert store.get_job(job["id"])["state"] == "queued"
