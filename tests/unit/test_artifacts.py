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
    frames = manager.path(JOB_ID, "graph", "frames")

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
    assert sorted(
        path.name for path in manager.path(JOB_ID, "composite", "concat").glob("*.png")
    ) == [
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

    assert sorted(
        path.name for path in manager.path(JOB_ID, "composite", "concat").glob("*.png")
    ) == [
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
    manager = ArtifactManager(tmp_path / "artifacts")
    previous = manager.path(JOB_ID, "audio", "mixed.m4a")
    previous.parent.mkdir(parents=True)
    previous.write_bytes(b"last-good")
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
    assert not staged.exists()
