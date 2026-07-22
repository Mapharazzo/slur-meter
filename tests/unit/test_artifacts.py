import asyncio
import json
from pathlib import Path

import pytest
from PIL import Image

from api.artifacts import ArtifactManager, ArtifactValidationError

JOB_ID = "job_0123456789abcdef"


def _frames(directory: Path, count: int, size=(4, 3), prefix="frame_") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        Image.new("RGB", size, (index, 0, 0)).save(
            directory / f"{prefix}{index:05d}.png"
        )


def test_generated_paths_are_confined_to_the_run(tmp_path):
    manager = ArtifactManager(tmp_path / "artifacts")

    path = manager.path(JOB_ID, "graph", "frames")

    assert path == (tmp_path / "artifacts" / JOB_ID / "graph" / "frames").resolve()
    with pytest.raises(ValueError, match="escapes|component"):
        manager.path(JOB_ID, "..", "outside")
    with pytest.raises(ValueError, match="Invalid job ID"):
        manager.path("tt0110912", "graph")


@pytest.mark.parametrize("mutation", ["missing", "extra", "dimension"])
def test_frame_manifest_rejects_missing_extra_and_wrong_dimension(tmp_path, mutation):
    manager = ArtifactManager(tmp_path / "artifacts")
    staging = manager.new_staging_directory(JOB_ID, "graph")
    _frames(staging, 3)
    manifest = manager.promote_frame_directory(
        JOB_ID,
        "graph",
        staging,
        final_name="frames",
        expected_count=3,
        dimensions=(4, 3),
        prefix="frame_",
        input_hashes={"analysis": "analysis-v1"},
        config_hash="config-v1",
    )
    frames = manager.artifact_path(manifest)

    if mutation == "missing":
        (frames / "frame_00001.png").unlink()
    elif mutation == "extra":
        Image.new("RGB", (4, 3)).save(frames / "frame_00003.png")
    else:
        Image.new("RGB", (5, 3)).save(frames / "frame_00001.png")

    assert not manager.validate(
        manifest,
        input_hashes={"analysis": "analysis-v1"},
        config_hash="config-v1",
    )


def test_hash_or_manifest_version_change_invalidates_reuse(tmp_path):
    manager = ArtifactManager(tmp_path / "artifacts")
    manifest = manager.write_json(
        JOB_ID,
        "analysis",
        "analysis.json",
        {"summary": {"total_hard": 1}},
        input_hashes={"subtitle": "subtitle-v1"},
        config_hash="categories-v1",
    )

    assert manager.validate(
        manifest,
        input_hashes={"subtitle": "subtitle-v1"},
        config_hash="categories-v1",
    )
    assert not manager.validate(
        manifest,
        input_hashes={"subtitle": "subtitle-v2"},
        config_hash="categories-v1",
    )
    assert not manager.validate(
        manifest,
        input_hashes={"subtitle": "subtitle-v1"},
        config_hash="categories-v2",
    )
    old = dict(manifest)
    old["manifest_version"] -= 1
    assert not manager.validate(old)


def test_directory_is_validated_before_atomic_promotion_and_stale_tail_is_removed(
    tmp_path,
):
    manager = ArtifactManager(tmp_path / "artifacts")
    initial = manager.new_staging_directory(JOB_ID, "composite")
    _frames(initial, 3, prefix="")
    manager.promote_frame_directory(
        JOB_ID,
        "composite",
        initial,
        final_name="concat",
        expected_count=3,
        dimensions=(4, 3),
        prefix="",
        input_hashes={"graph": "v1"},
        config_hash="v1",
    )

    invalid = manager.new_staging_directory(JOB_ID, "composite")
    _frames(invalid, 1, prefix="")
    with pytest.raises(ArtifactValidationError):
        manager.promote_frame_directory(
            JOB_ID,
            "composite",
            invalid,
            final_name="concat",
            expected_count=2,
            dimensions=(4, 3),
            prefix="",
            input_hashes={"graph": "v2"},
            config_hash="v1",
        )
    first_manifest = json.loads(
        manager.manifest_path(JOB_ID, "composite").read_text(encoding="utf-8")
    )
    assert sorted(path.name for path in manager.artifact_path(first_manifest).glob("*.png")) == [
        "00000.png",
        "00001.png",
        "00002.png",
    ]

    replacement = manager.new_staging_directory(JOB_ID, "composite")
    _frames(replacement, 2, prefix="")
    manifest = manager.promote_frame_directory(
        JOB_ID,
        "composite",
        replacement,
        final_name="concat",
        expected_count=2,
        dimensions=(4, 3),
        prefix="",
        input_hashes={"graph": "v2"},
        config_hash="v1",
    )

    assert sorted(path.name for path in manager.artifact_path(manifest).glob("*.png")) == [
        "00000.png",
        "00001.png",
    ]
    stored = json.loads(manager.manifest_path(JOB_ID, "composite").read_text())
    assert stored == manifest
    assert not list((tmp_path / "artifacts").rglob("*.partial*"))


def test_nonzero_media_validation_uses_probe_when_available(tmp_path):
    warnings = []
    manager = ArtifactManager(
        tmp_path / "artifacts",
        probe_duration=lambda _path: 1.25,
        warning_callback=warnings.append,
    )
    staged = manager.new_staging_file(JOB_ID, "audio", suffix=".m4a")
    staged.write_bytes(b"media")

    manifest = manager.promote_file(
        JOB_ID,
        "audio",
        staged,
        final_name="mixed.m4a",
        media_kind="audio",
        input_hashes={"timing": "v1"},
        config_hash="audio-v1",
    )

    assert manifest["artifact"]["size"] == 5
    assert manifest["artifact"]["duration"] == pytest.approx(1.25)
    assert manager.validate(manifest)
    assert warnings == []


def test_invalid_staged_media_is_removed_without_replacing_previous_output(tmp_path):
    manager = ArtifactManager(
        tmp_path / "artifacts", probe_duration=lambda _path: 1.0
    )
    initial_staged = manager.new_staging_file(JOB_ID, "audio", suffix=".m4a")
    initial_staged.write_bytes(b"last-good")
    initial = manager.promote_file(
        JOB_ID,
        "audio",
        initial_staged,
        final_name="mixed.m4a",
        media_kind="audio",
    )
    previous = manager.artifact_path(initial)
    staged = manager.new_staging_file(JOB_ID, "audio", suffix=".m4a")
    staged.write_bytes(b"")

    with pytest.raises(ArtifactValidationError, match="empty"):
        manager.promote_file(
            JOB_ID,
            "audio",
            staged,
            final_name="mixed.m4a",
            media_kind="audio",
        )

    assert previous.read_bytes() == b"last-good"
    assert manager.validate(initial)
    assert not staged.exists()


class SimulatedPromotionCrash(BaseException):
    pass


@pytest.mark.parametrize(
    ("checkpoint", "expected_value"),
    [
        ("journal_written", {"value": "old"}),
        ("bundle_installed", {"value": "old"}),
        ("pointer_replaced", {"value": "new"}),
    ],
)
def test_version_bundle_pointer_is_always_old_or_new_after_crash(
    tmp_path, checkpoint, expected_value
):
    root = tmp_path / "artifacts"
    manager = ArtifactManager(root)
    first = manager.write_json(
        JOB_ID, "analysis", "analysis.json", {"value": "old"}
    )
    previous_path = Path(root, first["artifact"]["path"])

    def crash(name):
        if name == checkpoint:
            raise SimulatedPromotionCrash(name)

    interrupted = ArtifactManager(root, promotion_checkpoint=crash)
    with pytest.raises(SimulatedPromotionCrash):
        interrupted.write_json(
            JOB_ID, "analysis", "analysis.json", {"value": "new"}
        )

    recovered = ArtifactManager(root)
    recovered.recover(JOB_ID)
    recovered.recover(JOB_ID)
    current_manifest = json.loads(
        recovered.manifest_path(JOB_ID, "analysis").read_text(encoding="utf-8")
    )
    assert recovered.load_json(current_manifest) == expected_value
    assert recovered.validate(current_manifest)
    assert previous_path.is_file()
    assert previous_path.read_text(encoding="utf-8").strip().endswith('"old"\n}')
    assert not list(root.rglob("*.partial*"))
    assert not list(root.rglob("*.journal"))


def test_recovery_removes_partial_protocol_files_without_changing_current(tmp_path):
    root = tmp_path / "artifacts"
    manager = ArtifactManager(root)
    current = manager.write_json(
        JOB_ID, "analysis", "analysis.json", {"value": "old"}
    )
    partial_bundle = manager.path(
        JOB_ID, "versions", "analysis", ".orphan.partial"
    )
    partial_bundle.mkdir(parents=True)
    (partial_bundle / "junk").write_bytes(b"junk")
    partial_pointer = manager.path(JOB_ID, "current", ".analysis.partial")
    partial_pointer.parent.mkdir(parents=True, exist_ok=True)
    partial_pointer.write_text("partial", encoding="utf-8")

    recovered = ArtifactManager(root)

    assert recovered.validate(current)
    assert recovered.load_json(current) == {"value": "old"}
    assert not partial_bundle.exists()
    assert not partial_pointer.exists()


@pytest.mark.parametrize("link_kind", ["file", "directory"])
def test_promotion_rejects_symlink_descendants(tmp_path, link_kind):
    manager = ArtifactManager(tmp_path / "artifacts")
    outside = tmp_path / "outside"
    outside.mkdir()
    staged = manager.new_staging_directory(JOB_ID, "metadata")
    if link_kind == "file":
        target = outside / "secret.txt"
        target.write_text("secret", encoding="utf-8")
        (staged / "secret.txt").symlink_to(target)
    else:
        (outside / "nested").mkdir()
        (outside / "nested" / "secret.txt").write_text("secret", encoding="utf-8")
        (staged / "nested").symlink_to(outside / "nested", target_is_directory=True)

    with pytest.raises(ArtifactValidationError, match="symlink"):
        manager.promote_directory(
            JOB_ID, "metadata", staged, final_name="bundle"
        )


def test_promotion_rejects_a_symlink_staging_file(tmp_path):
    manager = ArtifactManager(tmp_path / "artifacts")
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret": true}', encoding="utf-8")
    staged = manager.new_staging_file(JOB_ID, "analysis", suffix=".json")
    staged.symlink_to(outside)

    with pytest.raises(ArtifactValidationError, match="symlink"):
        manager.promote_file(
            JOB_ID, "analysis", staged, final_name="analysis.json"
        )


def test_media_probe_failure_fails_closed_and_preserves_current(tmp_path):
    root = tmp_path / "artifacts"
    manager = ArtifactManager(root, probe_duration=lambda _path: 1.0)
    initial_staged = manager.new_staging_file(JOB_ID, "audio", suffix=".m4a")
    initial_staged.write_bytes(b"last-good")
    initial = manager.promote_file(
        JOB_ID,
        "audio",
        initial_staged,
        final_name="mixed.m4a",
        media_kind="audio",
    )

    def probe_failure(_path):
        raise OSError("ffprobe failed")

    failing = ArtifactManager(root, probe_duration=probe_failure)
    staged = failing.new_staging_file(JOB_ID, "audio", suffix=".m4a")
    staged.write_bytes(b"replacement")
    with pytest.raises(ArtifactValidationError, match="probe"):
        failing.promote_file(
            JOB_ID,
            "audio",
            staged,
            final_name="mixed.m4a",
            media_kind="audio",
        )

    recovered = ArtifactManager(root, probe_duration=lambda _path: 1.0)
    assert recovered.validate(initial)
    assert recovered.artifact_path(initial).read_bytes() == b"last-good"


def test_media_expected_duration_mismatch_preserves_current_version(tmp_path):
    root = tmp_path / "artifacts"
    manager = ArtifactManager(root, probe_duration=lambda _path: 2.0)
    first = manager.new_staging_file(JOB_ID, "encode", suffix=".mp4")
    first.write_bytes(b"good-video")
    current = manager.promote_file(
        JOB_ID,
        "encode",
        first,
        final_name="final.mp4",
        media_kind="video",
        expected_duration=2.0,
    )

    mismatched = ArtifactManager(root, probe_duration=lambda _path: 0.25)
    second = mismatched.new_staging_file(JOB_ID, "encode", suffix=".mp4")
    second.write_bytes(b"truncated-video")
    with pytest.raises(ArtifactValidationError, match="duration"):
        mismatched.promote_file(
            JOB_ID,
            "encode",
            second,
            final_name="final.mp4",
            media_kind="video",
            expected_duration=2.0,
        )

    recovered = ArtifactManager(root, probe_duration=lambda _path: 2.0)
    assert recovered.artifact_path(current).read_bytes() == b"good-video"


def test_publication_guard_prevents_a_stale_owner_from_replacing_current(tmp_path):
    root = tmp_path / "artifacts"
    manager = ArtifactManager(root)
    first = manager.new_staging_file(JOB_ID, "analysis", suffix=".json")
    first.write_text('{"owner": "current"}', encoding="utf-8")
    current = manager.promote_file(
        JOB_ID,
        "analysis",
        first,
        final_name="analysis.json",
        artifact_kind="json",
    )
    stale = manager.new_staging_file(JOB_ID, "analysis", suffix=".json")
    stale.write_text('{"owner": "stale"}', encoding="utf-8")

    with pytest.raises(asyncio.CancelledError):
        manager.promote_file(
            JOB_ID,
            "analysis",
            stale,
            final_name="analysis.json",
            artifact_kind="json",
            publish_allowed=lambda: False,
        )

    assert manager.artifact_path(current).read_text(encoding="utf-8") == (
        '{"owner": "current"}'
    )
    assert not list((root / JOB_ID / "journals").glob("*.journal"))
