import asyncio

import pytest

from api.artifacts import ArtifactManager
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
    monkeypatch.setattr(
        main, "pipeline_services_factory", lambda: object(), raising=False
    )

    await main.startup()
    await main.shutdown()

    assert initialized == [True]
    assert dispatcher.starts == 1
    assert dispatcher.stops == 1


@pytest.mark.anyio
async def test_startup_service_factory_failure_happens_before_dispatcher_start(
    monkeypatch,
):
    from api import main

    initialized = []
    dispatcher = RecordingDispatcher()

    class Store:
        def initialize(self):
            initialized.append(True)

    monkeypatch.setattr(main, "operation_store", Store(), raising=False)
    monkeypatch.setattr(main, "job_dispatcher", dispatcher, raising=False)
    monkeypatch.setattr(
        main,
        "pipeline_services_factory",
        lambda: (_ for _ in ()).throw(RuntimeError("invalid services")),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="invalid services"):
        await main.startup()

    assert initialized == [True]
    assert dispatcher.starts == 0


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


@pytest.mark.anyio
async def test_versioned_video_frames_and_publish_routes_resolve_current_artifacts(
    tmp_path, monkeypatch
):
    from api import main

    store = OperationStore(tmp_path / "routes.db")
    store.initialize()
    job, _ = store.create_or_get_active_job("tt0110912", "", "Fixture Movie")
    artifacts = ArtifactManager(tmp_path / "output")

    render = artifacts.new_staging_directory(job["id"], "composite")
    segment = render / "intro_hold"
    segment.mkdir()
    (segment / "00000.png").write_bytes(b"png")
    composite = artifacts.promote_directory(
        job["id"],
        "composite",
        render,
        final_name="render",
        details={
            "timing": {"intro_hold": {"num_frames": 1, "start_time": 0.0}},
            "total_frames": 1,
        },
    )
    staged_video = artifacts.new_staging_file(job["id"], "encode", suffix=".mp4")
    staged_video.write_bytes(b"video")
    encoded = artifacts.promote_file(
        job["id"],
        "encode",
        staged_video,
        final_name="final.mp4",
        artifact_kind="file",
    )

    for ordinal, (name, manifest) in enumerate(
        (("composite", composite), ("encode", encoded)), 1
    ):
        store.ensure_stage(job["id"], name, ordinal=ordinal)
        if ordinal == 1:
            store.claim_next_job("worker", lease_seconds=30)
        store.transition_stage(job["id"], name, "queued", lease_owner="worker")
        store.transition_stage(job["id"], name, "running", lease_owner="worker")
        store.transition_stage(
            job["id"],
            name,
            "completed",
            output_manifest=manifest,
            lease_owner="worker",
        )
    store.transition_job(
        job["id"], "completed", expected_state="running", lease_owner="worker"
    )
    store.compatibility_update_job(job["id"], analysis_json={"summary": {}})

    services = type("Services", (), {"artifacts": artifacts})()
    monkeypatch.setattr(main, "operation_store", store, raising=False)
    monkeypatch.setattr(main, "_pipeline_services", services, raising=False)

    video_response = await main.serve_video("tt0110912")
    assert video_response.path == str(artifacts.artifact_path(encoded))
    segment_info = await main.serve_segment_info("tt0110912", "intro_hold")
    assert segment_info["frame_count"] == 1
    assert segment_info["timing"]["start_time"] == 0.0
    frame_response = await main.serve_frame("tt0110912", "intro_hold", 0)
    assert frame_response.path == str(
        artifacts.artifact_path(composite) / "intro_hold" / "00000.png"
    )

    published = []

    async def fake_publish(job_id, platform, video_path, metadata):
        published.append((job_id, platform, video_path, metadata))

    monkeypatch.setattr(main, "_do_publish", fake_publish)
    result = await main.publish_video("tt0110912", "youtube")
    await asyncio.sleep(0)

    assert result["status"] == "publishing"
    assert published[0][2] == artifacts.artifact_path(encoded)
    assert store.list_releases(job["id"])[0]["status"] == "pending"
