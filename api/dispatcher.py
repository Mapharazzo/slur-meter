"""Retained in-process dispatcher for the durable SQLite job queue."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from typing import Any, Protocol


class JobRunner(Protocol):
    async def run(self, job_id: str, lease_owner: str) -> object: ...


class DispatcherStore(Protocol):
    def recover_expired_leases(self) -> list[str]: ...

    def claim_next_job(self, owner: str, *, lease_seconds: float) -> dict[str, Any] | None: ...

    def renew_lease(self, job_id: str, owner: str, *, lease_seconds: float) -> bool: ...

    def release_job_lease(self, job_id: str, owner: str) -> bool: ...


class JobDispatcher:
    """Claim durable jobs, retain their tasks, and fence work with leases."""

    def __init__(
        self,
        store: DispatcherStore,
        runner_factory: Callable[[], JobRunner],
        concurrency: int = 1,
        *,
        poll_interval: float = 1.0,
        lease_seconds: float = 30.0,
        shutdown_timeout: float = 30.0,
    ) -> None:
        if concurrency < 1:
            raise ValueError("Dispatcher concurrency must be at least one")
        if poll_interval <= 0 or lease_seconds <= 0 or shutdown_timeout <= 0:
            raise ValueError("Dispatcher timing values must be positive")
        self.store = store
        self.runner_factory = runner_factory
        self.concurrency = int(concurrency)
        self.poll_interval = float(poll_interval)
        self.lease_seconds = float(lease_seconds)
        self.shutdown_timeout = float(shutdown_timeout)
        self._dispatcher_id = uuid.uuid4().hex
        self._wake_event = asyncio.Event()
        self._supervisor: asyncio.Task[None] | None = None
        self._active: set[asyncio.Task[None]] = set()
        self._stopping = False

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def pending_wakes(self) -> int:
        return int(self._wake_event.is_set())

    async def start(self) -> None:
        """Recover stale ownership and start one retained supervisor task."""
        if self._supervisor is not None and not self._supervisor.done():
            return
        self._stopping = False
        self.store.recover_expired_leases()
        self._wake_event.set()
        self._supervisor = asyncio.create_task(
            self._run(), name=f"job-dispatcher-{self._dispatcher_id}"
        )
        await asyncio.sleep(0)

    def wake(self) -> None:
        """Coalesce any number of enqueue notifications into one wake-up."""
        if not self._stopping:
            self._wake_event.set()

    async def stop(self) -> None:
        """Stop claiming new jobs and allow every owned runner to finish."""
        self._stopping = True
        self._wake_event.set()
        supervisor = self._supervisor
        if supervisor is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(supervisor), timeout=self.shutdown_timeout
                )
            except TimeoutError:
                supervisor.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await supervisor
        self._supervisor = None

    async def _run(self) -> None:
        try:
            while True:
                self._wake_event.clear()
                self.store.recover_expired_leases()
                self._claim_available()
                if self._stopping:
                    break
                if len(self._active) >= self.concurrency:
                    await self._wait_for_activity()
                else:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._wake_event.wait(), timeout=self.poll_interval
                        )
            if self._active:
                await asyncio.gather(*tuple(self._active), return_exceptions=True)
        except asyncio.CancelledError:
            for task in tuple(self._active):
                task.cancel()
            if self._active:
                await asyncio.gather(*tuple(self._active), return_exceptions=True)
            raise

    def _claim_available(self) -> None:
        while not self._stopping and len(self._active) < self.concurrency:
            owner = f"{self._dispatcher_id}:{uuid.uuid4().hex}"
            claimed = self.store.claim_next_job(owner, lease_seconds=self.lease_seconds)
            if claimed is None:
                return
            task = asyncio.create_task(
                self._execute(claimed["id"], owner),
                name=f"pipeline-{claimed['id']}",
            )
            self._active.add(task)
            task.add_done_callback(self._runner_done)

    async def _execute(self, job_id: str, owner: str) -> None:
        runner = asyncio.create_task(self.runner_factory().run(job_id, owner))
        heartbeat = asyncio.create_task(self._heartbeat(job_id, owner))
        try:
            done, _ = await asyncio.wait(
                {runner, heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
            if runner in done:
                await runner
                return
            lease_retained = await heartbeat
            if not lease_retained:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runner
        except asyncio.CancelledError:
            runner.cancel()
            heartbeat.cancel()
            await asyncio.gather(runner, heartbeat, return_exceptions=True)
            self.store.release_job_lease(job_id, owner)
            raise
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            if not runner.done():
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runner

    async def _heartbeat(self, job_id: str, owner: str) -> bool:
        interval = self.lease_seconds / 3
        while True:
            await asyncio.sleep(interval)
            if not self.store.renew_lease(
                job_id, owner, lease_seconds=self.lease_seconds
            ):
                return False

    def _runner_done(self, task: asyncio.Task[None]) -> None:
        self._active.discard(task)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.exception()
        self._wake_event.set()

    async def _wait_for_activity(self) -> None:
        wake = asyncio.create_task(self._wake_event.wait())
        watched: set[asyncio.Task[Any]] = {wake, *self._active}
        done, _ = await asyncio.wait(
            watched,
            timeout=self.poll_interval,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if wake not in done:
            wake.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wake
