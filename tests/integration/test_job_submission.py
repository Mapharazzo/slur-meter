import asyncio

import pytest

from api.database import OperationStore
from api.pipeline import PipelineRunner, UnavailablePipelineServices


@pytest.fixture
def anyio_backend():
    return "asyncio"


class RecordingDispatcher:
    def __init__(self):
        self.wakes = 0
        self.starts = 0
        self.stops = 0

    async def start(self):
        self.starts += 1

    def wake(self):
        self.wakes += 1

    async def stop(self):
        self.stops += 1


@pytest.mark.anyio
async def test_generation_submission_only_durably_enqueues_and_wakes(tmp_path, monkeypatch):
    from api import main

    store = OperationStore(tmp_path / "route.db")
    store.initialize()
    dispatcher = RecordingDispatcher()
    monkeypatch.setattr(main, "operation_store", store, raising=False)
    monkeypatch.setattr(main, "job_dispatcher", dispatcher, raising=False)
    monkeypatch.setattr(
        asyncio,
        "create_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("route created a pipeline task")
        ),
    )

    response = await main.submit_job(main.SubmitRequest(imdb_id="tt0110912"))

    assert response["state"] == "queued"
    assert response["source_imdb_id"] == "tt0110912"
    assert store.get_job(response["id"])["state"] == "queued"
    assert dispatcher.wakes == 1


@pytest.mark.anyio
async def test_app_lifecycle_starts_and_stops_dispatcher(monkeypatch):
    from api import main

    initialized = []
    dispatcher = RecordingDispatcher()

    class Store:
        def initialize(self):
            initialized.append(True)

    monkeypatch.setattr(main, "operation_store", Store(), raising=False)
    monkeypatch.setattr(main, "job_dispatcher", dispatcher, raising=False)

    await main.startup()
    await main.shutdown()

    assert initialized == [True]
    assert dispatcher.starts == 1
    assert dispatcher.stops == 1


@pytest.mark.anyio
async def test_default_unavailable_services_fail_durably_without_external_calls(tmp_path):
    store = OperationStore(tmp_path / "unavailable.db")
    store.initialize()
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.claim_next_job("worker-a", lease_seconds=30)

    await PipelineRunner(
        store,
        UnavailablePipelineServices(),
        stages=("input_resolution",),
    ).run(job["id"], "worker-a")

    detail = store.get_job_detail(job["id"])
    assert detail["run"]["state"] == "needs_attention"
    assert detail["run"]["safe_error"]["code"] == "pipeline_services_unavailable"
