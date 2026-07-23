import asyncio
import hashlib
import io
import subprocess
import threading
from pathlib import Path

import pytest
import requests
from PIL import Image
from requests import Timeout

from api.artifacts import ArtifactManager
from api.database import OperationStore
from api.dispatcher import JobDispatcher
from api.domain import StageState
from api.errors import AttentionRequired, TransientFailure
from api.pipeline import GenerationPipelineServices, PipelineRunner
from api.settings import Settings
from api.subtitles import SubtitleService
from src.audio.layers import AudioLayer
from src.audio.mixer import AudioMixer
from src.audio.pipeline import AudioPipeline
from src.audio.providers import (
    AudioProvider,
    ElevenLabsProvider,
    SilenceProvider,
    _cache_key,
)
from src.data.movie_metadata import MovieMetadataClient, MovieMetadataResult
from src.data.opensubtitles import SubtitleCache, SubtitleResult


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeMetadataClient:
    def fetch(self, imdb_id):
        return MovieMetadataResult(
            configured=True,
            metadata={
                "Title": "Fixture Movie",
                "Year": "1994",
                "Runtime": "2 min",
                "Director": "Fixture Director",
            },
            poster_bytes=None,
        )


class FakePlotter:
    W = 4
    H = 3

    def __init__(self, _config):
        pass

    def generate_frames(
        self,
        _binned,
        output_dir,
        n_frames=2,
        runtime_min=None,
        progress_cb=None,
    ):
        paths = []
        for index in range(n_frames):
            path = Path(output_dir) / f"frame_{index:05d}.png"
            Image.new("RGB", (self.W, self.H), (index, 0, 0)).save(path)
            paths.append(path)
            if progress_cb:
                progress_cb("Generating", index + 1, n_frames)
        return paths


class FakeCompositor:
    def __init__(self, config):
        self.width, self.height = config["video"]["resolution"]
        self.fps = config["video"]["fps"]

    def render_all(self, output_dir, progress_cb=None, **_kwargs):
        root = Path(output_dir)
        concat = root / "concat"
        concat.mkdir(parents=True)
        timing = {}
        global_index = 0
        for name in ("intro_hold", "intro_transition", "graph", "verdict"):
            segment = root / name
            segment.mkdir()
            path = segment / "00000.png"
            image = Image.new("RGB", (self.width, self.height), (global_index, 0, 0))
            image.save(path)
            image.save(concat / f"{global_index:05d}.png")
            timing[name] = {
                "start_frame": global_index,
                "end_frame": global_index,
                "start_time": global_index / self.fps,
                "end_time": (global_index + 1) / self.fps,
                "num_frames": 1,
            }
            if progress_cb:
                progress_cb(name, 1, 1)
            global_index += 1
        return {
            "segments": {name: {"num_frames": 1} for name in timing},
            "timing": timing,
            "total_frames": global_index,
            "total_duration": global_index / self.fps,
        }


class FakeAudioPipeline:
    def __init__(self, _config, audio_dir, segment_timing, warning_callback=None):
        self.audio_dir = Path(audio_dir)
        self.timing = segment_timing
        self.warning_callback = warning_callback
        self.timeline = type("Timeline", (), {"layers": [object(), object()]})()

    def build_layers(self, *_args):
        return self.timeline

    def generate_all(self, progress_cb=None, cancel_requested=None):
        assert cancel_requested is not None and not cancel_requested()
        if progress_cb:
            progress_cb(1, 2)
            progress_cb(2, 2)

    def mix(self, output_path, cancel_requested=None):
        assert cancel_requested is not None and not cancel_requested()
        Path(output_path).write_bytes(b"mixed-audio")
        return Path(output_path)


class FakeEncoder:
    def encode(self, frames, audio, output, on_progress, cancel_requested):
        assert Path(frames).is_dir()
        assert Path(audio).read_bytes() == b"mixed-audio"
        assert ".staging" in Path(output).parts
        assert ".partial" in Path(output).name
        assert not cancel_requested()
        on_progress(2)
        on_progress(4)
        Path(output).write_bytes(b"final-video")
        return Path(output)


def _config():
    return {
        "categories": {"hard": ["bad"], "soft": [], "f_bombs": []},
        "video": {
            "resolution": [4, 3],
            "fps": 2,
            "graph_frames": 2,
            "colors": {},
            "encoding": {"preset": "medium"},
        },
        "audio": {
            "intro_tts": {"enabled": False},
            "outro_tts": {"enabled": False},
        },
    }


def _selected_subtitle(store, job_id, path):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    store.record_candidate(
        job_id,
        "fixture",
        "fixture-1",
        source_type="upload",
        status="selected",
        content_hash=digest,
        artifact_path=str(path),
        selection_method="automatic",
    )


class FakeSubtitleClient:
    def __init__(
        self,
        results,
        payloads,
        *,
        before_download=None,
        callback_before_write=False,
    ):
        self.results = results
        self.payloads = payloads
        self.before_download = before_download
        self.callback_before_write = callback_before_write

    def search(self, **_kwargs):
        return self.results

    def download(self, file_id, destination):
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.before_download is not None and self.callback_before_write:
            self.before_download()
        path.write_bytes(self.payloads[file_id])
        if self.before_download is not None and not self.callback_before_write:
            self.before_download()
        return path


SHORT_PROVIDER_SRT = b"1\n00:00:01,000 --> 00:00:02,000\nToo short\n"
VALID_PROVIDER_SRT = (
    b"1\n00:00:01,000 --> 00:00:02,000\nHello\n\n"
    b"2\n01:20:00,000 --> 01:25:00,000\nBye\n"
)
REPLACEMENT_PROVIDER_SRT = VALID_PROVIDER_SRT.replace(b"Hello", b"Replacement")
CP1252_PROVIDER_SRT = (
    "1\n00:00:01,000 --> 00:00:02,000\ncaf\N{LATIN SMALL LETTER E WITH ACUTE}\n\n"
    "2\n01:20:00,000 --> 01:25:00,000\nBye\n"
).encode("cp1252")


def _expire_and_reclaim(store, job_id, replacement_owner):
    with store._mutation() as connection:
        connection.execute(
            "UPDATE job_runs SET lease_expires_at = "
            "'2000-01-01T00:00:00+00:00' WHERE id = ?",
            (job_id,),
        )
    store.recover_expired_leases()
    assert store.claim_next_job(replacement_owner, lease_seconds=30) is not None


def _lease_cancelled(store, job_id, owner):
    return not store.renew_lease(job_id, owner, lease_seconds=30)


@pytest.mark.anyio
async def test_real_generation_handlers_persist_actual_progress_children_and_manifests(
    tmp_path,
):
    store = OperationStore(tmp_path / "operations.db")
    store.initialize()
    job, _ = store.create_or_get_active_job("tt0110912", "", "Fixture Movie")
    subtitle = tmp_path / "fixture.srt"
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nA bad line.\n",
        encoding="utf-8",
    )
    _selected_subtitle(store, job["id"], subtitle)
    store.claim_next_job("worker", lease_seconds=30)
    settings = Settings(
        base_dir=tmp_path,
        output_dir=tmp_path / "output",
        results_dir=tmp_path / "results",
    )
    artifacts = ArtifactManager(
        settings.output_dir,
        probe_duration=lambda path: 2.0 if path.suffix == ".mp4" else 0.5,
    )
    services = GenerationPipelineServices(
        store,
        settings,
        config=_config(),
        artifacts=artifacts,
        metadata_client=FakeMetadataClient(),
        plotter_factory=FakePlotter,
        compositor_factory=FakeCompositor,
        audio_pipeline_factory=FakeAudioPipeline,
        encoder=FakeEncoder(),
    )
    stages = ("metadata", "analysis", "graph", "composite", "audio", "encode")

    try:
        await asyncio.wait_for(
            PipelineRunner(
                store,
                services,
                stages=stages,
                sleep=asyncio.sleep,
                settings=settings,
            ).run(job["id"], "worker"),
            timeout=10,
        )
    except TimeoutError:
        pytest.fail(f"generation runner stalled: {store.get_job_detail(job['id'])!r}")

    detail = store.get_job_detail(job["id"])
    assert detail["run"]["state"] == "completed", detail
    parents = {stage["name"]: stage for stage in detail["stages"]}
    assert parents["graph"]["progress"] == {
        "numerator": 2,
        "denominator": 2,
        "unit": "frames",
    }
    assert parents["encode"]["progress"] == {
        "numerator": 4,
        "denominator": 4,
        "unit": "frames",
    }
    for name in ("intro_hold", "intro_transition", "graph", "verdict"):
        child = parents[f"composite.{name}"]
        assert child["parent_stage_id"] == parents["composite"]["id"]
        assert child["state"] == "completed"
        assert child["progress"] == {
            "numerator": 1,
            "denominator": 1,
            "unit": "frames",
        }
        manifest = child["output_manifest"]
        assert manifest["version"] == parents["composite"]["output_manifest"]["version"]
        assert manifest["artifact"]["width"] == 4
        assert manifest["artifact"]["height"] == 3
        assert manifest["artifact"]["prefix"] == ""
        assert manifest["artifact"]["digits"] == 5
        assert manifest["artifact"]["sha256"]
        assert manifest["input_hashes"]
        assert manifest["config_hash"]
        assert services.validate_stage(f"composite.{name}", job["id"], manifest)
    events = detail["events"]
    intro_progress = next(
        event["id"]
        for event in events
        if event["stage_id"] == parents["composite.intro_hold"]["id"]
        and event["type"] == "stage_progress"
    )
    transition_start = next(
        event["id"]
        for event in events
        if event["message"].startswith(
            "Stage composite.intro_transition moved from pending to queued"
        )
    )
    assert intro_progress < transition_start
    encoded = artifacts.artifact_path(parents["encode"]["output_manifest"])
    assert encoded.read_bytes() == b"final-video"
    graph_manifest = parents["graph"]["output_manifest"]
    graph_artifact = artifacts.artifact_path(graph_manifest)
    assert graph_manifest["details"]["frames_directory"] == "frames"
    assert graph_manifest["details"]["preview_file"] == "preview.png"
    assert (graph_artifact / "preview.png").is_file()
    assert len(list((graph_artifact / "frames").glob("frame_*.png"))) == 2
    assert not (settings.output_dir / job["id"] / "preview.png").exists()
    assert not (settings.output_dir / job["id"] / "final.mp4").exists()
    assert all(
        services.validate_stage(stage, job["id"], parents[stage]["output_manifest"])
        for stage in stages
    )

    changed = GenerationPipelineServices(
        store,
        settings,
        config={**_config(), "categories": {"hard": ["changed"]}},
        artifacts=artifacts,
        metadata_client=FakeMetadataClient(),
        plotter_factory=FakePlotter,
        compositor_factory=FakeCompositor,
        audio_pipeline_factory=FakeAudioPipeline,
        encoder=FakeEncoder(),
    )
    assert not changed.validate_stage(
        "analysis", job["id"], parents["analysis"]["output_manifest"]
    )
    wrong_stage = dict(parents["analysis"]["output_manifest"])
    wrong_stage["stage"] = "graph"
    assert not services.validate_stage("analysis", job["id"], wrong_stage)
    assert not services.validate_stage(
        "analysis",
        "job_00000000000000000000000000000000",
        parents["analysis"]["output_manifest"],
    )


def test_audio_manifest_reuse_tracks_same_path_file_provider_content(tmp_path):
    store = OperationStore(tmp_path / "audio-provenance.db")
    store.initialize()
    job, _ = store.create_or_get_active_job("tt0110912", "", "Fixture Movie")
    source = tmp_path / "intro-source.mp3"
    source.write_bytes(b"original-local-audio")
    config = {
        "video": {"fps": 30},
        "audio": {
            "intro_tts": {
                "enabled": True,
                "provider": "file",
                "provider_config": {"path": str(source)},
            },
            "outro_tts": {
                "enabled": True,
                "provider": "file",
                "provider_config": {"path": str(source)},
            },
            "background_music": {
                "enabled": True,
                "provider": "file",
                "provider_config": {"path": str(source)},
            },
            "verdict_sfx": {
                "enabled": True,
                "provider": "file",
                "provider_config": {"path": str(source)},
                "rating": {
                    "provider": "file",
                    "provider_config": {"path": str(source)},
                },
            },
        },
    }
    artifacts = ArtifactManager(tmp_path / "output")
    services = GenerationPipelineServices(
        store,
        Settings(base_dir=tmp_path, output_dir=tmp_path / "output"),
        config=config,
        artifacts=artifacts,
        metadata_client=FakeMetadataClient(),
        encoder=FakeEncoder(),
    )
    services._artifact_hash = lambda _job_id, stage: f"{stage}-hash"
    inputs = services._input_hashes("audio", job["id"])
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    assert {
        key: value for key, value in inputs.items() if key.startswith("file_provider:")
    } == {
        "file_provider:intro_tts": source_hash,
        "file_provider:outro_tts": source_hash,
        "file_provider:background_music": source_hash,
        "file_provider:verdict_sfx": source_hash,
        "file_provider:verdict_sfx.rating": source_hash,
    }

    staged = artifacts.new_staging_file(job["id"], "audio", suffix=".m4a")
    staged.write_bytes(b"mixed-output")
    manifest = artifacts.promote_file(
        job["id"],
        "audio",
        staged,
        final_name="mixed.m4a",
        artifact_kind="file",
        input_hashes=inputs,
        config_hash=services._config_hash("audio"),
    )
    assert services.validate_stage("audio", job["id"], manifest)

    source.write_bytes(b"changed-at-the-same-path")
    assert not services.validate_stage("audio", job["id"], manifest)
    source.unlink()
    assert not services.validate_stage("audio", job["id"], manifest)


def test_metadata_client_distinguishes_optional_absence_and_transient_failure():
    class NeverSession:
        def get(self, *_args, **_kwargs):
            raise AssertionError("network should not be called")

    missing = MovieMetadataClient(tmdb_token=None, session=NeverSession()).fetch(
        "tt0110912"
    )
    assert missing.configured is False
    assert missing.metadata == {}
    assert missing.warnings

    class TimeoutSession:
        def get(self, *_args, **_kwargs):
            raise Timeout("temporary")

    with pytest.raises(TransientFailure):
        MovieMetadataClient(tmdb_token="configured", session=TimeoutSession()).fetch(
            "tt0110912"
        )

    class BrokenBodySession:
        def get(self, *_args, **_kwargs):
            raise requests.exceptions.ChunkedEncodingError("truncated response")

    with pytest.raises(TransientFailure):
        MovieMetadataClient(tmdb_token="configured", session=BrokenBodySession()).fetch(
            "tt0110912"
        )


def test_metadata_client_rejects_omdb_error_flags_and_invalid_poster_bytes():
    class Response:
        status_code = 200

        def __init__(self, payload=None, content=b""):
            self.payload = payload or {}
            self.content = content

        def json(self):
            return self.payload

    base = [
        Response({"movie_results": [{"id": 1, "title": "Fixture"}]}),
        Response({"title": "Fixture", "poster_path": "/poster.jpg"}),
        Response({"crew": [], "cast": []}),
    ]

    class InvalidOmdbSession:
        def __init__(self):
            self.responses = [
                *base,
                Response({"Response": "False", "Error": "Invalid API key!"}),
                Response(content=b"not-an-image"),
            ]

        def get(self, *_args, **_kwargs):
            return self.responses.pop(0)

    with pytest.raises(AttentionRequired) as captured:
        MovieMetadataClient(
            tmdb_token="configured",
            omdb_api_key="bad",
            session=InvalidOmdbSession(),
        ).fetch("tt0110912")
    assert captured.value.code == "metadata_request_rejected"

    class InvalidPosterSession:
        def __init__(self):
            self.responses = [*base, Response(content=b"not-an-image")]

        def get(self, *_args, **_kwargs):
            return self.responses.pop(0)

    result = MovieMetadataClient(
        tmdb_token="configured", session=InvalidPosterSession()
    ).fetch("tt0110912")
    assert result.poster_bytes is None
    assert result.warnings == ("Movie poster response was not a valid image.",)


def test_ffprobe_failure_is_emitted_as_warning_instead_of_swallowed(
    tmp_path, monkeypatch
):
    warnings = []
    timing = {
        "verdict": {
            "start_frame": 0,
            "end_frame": 1,
            "start_time": 0.0,
            "end_time": 1.0,
            "num_frames": 2,
        }
    }

    class FailedProbe:
        returncode = 1
        stdout = io.StringIO("")

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    pipeline = AudioPipeline(
        {"video": {"fps": 2}},
        tmp_path / "audio",
        timing,
        warning_callback=warnings.append,
        probe_popen=lambda *_args, **_kwargs: FailedProbe(),
    )
    pipeline.timeline.add(
        AudioLayer(
            name="voice",
            role="tts",
            duck_others=True,
            provider_name="fixture",
            text="hello",
        )
    )

    class Provider:
        def generate(self, text, output_path, **kwargs):
            output_path.write_bytes(b"voice")
            return output_path

    monkeypatch.setattr("src.audio.pipeline.get_provider", lambda *_args: Provider())
    pipeline.generate_all()

    assert warnings == [
        "Could not determine duration for audio layer voice (CalledProcessError)."
    ]


def test_audio_pipeline_cancellation_terminates_hung_duration_probe(
    tmp_path, monkeypatch
):
    timing = {
        "verdict": {
            "start_frame": 0,
            "end_frame": 1,
            "start_time": 0.0,
            "end_time": 1.0,
            "num_frames": 2,
        }
    }
    probe = None

    class HangingProbe:
        returncode = None
        stdout = io.StringIO("")
        terminated = False
        killed = False

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.killed = True
            self.returncode = -9

    def probe_popen(*_args, **_kwargs):
        nonlocal probe
        probe = HangingProbe()
        return probe

    pipeline = AudioPipeline(
        {"video": {"fps": 2}},
        tmp_path / "audio",
        timing,
        probe_popen=probe_popen,
    )
    pipeline.timeline.add(
        AudioLayer(
            name="voice",
            role="tts",
            duck_others=True,
            provider_name="fixture",
            text="hello",
        )
    )

    class Provider:
        def generate(self, text, output_path, **kwargs):
            assert kwargs["cancel_requested"] is not None
            output_path.write_bytes(b"voice")
            return output_path

    monkeypatch.setattr("src.audio.pipeline.get_provider", lambda *_args: Provider())

    with pytest.raises(asyncio.CancelledError):
        pipeline.generate_all(cancel_requested=lambda: probe is not None)

    assert probe is not None and probe.terminated


@pytest.mark.anyio
async def test_blocking_generation_stage_keeps_event_loop_and_lease_heartbeat_alive(
    tmp_path,
):
    store = OperationStore(tmp_path / "heartbeat.db")
    store.initialize()
    job, _ = store.create_or_get_active_job("tt0110912", "", "Fixture Movie")
    settings = Settings(base_dir=tmp_path, output_dir=tmp_path / "output")
    started = threading.Event()
    release = threading.Event()

    class BlockingMetadataClient:
        def fetch(self, _imdb_id):
            started.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test did not release blocked metadata")
            return MovieMetadataResult(configured=True, metadata={"Title": "Fixture"})

    def runner_factory():
        services = GenerationPipelineServices(
            store,
            settings,
            config=_config(),
            artifacts=ArtifactManager(settings.output_dir),
            metadata_client=BlockingMetadataClient(),
            plotter_factory=FakePlotter,
            compositor_factory=FakeCompositor,
            audio_pipeline_factory=FakeAudioPipeline,
            encoder=FakeEncoder(),
        )
        return PipelineRunner(
            store,
            services,
            stages=("metadata",),
            lease_seconds=0.6,
            settings=settings,
        )

    dispatcher = JobDispatcher(
        store,
        runner_factory,
        poll_interval=0.02,
        lease_seconds=0.6,
        shutdown_timeout=1,
    )
    await dispatcher.start()
    try:
        for _ in range(50):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        boundary = store.get_job_detail(job["id"])
        metadata_stage = next(
            stage for stage in boundary["stages"] if stage["name"] == "metadata"
        )
        assert boundary["run"]["state"] == "running"
        assert metadata_stage["state"] == "running"
        first_expiry = boundary["run"]["lease_expires_at"]
        assert first_expiry is not None
        ticks = 0
        second_expiry = first_expiry
        for _ in range(50):
            await asyncio.sleep(0.01)
            ticks += 1
            second_expiry = store.get_job(job["id"])["lease_expires_at"]
            assert second_expiry is not None
            if second_expiry > first_expiry:
                break
        assert ticks > 0
        assert second_expiry > first_expiry
        release.set()
        for _ in range(100):
            if store.get_job(job["id"])["state"] == "completed":
                break
            await asyncio.sleep(0.01)
        assert store.get_job(job["id"])["state"] == "completed"
    finally:
        release.set()
        await dispatcher.stop()


@pytest.mark.anyio
async def test_cancelled_blocking_stage_cannot_publish_after_its_task_is_cancelled(
    tmp_path,
):
    store = OperationStore(tmp_path / "cancelled-publication.db")
    store.initialize()
    job, _ = store.create_or_get_active_job("tt0110912", "", "Fixture Movie")
    store.claim_next_job("worker", lease_seconds=30)
    settings = Settings(base_dir=tmp_path, output_dir=tmp_path / "output")
    artifacts = ArtifactManager(settings.output_dir)
    started = threading.Event()
    release = threading.Event()

    class BlockingMetadataClient:
        def fetch(self, _imdb_id):
            started.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test did not release blocked metadata")
            return MovieMetadataResult(configured=True, metadata={"Title": "Fixture"})

    services = GenerationPipelineServices(
        store,
        settings,
        config=_config(),
        artifacts=artifacts,
        metadata_client=BlockingMetadataClient(),
        plotter_factory=FakePlotter,
        compositor_factory=FakeCompositor,
        audio_pipeline_factory=FakeAudioPipeline,
        encoder=FakeEncoder(),
    )

    def progress(_numerator, _denominator, _unit):
        return None

    progress.lease_owner = "worker"
    progress.lease_seconds = 30
    task = asyncio.create_task(services.run_stage("metadata", job["id"], progress))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(FileNotFoundError):
            artifacts.manifest_path(job["id"], "metadata")
    finally:
        release.set()


@pytest.mark.anyio
async def test_stage_that_lost_its_lease_cannot_publish_an_artifact(tmp_path):
    store = OperationStore(tmp_path / "stale-publication.db")
    store.initialize()
    job, _ = store.create_or_get_active_job("tt0110912", "", "Fixture Movie")
    store.claim_next_job("stale-worker", lease_seconds=30)
    with store._mutation() as connection:
        connection.execute(
            "UPDATE job_runs SET lease_expires_at = "
            "'2000-01-01T00:00:00+00:00' WHERE id = ?",
            (job["id"],),
        )
    store.recover_expired_leases()
    assert store.claim_next_job("replacement-worker", lease_seconds=30) is not None
    settings = Settings(base_dir=tmp_path, output_dir=tmp_path / "output")
    artifacts = ArtifactManager(settings.output_dir)
    services = GenerationPipelineServices(
        store,
        settings,
        config=_config(),
        artifacts=artifacts,
        metadata_client=FakeMetadataClient(),
        plotter_factory=FakePlotter,
        compositor_factory=FakeCompositor,
        audio_pipeline_factory=FakeAudioPipeline,
        encoder=FakeEncoder(),
    )

    def progress(_numerator, _denominator, _unit):
        return None

    progress.lease_owner = "stale-worker"
    progress.lease_seconds = 30
    with pytest.raises(asyncio.CancelledError):
        await services.run_stage("metadata", job["id"], progress)
    with pytest.raises(FileNotFoundError):
        artifacts.manifest_path(job["id"], "metadata")


@pytest.mark.anyio
async def test_real_subtitle_service_rejects_first_candidate_then_selects_second(
    tmp_path,
):
    store = OperationStore(tmp_path / "real-subtitle.db")
    store.initialize()
    job, _ = store.create_or_get_active_job("tt0110912", "pulp fiction", "Pulp Fiction")
    store.claim_next_job("subtitle-worker", lease_seconds=30)
    settings = Settings(
        base_dir=tmp_path,
        output_dir=tmp_path / "output",
        results_dir=tmp_path / "results",
    )
    results = [
        SubtitleResult(
            str(index),
            f"{index}.srt",
            "Pulp Fiction",
            "1994",
            "en",
            None,
            "tt0110912",
            runtime_seconds=100 * 60,
        )
        for index in (1, 2)
    ]
    client = FakeSubtitleClient(
        results,
        {"1": SHORT_PROVIDER_SRT, "2": VALID_PROVIDER_SRT},
    )
    service = SubtitleService(
        store,
        client,
        SubtitleCache(settings.results_dir),
        settings,
    )
    services = GenerationPipelineServices(
        store,
        settings,
        config=_config(),
        artifacts=ArtifactManager(settings.output_dir),
        metadata_client=FakeMetadataClient(),
        subtitle_service=service,
        encoder=FakeEncoder(),
    )

    await PipelineRunner(
        store,
        services,
        stages=("subtitle_discovery", "subtitle_selection"),
        settings=settings,
    ).run(job["id"], "subtitle-worker")

    detail = store.get_job_detail(job["id"])
    candidates = detail["candidates"]
    candidate_attempts = [
        attempt for attempt in detail["attempts"] if attempt["candidate_id"]
    ]
    selection = next(
        stage for stage in detail["stages"] if stage["name"] == "subtitle_selection"
    )
    assert [candidate["status"] for candidate in candidates] == [
        "rejected",
        "selected",
    ], detail
    assert [attempt["outcome"] for attempt in candidate_attempts] == [
        "rejected",
        "completed",
    ]
    assert detail["run"]["state"] == "completed"
    assert selection["state"] == "completed"
    assert (
        selection["output_manifest"]["artifact"]["candidate_id"] == candidates[1]["id"]
    )
    assert (
        len(
            [
                event
                for event in detail["events"]
                if event["type"] == "subtitle_selected"
            ]
        )
        == 1
    )


@pytest.mark.anyio
async def test_real_subtitle_service_stops_mutating_after_lease_reclamation(tmp_path):
    store = OperationStore(tmp_path / "stale-subtitle.db")
    store.initialize()
    job, _ = store.create_or_get_active_job("tt0110912", "pulp fiction", "Pulp Fiction")
    store.claim_next_job("stale-subtitle-worker", lease_seconds=30)
    settings = Settings(
        base_dir=tmp_path,
        output_dir=tmp_path / "output",
        results_dir=tmp_path / "results",
    )
    snapshot = None

    def reclaim_lease():
        nonlocal snapshot
        with store._mutation() as connection:
            connection.execute(
                "UPDATE job_runs SET lease_expires_at = "
                "'2000-01-01T00:00:00+00:00' WHERE id = ?",
                (job["id"],),
            )
        store.recover_expired_leases()
        assert (
            store.claim_next_job("replacement-subtitle-worker", lease_seconds=30)
            is not None
        )
        snapshot = store.get_job_detail(job["id"])

    result = SubtitleResult(
        "1",
        "one.srt",
        "Pulp Fiction",
        "1994",
        "en",
        None,
        "tt0110912",
        runtime_seconds=100 * 60,
    )
    client = FakeSubtitleClient(
        [result],
        {"1": VALID_PROVIDER_SRT},
        before_download=reclaim_lease,
    )
    services = GenerationPipelineServices(
        store,
        settings,
        config=_config(),
        artifacts=ArtifactManager(settings.output_dir),
        metadata_client=FakeMetadataClient(),
        subtitle_service=SubtitleService(
            store,
            client,
            SubtitleCache(settings.results_dir),
            settings,
        ),
        encoder=FakeEncoder(),
    )

    with pytest.raises(asyncio.CancelledError):
        await PipelineRunner(
            store,
            services,
            stages=("subtitle_discovery", "subtitle_selection"),
            settings=settings,
        ).run(job["id"], "stale-subtitle-worker")

    assert snapshot is not None
    after = store.get_job_detail(job["id"])
    assert after["run"] == snapshot["run"]
    for key in ("stages", "attempts", "candidates", "events", "decisions"):
        assert after[key] == snapshot[key]


def test_reclaimed_subtitle_download_cannot_overwrite_replacement_candidate_bytes(
    tmp_path,
):
    store = OperationStore(tmp_path / "download-race.db")
    store.initialize()
    job, _ = store.create_or_get_active_job(
        "tt0110912", "pulp fiction", "Pulp Fiction"
    )
    stale_owner = "stale-download-worker"
    replacement_owner = "replacement-download-worker"
    store.claim_next_job(stale_owner, lease_seconds=30)
    settings = Settings(base_dir=tmp_path, results_dir=tmp_path / "results")
    cache = SubtitleCache(settings.results_dir)
    result = SubtitleResult(
        "1",
        "one.srt",
        "Pulp Fiction",
        "1994",
        "en",
        None,
        "tt0110912",
        runtime_seconds=100 * 60,
    )
    replacement_service = SubtitleService(
        store,
        FakeSubtitleClient([result], {"1": REPLACEMENT_PROVIDER_SRT}),
        cache,
        settings,
    )

    def replace_after_reclaim():
        _expire_and_reclaim(store, job["id"], replacement_owner)
        replacement_service.select(
            job["id"],
            lease_owner=replacement_owner,
            cancel_requested=lambda: _lease_cancelled(
                store, job["id"], replacement_owner
            ),
        )

    stale_service = SubtitleService(
        store,
        FakeSubtitleClient(
            [result],
            {"1": VALID_PROVIDER_SRT},
            before_download=replace_after_reclaim,
            callback_before_write=True,
        ),
        cache,
        settings,
    )
    candidate = stale_service.discover(
        job["id"],
        lease_owner=stale_owner,
        cancel_requested=lambda: _lease_cancelled(store, job["id"], stale_owner),
    )[0]

    with pytest.raises(asyncio.CancelledError):
        stale_service.select(
            job["id"],
            lease_owner=stale_owner,
            cancel_requested=lambda: _lease_cancelled(
                store, job["id"], stale_owner
            ),
        )

    artifact = Path(
        store.get_candidate(candidate["id"], include_internal=True)["artifact_path"]
    )
    assert artifact.read_bytes() == REPLACEMENT_PROVIDER_SRT
    assert cache.has("tt0110912").read_bytes() == REPLACEMENT_PROVIDER_SRT
    assert not list(settings.results_dir.rglob("*.partial*"))


def test_reclaimed_normalization_cannot_overwrite_replacement_candidate_bytes(
    tmp_path, monkeypatch
):
    from api import subtitles as subtitles_module

    store = OperationStore(tmp_path / "normalization-race.db")
    store.initialize()
    job, _ = store.create_or_get_active_job(
        "tt0110912", "pulp fiction", "Pulp Fiction"
    )
    stale_owner = "stale-normalization-worker"
    replacement_owner = "replacement-normalization-worker"
    store.claim_next_job(stale_owner, lease_seconds=30)
    settings = Settings(base_dir=tmp_path, results_dir=tmp_path / "results")
    cache = SubtitleCache(settings.results_dir)
    result = SubtitleResult(
        "1",
        "one.srt",
        "Pulp Fiction",
        "1994",
        "en",
        None,
        "tt0110912",
        runtime_seconds=100 * 60,
    )
    replacement_service = SubtitleService(
        store,
        FakeSubtitleClient([result], {"1": REPLACEMENT_PROVIDER_SRT}),
        cache,
        settings,
    )
    original_write_normalized = subtitles_module._write_normalized
    reclaimed = False

    def reclaim_before_stale_normalization(path, content):
        nonlocal reclaimed
        if not reclaimed:
            reclaimed = True
            _expire_and_reclaim(store, job["id"], replacement_owner)
            replacement_service.select(
                job["id"],
                lease_owner=replacement_owner,
                cancel_requested=lambda: _lease_cancelled(
                    store, job["id"], replacement_owner
                ),
            )
        original_write_normalized(path, content)

    stale_service = SubtitleService(
        store,
        FakeSubtitleClient([result], {"1": CP1252_PROVIDER_SRT}),
        cache,
        settings,
    )
    candidate = stale_service.discover(
        job["id"],
        lease_owner=stale_owner,
        cancel_requested=lambda: _lease_cancelled(store, job["id"], stale_owner),
    )[0]
    monkeypatch.setattr(
        subtitles_module, "_write_normalized", reclaim_before_stale_normalization
    )

    with pytest.raises(asyncio.CancelledError):
        stale_service.select(
            job["id"],
            lease_owner=stale_owner,
            cancel_requested=lambda: _lease_cancelled(
                store, job["id"], stale_owner
            ),
        )

    artifact = Path(
        store.get_candidate(candidate["id"], include_internal=True)["artifact_path"]
    )
    assert artifact.read_bytes() == REPLACEMENT_PROVIDER_SRT
    assert cache.has("tt0110912").read_bytes() == REPLACEMENT_PROVIDER_SRT
    assert not list(settings.results_dir.rglob("*.partial*"))


def test_reclaimed_candidate_promotion_cannot_overwrite_replacement_bytes(
    tmp_path, monkeypatch
):
    from api import subtitles as subtitles_module

    store = OperationStore(tmp_path / "candidate-promotion-race.db")
    store.initialize()
    job, _ = store.create_or_get_active_job(
        "tt0110912", "pulp fiction", "Pulp Fiction"
    )
    stale_owner = "stale-candidate-worker"
    replacement_owner = "replacement-candidate-worker"
    store.claim_next_job(stale_owner, lease_seconds=30)
    settings = Settings(base_dir=tmp_path, results_dir=tmp_path / "results")
    cache = SubtitleCache(settings.results_dir)
    result = SubtitleResult(
        "1",
        "one.srt",
        "Pulp Fiction",
        "1994",
        "en",
        None,
        "tt0110912",
        runtime_seconds=100 * 60,
    )
    replacement_service = SubtitleService(
        store,
        FakeSubtitleClient([result], {"1": REPLACEMENT_PROVIDER_SRT}),
        cache,
        settings,
    )
    stale_service = SubtitleService(
        store,
        FakeSubtitleClient([result], {"1": VALID_PROVIDER_SRT}),
        cache,
        settings,
    )
    candidate = stale_service.discover(
        job["id"],
        lease_owner=stale_owner,
        cancel_requested=lambda: _lease_cancelled(store, job["id"], stale_owner),
    )[0]
    original_promote = subtitles_module.promote_subtitle_file
    reclaimed = False

    def reclaim_at_candidate_promotion(
        staged_path, destination, *, publish_allowed=None
    ):
        nonlocal reclaimed
        if not reclaimed:
            reclaimed = True
            _expire_and_reclaim(store, job["id"], replacement_owner)
            replacement_service.select(
                job["id"],
                lease_owner=replacement_owner,
                cancel_requested=lambda: _lease_cancelled(
                    store, job["id"], replacement_owner
                ),
            )
        return original_promote(
            staged_path,
            destination,
            publish_allowed=publish_allowed,
        )

    monkeypatch.setattr(
        subtitles_module,
        "promote_subtitle_file",
        reclaim_at_candidate_promotion,
    )

    with pytest.raises(asyncio.CancelledError):
        stale_service.select(
            job["id"],
            lease_owner=stale_owner,
            cancel_requested=lambda: _lease_cancelled(
                store, job["id"], stale_owner
            ),
        )

    artifact = Path(
        store.get_candidate(candidate["id"], include_internal=True)["artifact_path"]
    )
    assert artifact.read_bytes() == REPLACEMENT_PROVIDER_SRT
    assert cache.has("tt0110912").read_bytes() == REPLACEMENT_PROVIDER_SRT
    assert not list(settings.results_dir.rglob("*.partial*"))


def test_reclaimed_cache_promotion_cannot_overwrite_replacement_owner_bytes(tmp_path):
    store = OperationStore(tmp_path / "cache-race.db")
    store.initialize()
    job, _ = store.create_or_get_active_job(
        "tt0110912", "pulp fiction", "Pulp Fiction"
    )
    stale_owner = "stale-cache-worker"
    replacement_owner = "replacement-cache-worker"
    store.claim_next_job(stale_owner, lease_seconds=30)
    settings = Settings(base_dir=tmp_path, results_dir=tmp_path / "results")
    replacement_source = tmp_path / "replacement-cache.srt"
    replacement_source.write_bytes(REPLACEMENT_PROVIDER_SRT)
    reclaimed = False

    class ReclaimingCache(SubtitleCache):
        def store(
            self,
            imdb_id,
            srt_path,
            *,
            replace=False,
            publish_allowed=None,
        ):
            nonlocal reclaimed
            if not reclaimed:
                reclaimed = True
                _expire_and_reclaim(store, job["id"], replacement_owner)
                super().store(
                    imdb_id,
                    replacement_source,
                    replace=True,
                    publish_allowed=lambda: not _lease_cancelled(
                        store, job["id"], replacement_owner
                    ),
                )
            return super().store(
                imdb_id,
                srt_path,
                replace=replace,
                publish_allowed=publish_allowed,
            )

    cache = ReclaimingCache(settings.results_dir)
    result = SubtitleResult(
        "1",
        "one.srt",
        "Pulp Fiction",
        "1994",
        "en",
        None,
        "tt0110912",
        runtime_seconds=100 * 60,
    )
    service = SubtitleService(
        store,
        FakeSubtitleClient([result], {"1": VALID_PROVIDER_SRT}),
        cache,
        settings,
    )
    service.discover(
        job["id"],
        lease_owner=stale_owner,
        cancel_requested=lambda: _lease_cancelled(store, job["id"], stale_owner),
    )

    with pytest.raises(asyncio.CancelledError):
        service.select(
            job["id"],
            lease_owner=stale_owner,
            cancel_requested=lambda: _lease_cancelled(
                store, job["id"], stale_owner
            ),
        )

    assert cache.has("tt0110912").read_bytes() == REPLACEMENT_PROVIDER_SRT
    assert not list(settings.results_dir.rglob("*.partial*"))


@pytest.mark.anyio
async def test_automatic_subtitle_completion_persists_a_resumable_manifest(tmp_path):
    store = OperationStore(tmp_path / "subtitle-resume.db")
    store.initialize()
    job, _ = store.create_or_get_active_job("tt0110912", "", "Fixture Movie")
    subtitle = tmp_path / "fixture.srt"
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nFixture.\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(subtitle.read_bytes()).hexdigest()
    candidate, _ = store.record_candidate(
        job["id"],
        "fixture",
        "fixture-1",
        source_type="upload",
        status="validated",
        content_hash=digest,
        artifact_path=str(subtitle),
    )

    class CompletingSubtitleService:
        def select(self, job_id, *, lease_owner, cancel_requested):
            assert not cancel_requested()
            selected = store.update_candidate(
                candidate["id"], status="selected", lease_owner=lease_owner
            )
            store.transition_stage(
                job_id,
                "subtitle_selection",
                StageState.COMPLETED,
                expected_state=StageState.RUNNING,
                lease_owner=lease_owner,
            )
            return selected

    store.claim_next_job("worker", lease_seconds=30)
    settings = Settings(base_dir=tmp_path, output_dir=tmp_path / "output")
    services = GenerationPipelineServices(
        store,
        settings,
        config=_config(),
        metadata_client=FakeMetadataClient(),
        subtitle_service=CompletingSubtitleService(),
        encoder=FakeEncoder(),
    )
    await PipelineRunner(
        store,
        services,
        stages=("subtitle_selection",),
        settings=settings,
    ).run(job["id"], "worker")

    detail = store.get_job_detail(job["id"])
    stage = next(
        item for item in detail["stages"] if item["name"] == "subtitle_selection"
    )
    assert detail["run"]["state"] == "completed"
    assert stage["output_manifest"]["job_id"] == job["id"]
    assert services.validate_stage(
        "subtitle_selection", job["id"], stage["output_manifest"]
    )

    changed_subtitle = tmp_path / "changed.srt"
    changed_subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nChanged.\n", encoding="utf-8"
    )
    store.update_candidate(
        candidate["id"],
        content_hash=hashlib.sha256(changed_subtitle.read_bytes()).hexdigest(),
        artifact_path=str(changed_subtitle),
    )
    assert not services.validate_stage(
        "subtitle_selection", job["id"], stage["output_manifest"]
    )


def test_audio_cache_keys_cover_output_settings_and_promotions_are_atomic(tmp_path):
    first = ElevenLabsProvider(
        {"voice_id": "voice", "stability": 0.1, "cache_dir": tmp_path / "cache"}
    )
    second = ElevenLabsProvider(
        {"voice_id": "voice", "stability": 0.9, "cache_dir": tmp_path / "cache"}
    )
    first_key = _cache_key(first.name, "line", **first._cache_params())
    second_key = _cache_key(second.name, "line", **second._cache_params())
    assert first_key != second_key

    class CachedFixtureProvider(AudioProvider):
        name = "fixture"
        cacheable = True

        def __init__(self, config=None):
            super().__init__(config)
            self.calls = 0
            self.reject_cache_probe = False

        def _generate(self, text, output_path, **_kwargs):
            self.calls += 1
            output_path.write_bytes(text.encode())
            return output_path

        def _validate_audio(self, path, **_kwargs):
            return not (self.reject_cache_probe and Path(path).parent == self.cache_dir)

    provider = CachedFixtureProvider({"cache_dir": tmp_path / "cache"})
    first_output = provider.generate("hello", tmp_path / "first.m4a")
    second_output = provider.generate("hello", tmp_path / "second.m4a")
    assert provider.calls == 1
    assert first_output.read_bytes() == second_output.read_bytes() == b"hello"
    cached = next((tmp_path / "cache").glob("*.m4a"))
    cached.with_suffix(f"{cached.suffix}.json").unlink()
    provider.generate("hello", tmp_path / "missing-checksum.m4a")
    assert provider.calls == 2
    cached.write_bytes(b"corrupt")
    third_output = provider.generate("hello", tmp_path / "third.m4a")
    assert provider.calls == 3
    assert third_output.read_bytes() == b"hello"
    provider.reject_cache_probe = True
    provider.generate("hello", tmp_path / "probe-rejected.m4a")
    assert provider.calls == 4
    assert not list(tmp_path.rglob("*.partial*"))


def test_silence_provider_cancellation_terminates_hung_ffmpeg(tmp_path):
    process = None

    class HangingProcess:
        returncode = None
        terminated = False
        killed = False

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.killed = True
            self.returncode = -9

    def popen(args, **kwargs):
        nonlocal process
        assert kwargs["shell"] is False
        process = HangingProcess()
        Path(args[-1]).write_bytes(b"partial")
        return process

    provider = SilenceProvider({}, popen=popen)
    output = tmp_path / "silence.m4a"
    with pytest.raises(asyncio.CancelledError):
        provider.generate(
            "",
            output,
            duration=1.0,
            cancel_requested=lambda: process is not None,
        )

    assert process is not None and process.terminated
    assert not output.exists()


def test_failed_audio_mix_preserves_previous_output(tmp_path):
    output = tmp_path / "mixed.m4a"
    output.write_bytes(b"last-good")

    class FailedProcess:
        returncode = 1

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    def fail(args, **kwargs):
        Path(args[-1]).write_bytes(b"partial")
        assert kwargs["shell"] is False
        return FailedProcess()

    timeline = type("Timeline", (), {"layers": [], "total_duration": 1.0})()

    with pytest.raises(subprocess.CalledProcessError):
        AudioMixer(popen=fail).mix(timeline, output)

    assert output.read_bytes() == b"last-good"
    assert not list(tmp_path.glob("*.partial*.m4a"))


def test_audio_mix_cancellation_terminates_hung_process_and_preserves_output(tmp_path):
    output = tmp_path / "mixed.m4a"
    output.write_bytes(b"last-good")
    process = None

    class HangingProcess:
        returncode = None
        terminated = False
        killed = False

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.killed = True
            self.returncode = -9

    def popen(args, **kwargs):
        nonlocal process
        assert kwargs["shell"] is False
        process = HangingProcess()
        Path(args[-1]).write_bytes(b"partial")
        return process

    timeline = type("Timeline", (), {"layers": [], "total_duration": 1.0})()
    mixer = AudioMixer(popen=popen)

    with pytest.raises(asyncio.CancelledError):
        mixer.mix(
            timeline,
            output,
            cancel_requested=lambda: process is not None,
        )

    assert process is not None and process.terminated
    assert output.read_bytes() == b"last-good"
    assert not list(tmp_path.glob("*.partial*.m4a"))
