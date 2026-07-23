"""The console adapter uses the same durable pipeline contract as the API."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from api.database import OperationStore
from api.pipeline import GENERATION_STAGES, StageResult
from api.retry import RetryPolicy
from api.settings import Settings, validate_job_id
from main import run_cli
from scripts.dev_frames import load_preview_context, preview_settings, run_preview_cli
from src.video.encoder import EncodingError

ANALYSIS_STAGES = GENERATION_STAGES[: GENERATION_STAGES.index("analysis") + 1]


class FakeArtifacts:
    def __init__(self, root: Path):
        self.root = root

    def artifact_path(self, manifest):
        path = self.root / manifest["artifact"]["path"]
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError("artifact is invalid")
        return path


class EscapingArtifacts(FakeArtifacts):
    def __init__(self, root: Path, escaped_path: Path):
        super().__init__(root)
        self.escaped_path = escaped_path

    def artifact_path(self, manifest):
        super().artifact_path(manifest)
        return self.escaped_path


class FakeServices:
    def __init__(self, root: Path, *, invalid_stage: str | None = None, error=None):
        self.artifacts = FakeArtifacts(root)
        self.calls: list[tuple[str, str]] = []
        self.invalid_stage = invalid_stage
        self.error = error

    def retry_policy(self, _stage_name):
        return RetryPolicy(1)

    def run_stage(self, stage_name, job_id, progress):
        self.calls.append((stage_name, job_id))
        progress(1, 1, "items")
        if stage_name == self.error:
            raise RuntimeError(f"provider secret-value failed at {Path.cwd()}")
        if stage_name == "composite":
            parent = next(
                stage
                for stage in self.store.get_job_detail(job_id)["stages"]
                if stage["name"] == "composite"
            )
            for index, child in enumerate(
                ("intro_hold", "intro_transition", "graph", "verdict"), 1
            ):
                name = f"composite.{child}"
                self.store.ensure_stage(
                    job_id,
                    name,
                    ordinal=parent["ordinal"] * 100 + index,
                    parent_name="composite",
                    lease_owner=progress.lease_owner,
                )
                self.store.transition_stage(
                    job_id,
                    name,
                    "queued",
                    expected_state="pending",
                    lease_owner=progress.lease_owner,
                )
                self.store.transition_stage(
                    job_id,
                    name,
                    "running",
                    expected_state="queued",
                    lease_owner=progress.lease_owner,
                )
                self.store.transition_stage(
                    job_id,
                    name,
                    "running",
                    expected_state="running",
                    output_manifest={"child": name},
                    lease_owner=progress.lease_owner,
                )
        suffix = ".mp4" if stage_name == "encode" else ".json"
        relative = Path(job_id) / f"{stage_name}{suffix}"
        path = self.artifacts.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video" if stage_name == "encode" else b"{}")
        return StageResult(
            output_manifest={
                "job_id": job_id,
                "stage": stage_name,
                "artifact": {"path": relative.as_posix()},
            }
        )

    def validate_stage(self, stage_name, expected_job_id, manifest):
        if stage_name == self.invalid_stage:
            return False
        return (
            manifest.get("job_id") == expected_job_id
            and manifest.get("stage") == stage_name
            and (self.artifacts.root / manifest["artifact"]["path"]).is_file()
        )


class MissingFFmpegServices(FakeServices):
    def run_stage(self, stage_name, job_id, progress):
        if stage_name == "encode":
            raise EncodingError("ffmpeg executable was not found")
        return super().run_stage(stage_name, job_id, progress)


class NonProducingEncodeServices(FakeServices):
    def run_stage(self, stage_name, job_id, progress):
        if stage_name != "encode":
            return super().run_stage(stage_name, job_id, progress)
        self.calls.append((stage_name, job_id))
        progress(1, 1, "items")
        return StageResult(
            output_manifest={
                "job_id": job_id,
                "stage": stage_name,
                "artifact": {"path": f"{job_id}/encode.mp4"},
            }
        )


class LeaseLossServices(FakeServices):
    def run_stage(self, stage_name, job_id, progress):
        if stage_name == "analysis":
            raise asyncio.CancelledError("lost /absolute/private lease")
        return super().run_stage(stage_name, job_id, progress)


class BlockedAsyncServices(FakeServices):
    async def run_stage(self, stage_name, job_id, progress):
        if stage_name == "analysis":
            await asyncio.sleep(0.8)
        return super().run_stage(stage_name, job_id, progress)


class EmptyEncodeServices(FakeServices):
    def run_stage(self, stage_name, job_id, progress):
        result = super().run_stage(stage_name, job_id, progress)
        if stage_name == "encode":
            path = self.artifacts.root / result.output_manifest["artifact"]["path"]
            path.write_bytes(b"")
        return result


class ZeroFrameServices(FakeServices):
    def run_stage(self, stage_name, job_id, progress):
        if stage_name == "composite":
            raise ValueError("Composite artifact has no frames")
        return super().run_stage(stage_name, job_id, progress)


class TransientFailureServices(FakeServices):
    def run_stage(self, stage_name, job_id, progress):
        if stage_name == "analysis":
            raise ConnectionError("temporary provider failure")
        return super().run_stage(stage_name, job_id, progress)


class HeartbeatFailureStore(OperationStore):
    fail_renewal = False

    def renew_lease(self, job_id, owner, *, lease_seconds):
        if self.fail_renewal:
            raise RuntimeError("heartbeat secret-path failure")
        return super().renew_lease(job_id, owner, lease_seconds=lease_seconds)


class HeartbeatFailureServices(FakeServices):
    async def run_stage(self, stage_name, job_id, progress):
        if stage_name == "analysis":
            self.store.fail_renewal = True
            await asyncio.sleep(1)
        return super().run_stage(stage_name, job_id, progress)


@pytest.fixture
def cli_runtime(tmp_path):
    base = tmp_path / "project"
    base.mkdir()
    (base / "config.yaml").write_text("categories: {}\nvideo: {}\n", encoding="utf-8")
    settings = Settings(
        base_dir=base,
        data_dir=base / "data",
        output_dir=base / "output",
        results_dir=base / "results",
        retry_delays=(),
    )
    store = OperationStore(settings.data_dir / "slur_meter.db")
    services = FakeServices(settings.output_dir)
    return base, settings, store, services


def invoke(cli_runtime, argv, *, services=None):
    base, settings, store, default_services = cli_runtime
    stdout: list[str] = []
    stderr: list[str] = []
    selected_services = services or default_services
    selected_services.store = store
    code = run_cli(
        argv,
        base_dir=base,
        settings_factory=lambda _base: settings,
        store_factory=lambda _settings: store,
        services_factory=lambda _store, _settings, _config: selected_services,
        stdout=stdout.append,
        stderr=stderr.append,
    )
    return code, "\n".join(stdout), "\n".join(stderr)


@pytest.mark.parametrize(
    ("argv", "expected_stages", "terminal_stage"),
    [
        (["--imdb", "110912"], ANALYSIS_STAGES, "analysis"),
        (["--query", "  Pulp   Fiction  ", "--render"], GENERATION_STAGES, "encode"),
    ],
)
def test_cli_runs_shared_pipeline_and_prints_validated_artifact(
    cli_runtime, argv, expected_stages, terminal_stage
):
    code, stdout, stderr = invoke(cli_runtime, argv)

    services = cli_runtime[3]
    job_ids = {job_id for _, job_id in services.calls}
    assert code == 0
    assert stderr == ""
    assert [stage for stage, _ in services.calls] == list(expected_stages)
    assert len(job_ids) == 1
    job_id = validate_job_id(job_ids.pop())
    assert job_id in stdout
    assert f"output/{job_id}/{terminal_stage}" in stdout
    assert str(cli_runtime[0]) not in stdout


def test_render_only_resumes_existing_validated_analysis(cli_runtime):
    first_code, first_stdout, _ = invoke(cli_runtime, ["--imdb", "tt0110912"])
    job_id = next(word for word in first_stdout.split() if word.startswith("job_"))
    cli_runtime[3].calls.clear()

    code, stdout, stderr = invoke(cli_runtime, ["--render-only", job_id])

    assert first_code == 0
    assert code == 0
    assert stderr == ""
    assert [stage for stage, _ in cli_runtime[3].calls] == list(
        GENERATION_STAGES[GENERATION_STAGES.index("graph") :]
    )
    assert f"output/{job_id}/encode.mp4" in stdout


def test_query_identity_is_opaque_distinct_and_active_duplicates_are_reused(cli_runtime):
    store = cli_runtime[2]
    store.initialize()
    unrelated, _ = store.create_or_get_active_job("tt0100000", "", "Other")
    existing, _ = store.create_or_get_active_job("", " Pulp   Fiction ", "Pulp Fiction")

    code, stdout, _ = invoke(cli_runtime, ["--query", "pulp fiction"])
    code_two, stdout_two, _ = invoke(cli_runtime, ["--query", "Jackie Brown"])

    assert code == code_two == 0
    assert existing["id"] in stdout
    assert store.get_job(unrelated["id"])["state"] == "queued"
    second_id = next(word for word in stdout_two.split() if word.startswith("job_"))
    assert second_id != existing["id"]
    assert not (cli_runtime[1].results_dir / "query.json").exists()
    assert "Pulp" not in stdout and "Jackie" not in stdout_two


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--imdb", ""],
        ["--query", "   "],
        ["--imdb", "tt0110912", "--query", "movie"],
        ["--render-only", "../job_0123456789abcdef"],
        ["--render-only", "/tmp/job_0123456789abcdef"],
        ["--render-only", "job_0123456789abcdef/analysis.json"],
        ["--render-only", "job_bad"],
        ["--render-only", "job_0123456789abcdef", "--render"],
        ["--imdb", "tt0110912/../../secret"],
        ["--query", "movie", "unexpected"],
    ],
)
def test_invalid_or_conflicting_identity_is_nonzero(cli_runtime, argv):
    code, stdout, stderr = invoke(cli_runtime, argv)
    assert code != 0
    assert stdout == ""
    assert stderr
    assert str(cli_runtime[0]) not in stderr


def test_unknown_render_only_id_is_nonzero(cli_runtime):
    code, stdout, stderr = invoke(
        cli_runtime, ["--render-only", "job_0123456789abcdef"]
    )
    assert code != 0
    assert stdout == ""
    assert "not found" in stderr.lower()


@pytest.mark.parametrize("invalid_stage", ["analysis", "encode"])
def test_nonvalidated_or_nonproduced_terminal_artifact_is_nonzero(
    cli_runtime, invalid_stage
):
    services = FakeServices(cli_runtime[1].output_dir, invalid_stage=invalid_stage)
    argv = ["--query", "movie"] + (["--render"] if invalid_stage == "encode" else [])
    code, stdout, stderr = invoke(cli_runtime, argv, services=services)
    assert code != 0
    assert stdout == ""
    assert stderr
    assert cli_runtime[2].list_jobs()["items"][0]["state"] == "needs_attention"


def test_validating_service_cannot_return_artifact_outside_requested_run(cli_runtime):
    escaped = cli_runtime[0].parent / "escaped.mp4"
    escaped.write_bytes(b"private")
    services = FakeServices(cli_runtime[1].output_dir)
    services.artifacts = EscapingArtifacts(cli_runtime[1].output_dir, escaped)

    code, stdout, stderr = invoke(
        cli_runtime, ["--query", "movie", "--render"], services=services
    )

    assert code != 0
    assert stdout == ""
    assert str(escaped) not in stderr


def test_persisted_pipeline_failure_is_nonzero_and_diagnostics_are_sanitized(
    cli_runtime, monkeypatch
):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "secret-value")
    services = FakeServices(cli_runtime[1].output_dir, error="encode")

    code, stdout, stderr = invoke(
        cli_runtime, ["--query", "movie", "--render"], services=services
    )

    assert code != 0
    assert stdout == ""
    assert "secret-value" not in stderr
    assert str(Path.cwd()) not in stderr
    assert stderr.startswith("Error:")


def test_persisted_retryable_failed_state_is_nonzero(cli_runtime):
    services = TransientFailureServices(cli_runtime[1].output_dir)
    code, stdout, stderr = invoke(cli_runtime, ["--query", "movie"], services=services)
    assert code != 0
    assert stdout == ""
    assert stderr
    assert cli_runtime[2].list_jobs()["items"][0]["state"] == "failed"


def test_lost_lease_returns_sanitized_nonzero_instead_of_escaping(cli_runtime):
    services = LeaseLossServices(cli_runtime[1].output_dir)
    code, stdout, stderr = invoke(cli_runtime, ["--query", "movie"], services=services)
    assert code != 0
    assert stdout == ""
    assert "lost /absolute/private lease" not in stderr
    assert stderr.startswith("Error:")
    assert cli_runtime[2].list_jobs()["items"][0]["state"] == "queued"


def test_cli_renews_exact_claim_while_async_stage_is_blocked(cli_runtime):
    base, settings, store, _ = cli_runtime
    services = BlockedAsyncServices(settings.output_dir)
    services.store = store
    stdout: list[str] = []
    stderr: list[str] = []
    code = run_cli(
        ["--query", "movie"],
        base_dir=base,
        lease_seconds=0.5,
        heartbeat_interval=0.05,
        settings_factory=lambda _base: settings,
        store_factory=lambda _settings: store,
        services_factory=lambda _store, _settings, _config: services,
        stdout=stdout.append,
        stderr=stderr.append,
    )
    assert code == 0
    assert stderr == []
    assert any("Artifact:" in line for line in stdout)


def test_heartbeat_failure_cancels_runner_before_releasing_claim(cli_runtime):
    base, settings, _store, _ = cli_runtime
    store = HeartbeatFailureStore(settings.data_dir / "heartbeat.db")
    services = HeartbeatFailureServices(settings.output_dir)
    services.store = store
    stdout: list[str] = []
    stderr: list[str] = []
    code = run_cli(
        ["--query", "movie"],
        base_dir=base,
        lease_seconds=0.5,
        heartbeat_interval=0.05,
        settings_factory=lambda _base: settings,
        store_factory=lambda _settings: store,
        services_factory=lambda _store, _settings, _config: services,
        stdout=stdout.append,
        stderr=stderr.append,
    )
    assert code != 0
    assert stdout == []
    assert "secret-path" not in "\n".join(stderr)
    assert store.list_jobs()["items"][0]["state"] == "queued"


def test_missing_ffmpeg_is_nonzero_without_false_success(cli_runtime):
    services = MissingFFmpegServices(cli_runtime[1].output_dir)
    code, stdout, stderr = invoke(
        cli_runtime, ["--query", "movie", "--render"], services=services
    )
    assert code != 0
    assert stdout == ""
    assert stderr


def test_encoder_return_without_produced_mp4_is_nonzero(cli_runtime):
    services = NonProducingEncodeServices(cli_runtime[1].output_dir)
    code, stdout, stderr = invoke(
        cli_runtime, ["--query", "movie", "--render"], services=services
    )
    assert code != 0
    assert stdout == ""
    assert stderr


def test_tampered_encode_rejected_by_shared_validator(cli_runtime):
    services = FakeServices(cli_runtime[1].output_dir, invalid_stage="encode")
    code, stdout, stderr = invoke(
        cli_runtime, ["--query", "movie", "--render"], services=services
    )
    assert code != 0
    assert stdout == ""
    assert stderr
    assert cli_runtime[2].list_jobs()["items"][0]["state"] == "needs_attention"


@pytest.mark.parametrize("service_type", [EmptyEncodeServices, ZeroFrameServices])
def test_empty_encode_or_zero_frames_is_nonzero(cli_runtime, service_type):
    services = service_type(cli_runtime[1].output_dir)
    code, stdout, stderr = invoke(
        cli_runtime, ["--query", "movie", "--render"], services=services
    )
    assert code != 0
    assert stdout == ""
    assert stderr


def test_render_only_requeues_the_failed_downstream_stage(cli_runtime):
    failed_services = MissingFFmpegServices(cli_runtime[1].output_dir)
    first_code, _, _ = invoke(
        cli_runtime,
        ["--query", "movie", "--render"],
        services=failed_services,
    )
    failed_job = cli_runtime[2].list_jobs()["items"][0]
    resumed_services = FakeServices(cli_runtime[1].output_dir)

    code, stdout, stderr = invoke(
        cli_runtime,
        ["--render-only", failed_job["id"]],
        services=resumed_services,
    )

    assert first_code != 0
    assert code == 0
    assert stderr == ""
    assert resumed_services.calls == [("encode", failed_job["id"])]
    assert "encode.mp4" in stdout


def test_render_only_requires_current_validated_analysis(cli_runtime):
    code, stdout, _ = invoke(cli_runtime, ["--imdb", "tt0110912"])
    job_id = next(word for word in stdout.split() if word.startswith("job_"))
    (cli_runtime[1].output_dir / job_id / "analysis.json").write_bytes(b"")

    resume_code, resume_stdout, resume_stderr = invoke(
        cli_runtime, ["--render-only", job_id]
    )
    assert code == 0
    assert resume_code != 0
    assert resume_stdout == ""
    assert resume_stderr


def test_shell_environment_beats_dotenv_for_cli_settings(tmp_path, monkeypatch):
    base = tmp_path / "project"
    base.mkdir()
    (base / "config.yaml").write_text("categories: {}\n", encoding="utf-8")
    (base / ".env").write_text("DATA_DIR=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("DATA_DIR", "from-shell")
    captured = {}

    def store_factory(settings):
        captured["data_dir"] = settings.data_dir
        return OperationStore(settings.data_dir / "slur_meter.db")

    services = FakeServices(base / "output")
    code = run_cli(
        ["--query", "movie"],
        base_dir=base,
        store_factory=store_factory,
        services_factory=lambda _store, _settings, _config: services,
        stdout=lambda _line: None,
        stderr=lambda _line: None,
    )
    assert code == 0
    assert captured["data_dir"] == (base / "from-shell").resolve()


def test_preview_uses_validated_manifest_paths_and_rejects_unsafe_ids(cli_runtime):
    code, stdout, _ = invoke(cli_runtime, ["--imdb", "tt0110912"])
    job_id = next(word for word in stdout.split() if word.startswith("job_"))
    settings, store, services = cli_runtime[1:]

    context = load_preview_context(job_id, settings, store, services)
    assert code == 0
    assert context.analysis_path == settings.output_dir / job_id / "analysis.json"
    assert context.output_dir == settings.output_dir / job_id / "dev_frames"
    assert context.output_dir.resolve().is_relative_to(
        (settings.output_dir / job_id).resolve()
    )

    for unsafe in ("../job_0123456789abcdef", "/tmp/x", "job_bad"):
        with pytest.raises((ValueError, RuntimeError)):
            load_preview_context(unsafe, settings, store, services)


def test_preview_rejects_artifact_service_symlink_escape(cli_runtime):
    code, stdout, _ = invoke(cli_runtime, ["--imdb", "tt0110912"])
    job_id = next(word for word in stdout.split() if word.startswith("job_"))
    settings, store, services = cli_runtime[1:]
    outside = cli_runtime[0].parent / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = settings.output_dir / job_id / "escaped-analysis.json"
    link.symlink_to(outside)
    services.artifacts = EscapingArtifacts(settings.output_dir, link)

    assert code == 0
    with pytest.raises(RuntimeError, match="confined"):
        load_preview_context(job_id, settings, store, services)


def test_preview_environment_keeps_shell_values(tmp_path, monkeypatch):
    base = tmp_path / "project"
    base.mkdir()
    (base / ".env").write_text("OUTPUT_DIR=dotenv-output\n", encoding="utf-8")
    monkeypatch.setenv("OUTPUT_DIR", "shell-output")
    assert preview_settings(base).output_dir == (base / "shell-output").resolve()


def test_preview_cli_rejects_unsafe_job_without_traceback_or_absolute_path(cli_runtime):
    base, settings, store, services = cli_runtime
    stdout: list[str] = []
    stderr: list[str] = []
    code = run_preview_cli(
        ["--job", "../job_0123456789abcdef"],
        base_dir=base,
        settings_factory=lambda _base: settings,
        store_factory=lambda _settings: store,
        services_factory=lambda _store, _settings, _config: services,
        stdout=stdout.append,
        stderr=stderr.append,
    )
    assert code != 0
    assert stdout == []
    diagnostic = "\n".join(stderr)
    assert "Traceback" not in diagnostic
    assert str(base) not in diagnostic


def test_default_config_disables_paid_audio_and_env_example_is_complete():
    config = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    assert all(
        not config["audio"][name]["enabled"]
        for name in ("intro_tts", "outro_tts", "background_music")
    )

    documented = {
        line.split("=", 1)[0]
        for line in Path(".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    assert {
        "OPENSUBTITLES_API_KEY",
        "OPENSUBTITLES_USER_AGENT",
        "OPENSUBTITLES_JWT",
        "OPENSUBTITLES_USERNAME",
        "OPENSUBTITLES_PASSWORD",
        "TMDB_READ_TOKEN",
        "OMDB_API_KEY",
        "ELEVENLABS_API_KEY",
        "OPENROUTER_API_KEY",
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
        "TIKTOK_SESSION_ID",
        "INSTAGRAM_SESSION_ID",
        "ADMIN_API_TOKEN",
        "ALLOW_LOCAL_DEVELOPMENT_AUTH",
        "ALLOWED_ORIGINS",
        "RETRY_DELAYS",
        "SUBTITLE_COVERAGE_THRESHOLD",
        "SUBTITLE_CANDIDATES_PER_CYCLE",
        "DATA_DIR",
        "OUTPUT_DIR",
        "RESULTS_DIR",
    } <= documented


def test_api_and_cli_default_store_use_configured_data_root(cli_runtime):
    from api import main as api_main
    from main import _default_store

    settings = cli_runtime[1]
    assert _default_store(settings).path == settings.data_dir / "slur_meter.db"
    assert api_main.operation_store.path == api_main.runtime_settings.data_dir / "slur_meter.db"
