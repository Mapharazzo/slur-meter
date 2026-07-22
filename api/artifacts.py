"""Validated, resumable generation artifacts with atomic promotion."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from PIL import Image

from api.settings import validate_job_id

MANIFEST_VERSION = 1
_STAGE_RE = re.compile(r"[a-z][a-z0-9_.-]{0,79}\Z")


class ArtifactValidationError(ValueError):
    """A staged or persisted artifact does not match its declared contract."""


class ArtifactManager:
    """Own per-run paths, content manifests, validation, and promotion."""

    def __init__(
        self,
        root: str | Path,
        *,
        manifest_version: int = MANIFEST_VERSION,
        probe_duration: Callable[[Path], float | None] | None = None,
        warning_callback: Callable[[str], None] | None = None,
        promotion_checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.manifest_version = int(manifest_version)
        self._probe_duration = probe_duration
        self._warning_callback = warning_callback
        self._promotion_checkpoint = promotion_checkpoint
        self.root.mkdir(parents=True, exist_ok=True)
        self.recover()

    def path(self, job_id: str, *parts: str | Path) -> Path:
        """Return a confined generated run path."""
        validated = validate_job_id(job_id)
        candidate = self.root / validated
        for value in parts:
            part = Path(value)
            if part.is_absolute() or any(item in {"", ".", ".."} for item in part.parts):
                raise ValueError("Generated path component escapes its run")
            candidate = candidate.joinpath(*part.parts)
        candidate = Path(os.path.abspath(candidate))
        self._require_in_run(validated, candidate)
        return candidate

    def manifest_path(self, job_id: str, stage: str) -> Path:
        stage = self._stage(stage)
        pointer = self._read_pointer(job_id, stage)
        path = Path(self.root, str(pointer["manifest_path"]))
        self._require_in_run(job_id, path)
        if self.hash_file(path) != pointer.get("manifest_sha256"):
            raise ArtifactValidationError("Current artifact manifest hash does not match")
        return path

    def recover(self, job_id: str | None = None) -> None:
        """Resolve interrupted publications without changing a valid current pointer."""
        if job_id is None:
            runs = []
            for entry in os.scandir(self.root):
                if entry.is_symlink():
                    raise ArtifactValidationError("Generated run root contains a symlink")
                if entry.is_dir(follow_symlinks=False) and entry.name.startswith("job_"):
                    try:
                        runs.append(validate_job_id(entry.name))
                    except ValueError:
                        continue
        else:
            runs = [validate_job_id(job_id)]
        for run_id in runs:
            run = self.root / run_id
            if not run.exists():
                continue
            self._reject_symlink_components(run, self.root)
            journals = run / "journals"
            if journals.is_dir():
                for journal in list(journals.glob("*.journal")):
                    self._recover_journal(run_id, journal)
            for parent_name in (".staging", "current", "versions"):
                parent = run / parent_name
                if not parent.exists():
                    continue
                self._reject_symlink_tree(parent)
                for partial in sorted(parent.rglob("*"), reverse=True):
                    if ".partial" in partial.name:
                        self._remove_path(partial)

    @staticmethod
    def fingerprint(value: Any) -> str:
        """Hash JSON-compatible input/config values deterministically."""
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def hash_file(path: str | Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def new_staging_directory(self, job_id: str, stage: str) -> Path:
        stage = self._stage(stage)
        directory = self.path(job_id, ".staging", f"{stage}.{uuid.uuid4().hex}.partial")
        directory.mkdir(parents=True, exist_ok=False)
        return directory

    def new_staging_file(self, job_id: str, stage: str, *, suffix: str = "") -> Path:
        stage = self._stage(stage)
        if suffix and (Path(suffix).name != suffix or not suffix.startswith(".")):
            raise ValueError("Staging suffix must be a simple file suffix")
        path = self.path(
            job_id,
            ".staging",
            f"{stage}.{uuid.uuid4().hex}.partial{suffix}",
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_json(
        self,
        job_id: str,
        stage: str,
        final_name: str,
        value: Any,
        *,
        input_hashes: Mapping[str, str] | None = None,
        config_hash: str = "",
        details: Mapping[str, Any] | None = None,
        publish_allowed: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        staged = self.new_staging_file(job_id, stage, suffix=".json")
        try:
            staged.write_text(
                json.dumps(value, sort_keys=True, indent=2, default=_json_default),
                encoding="utf-8",
            )
            return self.promote_file(
                job_id,
                stage,
                staged,
                final_name=final_name,
                artifact_kind="json",
                input_hashes=input_hashes,
                config_hash=config_hash,
                details=details,
                publish_allowed=publish_allowed,
            )
        finally:
            staged.unlink(missing_ok=True)

    def promote_file(
        self,
        job_id: str,
        stage: str,
        staged_path: str | Path,
        *,
        final_name: str,
        media_kind: str | None = None,
        artifact_kind: str | None = None,
        expected_duration: float | None = None,
        input_hashes: Mapping[str, str] | None = None,
        config_hash: str = "",
        details: Mapping[str, Any] | None = None,
        publish_allowed: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Validate a file and publish one immutable version bundle."""
        stage = self._stage(stage)
        staged = self._confined_staging(job_id, staged_path)
        kind = media_kind or artifact_kind or "file"
        try:
            artifact = self._inspect_file(
                staged,
                kind,
                expected_duration=expected_duration,
            )
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        return self._publish(
            job_id,
            stage,
            staged,
            artifact,
            logical_name=final_name,
            is_directory=False,
            input_hashes=input_hashes,
            config_hash=config_hash,
            details=details,
            publish_allowed=publish_allowed,
        )

    def record_file(
        self,
        job_id: str,
        stage: str,
        path: str | Path,
        *,
        media_kind: str | None = None,
        artifact_kind: str | None = None,
        expected_duration: float | None = None,
        input_hashes: Mapping[str, str] | None = None,
        config_hash: str = "",
        details: Mapping[str, Any] | None = None,
        publish_allowed: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Copy a confined file to staging and publish it as a new version."""
        stage = self._stage(stage)
        candidate = Path(os.path.abspath(path))
        self._require_in_run(job_id, candidate)
        staged = self.new_staging_file(job_id, stage, suffix=candidate.suffix)
        shutil.copy2(candidate, staged)
        return self.promote_file(
            job_id,
            stage,
            staged,
            final_name=candidate.name,
            media_kind=media_kind,
            artifact_kind=artifact_kind,
            expected_duration=expected_duration,
            input_hashes=input_hashes,
            config_hash=config_hash,
            details=details,
            publish_allowed=publish_allowed,
        )

    def promote_frame_directory(
        self,
        job_id: str,
        stage: str,
        staged_directory: str | Path,
        *,
        final_name: str,
        expected_count: int,
        dimensions: tuple[int, int],
        prefix: str = "",
        digits: int = 5,
        input_hashes: Mapping[str, str] | None = None,
        config_hash: str = "",
        details: Mapping[str, Any] | None = None,
        publish_allowed: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Publish a newly rendered exact PNG sequence as an immutable version."""
        stage = self._stage(stage)
        staged = self._confined_staging(job_id, staged_directory)
        try:
            artifact = self._inspect_frames(
                staged,
                expected_count=expected_count,
                dimensions=dimensions,
                prefix=prefix,
                digits=digits,
            )
        except BaseException:
            if staged.exists():
                shutil.rmtree(staged)
            raise
        return self._publish(
            job_id,
            stage,
            staged,
            artifact,
            logical_name=final_name,
            is_directory=True,
            input_hashes=input_hashes,
            config_hash=config_hash,
            details=details,
            publish_allowed=publish_allowed,
        )

    def verify_frame_directory(
        self,
        directory: str | Path,
        *,
        expected_count: int,
        dimensions: tuple[int, int],
        prefix: str = "",
        digits: int = 5,
    ) -> dict[str, Any]:
        """Validate an unpromoted exact frame sequence."""
        return self._inspect_frames(
            Path(directory),
            expected_count=expected_count,
            dimensions=dimensions,
            prefix=prefix,
            digits=digits,
        )

    def promote_directory(
        self,
        job_id: str,
        stage: str,
        staged_directory: str | Path,
        *,
        final_name: str,
        input_hashes: Mapping[str, str] | None = None,
        config_hash: str = "",
        details: Mapping[str, Any] | None = None,
        publish_allowed: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Publish a validated generic directory tree as an immutable version."""
        stage = self._stage(stage)
        staged = self._confined_staging(job_id, staged_directory)
        try:
            artifact = self._inspect_directory(staged)
        except BaseException:
            if staged.exists():
                shutil.rmtree(staged)
            raise
        return self._publish(
            job_id,
            stage,
            staged,
            artifact,
            logical_name=final_name,
            is_directory=True,
            input_hashes=input_hashes,
            config_hash=config_hash,
            details=details,
            publish_allowed=publish_allowed,
        )

    def validate(
        self,
        manifest: Mapping[str, Any],
        *,
        input_hashes: Mapping[str, str] | None = None,
        config_hash: str | None = None,
    ) -> bool:
        """Revalidate a manifest, its hashes, and the current artifact bytes."""
        try:
            if int(manifest.get("manifest_version", -1)) != self.manifest_version:
                return False
            job_id = validate_job_id(manifest.get("job_id"))
            stage = self._stage(str(manifest.get("stage", "")))
            details = manifest.get("details")
            embedded = bool(
                isinstance(details, Mapping)
                and details.get("parent_stage") == "composite"
                and stage.startswith("composite.")
            )
            pointer_stage = "composite" if embedded else stage
            current_path = self.manifest_path(job_id, pointer_stage)
            current = json.loads(current_path.read_text(encoding="utf-8"))
            if embedded:
                if not self.validate(current):
                    return False
                if (
                    current.get("version") != manifest.get("version")
                    or details.get("parent_version") != current.get("version")
                ):
                    return False
                parent_artifact = current.get("artifact")
                child_artifact = manifest.get("artifact")
                if not isinstance(parent_artifact, Mapping) or not isinstance(
                    child_artifact, Mapping
                ):
                    return False
                child_path = Path(str(child_artifact.get("path", "")))
                parent_path = Path(str(parent_artifact.get("path", "")))
                if child_path == parent_path:
                    return False
                child_path.relative_to(parent_path)
            elif current != dict(manifest):
                return False
            if not isinstance(manifest.get("version"), str):
                return False
            if input_hashes is not None and dict(
                manifest.get("input_hashes", {})
            ) != dict(input_hashes):
                return False
            if config_hash is not None and manifest.get("config_hash") != config_hash:
                return False
            artifact = manifest.get("artifact")
            if not isinstance(artifact, Mapping):
                return False
            path = Path(self.root, str(artifact["path"]))
            self._require_in_run(job_id, path)
            kind = artifact.get("kind")
            if kind == "frames":
                actual = self._inspect_frames(
                    path,
                    expected_count=int(artifact["frame_count"]),
                    dimensions=(int(artifact["width"]), int(artifact["height"])),
                    prefix=str(artifact.get("prefix", "")),
                    digits=int(artifact.get("digits", 5)),
                )
                return actual["sha256"] == artifact.get("sha256")
            if kind == "directory":
                actual = self._inspect_directory(path)
                return actual["sha256"] == artifact.get("sha256") and actual[
                    "files"
                ] == artifact.get("files")
            expected_duration = artifact.get("expected_duration")
            actual = self._inspect_file(
                path,
                str(kind or "file"),
                expected_duration=(
                    float(expected_duration)
                    if expected_duration is not None
                    else None
                ),
            )
            if actual["size"] != artifact.get("size"):
                return False
            if actual["sha256"] != artifact.get("sha256"):
                return False
            if artifact.get("duration") is not None:
                return actual.get("duration", 0) > 0
            return True
        except (KeyError, OSError, TypeError, ValueError, ArtifactValidationError):
            return False

    def load_json(self, manifest: Mapping[str, Any]) -> Any:
        path = self.artifact_path(manifest)
        return json.loads(path.read_text(encoding="utf-8"))

    def artifact_path(self, manifest: Mapping[str, Any]) -> Path:
        if not self.validate(manifest):
            raise ArtifactValidationError("Artifact manifest is not the current version")
        artifact = manifest.get("artifact") or {}
        path = Path(self.root, str(artifact["path"]))
        self._require_in_run(str(manifest.get("job_id")), path)
        return path

    def _manifest(
        self,
        job_id: str,
        stage: str,
        artifact: Mapping[str, Any],
        *,
        input_hashes: Mapping[str, str] | None,
        config_hash: str,
        details: Mapping[str, Any] | None,
        version: str,
    ) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "version": version,
            "job_id": validate_job_id(job_id),
            "stage": stage,
            "input_hashes": dict(sorted((input_hashes or {}).items())),
            "config_hash": str(config_hash),
            "artifact": dict(artifact),
            "details": dict(details or {}),
        }

    def _publish(
        self,
        job_id: str,
        stage: str,
        staged: Path,
        artifact: dict[str, Any],
        *,
        logical_name: str,
        is_directory: bool,
        input_hashes: Mapping[str, str] | None,
        config_hash: str,
        details: Mapping[str, Any] | None,
        publish_allowed: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        if Path(logical_name).name != logical_name or not logical_name:
            raise ValueError("Artifact logical name must be a simple generated name")
        version = uuid.uuid4().hex
        versions = self.path(job_id, "versions", stage)
        versions.mkdir(parents=True, exist_ok=True)
        bundle_partial = versions / f".{version}.partial"
        bundle = versions / version
        bundle_partial.mkdir()
        suffix = "" if is_directory else Path(logical_name).suffix
        artifact_name = "artifact" if is_directory else f"artifact{suffix}"
        partial_artifact = bundle_partial / artifact_name
        final_artifact = bundle / artifact_name
        os.replace(staged, partial_artifact)
        artifact["path"] = self._relative(final_artifact)
        merged_details = {"logical_name": logical_name, **dict(details or {})}
        manifest = self._manifest(
            job_id,
            stage,
            artifact,
            input_hashes=input_hashes,
            config_hash=config_hash,
            details=merged_details,
            version=version,
        )
        manifest_partial = bundle_partial / "manifest.json"
        self._write_json_file(manifest_partial, manifest)
        journal = self.path(job_id, "journals", f"{stage}.journal")
        previous = self._pointer_or_none(job_id, stage)
        self._write_json_file(
            journal,
            {
                "job_id": job_id,
                "stage": stage,
                "new_version": version,
                "previous_version": previous.get("version") if previous else None,
            },
        )
        self._checkpoint("journal_written")
        os.replace(bundle_partial, bundle)
        self._fsync_directory(versions)
        self._checkpoint("bundle_installed")
        final_manifest = bundle / "manifest.json"
        pointer = {
            "manifest_version": self.manifest_version,
            "job_id": job_id,
            "stage": stage,
            "version": version,
            "manifest_path": self._relative(final_manifest),
            "manifest_sha256": self.hash_file(final_manifest),
        }
        with self._publication_lock(job_id, stage):
            try:
                if publish_allowed is not None and not publish_allowed():
                    raise asyncio.CancelledError(
                        "Artifact publication lease is no longer owned"
                    )
            except asyncio.CancelledError:
                self._remove_path(bundle)
                journal.unlink(missing_ok=True)
                self._fsync_directory(versions)
                self._fsync_directory(journal.parent)
                raise
            self._write_json_file(self._pointer_path(job_id, stage), pointer)
        self._checkpoint("pointer_replaced")
        journal.unlink(missing_ok=True)
        self._fsync_directory(journal.parent)
        return manifest

    @contextmanager
    def _publication_lock(self, job_id: str, stage: str):
        directory = self.path(job_id, ".locks")
        directory.mkdir(parents=True, exist_ok=True)
        lock_path = self.path(job_id, ".locks", f"{self._stage(stage)}.lock")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _inspect_file(
        self,
        path: Path,
        kind: str,
        *,
        expected_duration: float | None = None,
    ) -> dict[str, Any]:
        self._reject_symlink_components(path, self.root)
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError as exc:
            raise ArtifactValidationError("Artifact file is missing") from exc
        if stat.S_ISLNK(mode):
            raise ArtifactValidationError("Artifact file is a symlink")
        if not stat.S_ISREG(mode):
            raise ArtifactValidationError("Artifact file is missing")
        size = path.stat().st_size
        if size <= 0:
            raise ArtifactValidationError("Artifact file is empty")
        artifact: dict[str, Any] = {
            "kind": kind,
            "path": "",
            "size": size,
            "sha256": self.hash_file(path),
        }
        if kind in {"audio", "video"}:
            duration = self._duration(path, kind)
            if duration is not None:
                if duration <= 0:
                    raise ArtifactValidationError(
                        f"{kind.capitalize()} artifact has no positive duration"
                    )
                artifact["duration"] = duration
            if expected_duration is not None:
                expected = float(expected_duration)
                if expected <= 0:
                    raise ArtifactValidationError("Expected media duration must be positive")
                if duration is None:
                    raise ArtifactValidationError(
                        f"{kind.capitalize()} duration could not be validated"
                    )
                tolerance = max(0.02, expected * 0.01)
                if abs(duration - expected) > tolerance:
                    raise ArtifactValidationError(
                        f"{kind.capitalize()} duration does not match expected duration"
                    )
                artifact["expected_duration"] = expected
        elif expected_duration is not None:
            raise ArtifactValidationError(
                "Expected duration is only valid for media artifacts"
            )
        return artifact

    def _inspect_frames(
        self,
        directory: Path,
        *,
        expected_count: int,
        dimensions: tuple[int, int],
        prefix: str,
        digits: int,
    ) -> dict[str, Any]:
        if expected_count < 1:
            raise ArtifactValidationError("A frame sequence must not be empty")
        self._reject_symlink_tree(directory)
        if not directory.is_dir():
            raise ArtifactValidationError("Frame directory is missing")
        expected = [
            f"{prefix}{index:0{digits}d}.png" for index in range(expected_count)
        ]
        actual = sorted(path.name for path in directory.iterdir())
        if actual != expected:
            raise ArtifactValidationError(
                "Frame sequence is missing frames or contains stale extra frames"
            )
        width, height = (int(dimensions[0]), int(dimensions[1]))
        digest = hashlib.sha256()
        total_size = 0
        for name in expected:
            path = directory / name
            if path.stat().st_size <= 0:
                raise ArtifactValidationError("Frame file is empty")
            with Image.open(path) as image:
                if image.size != (width, height):
                    raise ArtifactValidationError("Frame dimensions do not match")
                image.verify()
            total_size += path.stat().st_size
            digest.update(name.encode("utf-8"))
            digest.update(bytes.fromhex(self.hash_file(path)))
        return {
            "kind": "frames",
            "path": "",
            "frame_count": expected_count,
            "width": width,
            "height": height,
            "prefix": prefix,
            "digits": digits,
            "size": total_size,
            "sha256": digest.hexdigest(),
        }

    def _inspect_directory(self, directory: Path) -> dict[str, Any]:
        self._reject_symlink_tree(directory)
        if not directory.is_dir():
            raise ArtifactValidationError("Artifact directory is missing")
        paths: list[Path] = []
        for current, directories, filenames in os.walk(directory, followlinks=False):
            current_path = Path(current)
            for name in directories:
                child = current_path / name
                if stat.S_ISLNK(child.lstat().st_mode):
                    raise ArtifactValidationError("Artifact directory contains a symlink")
            for name in filenames:
                child = current_path / name
                mode = child.lstat().st_mode
                if stat.S_ISLNK(mode):
                    raise ArtifactValidationError("Artifact directory contains a symlink")
                if not stat.S_ISREG(mode):
                    raise ArtifactValidationError("Artifact directory contains a non-file")
                paths.append(child)
        paths.sort()
        if not paths:
            raise ArtifactValidationError("Artifact directory is empty")
        digest = hashlib.sha256()
        files: dict[str, dict[str, Any]] = {}
        total_size = 0
        for path in paths:
            relative = path.relative_to(directory).as_posix()
            size = path.stat().st_size
            if size <= 0:
                raise ArtifactValidationError(
                    "Artifact directory contains an empty file"
                )
            file_hash = self.hash_file(path)
            files[relative] = {"size": size, "sha256": file_hash}
            total_size += size
            digest.update(relative.encode("utf-8"))
            digest.update(bytes.fromhex(file_hash))
        return {
            "kind": "directory",
            "path": "",
            "size": total_size,
            "sha256": digest.hexdigest(),
            "files": files,
        }

    def _duration(self, path: Path, kind: str) -> float | None:
        try:
            if self._probe_duration is not None:
                value = self._probe_duration(path)
                return None if value is None else float(value)
            executable = shutil.which("ffprobe")
            if executable is None:
                return None
            result = subprocess.run(
                [
                    executable,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                timeout=15,
            )
            return float(result.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise ArtifactValidationError(
                f"Media probe failed for {kind} artifact ({type(exc).__name__})"
            ) from exc

    def _warn(self, message: str) -> None:
        if self._warning_callback is not None:
            self._warning_callback(message)

    def _confined_staging(self, job_id: str, path: str | Path) -> Path:
        candidate = Path(os.path.abspath(path))
        self._require_in_run(job_id, candidate)
        staging_root = self.path(job_id, ".staging")
        try:
            candidate.relative_to(staging_root)
        except ValueError as exc:
            raise ValueError(
                "Staged artifact is outside the generated staging area"
            ) from exc
        return candidate

    def _require_in_run(self, job_id: str, path: Path) -> None:
        run = Path(os.path.abspath(self.root / validate_job_id(job_id)))
        candidate = Path(os.path.abspath(path))
        try:
            candidate.relative_to(run)
        except ValueError as exc:
            raise ValueError("Artifact path escapes its generated run") from exc
        self._reject_symlink_components(candidate, self.root)

    def _relative(self, path: Path) -> str:
        candidate = Path(os.path.abspath(path))
        self._reject_symlink_components(candidate, self.root)
        return candidate.relative_to(self.root).as_posix()

    def _pointer_path(self, job_id: str, stage: str) -> Path:
        return self.path(job_id, "current", f"{self._stage(stage)}.json")

    def _pointer_or_none(self, job_id: str, stage: str) -> dict[str, Any] | None:
        try:
            return self._read_pointer(job_id, stage)
        except FileNotFoundError:
            return None

    def _read_pointer(self, job_id: str, stage: str) -> dict[str, Any]:
        path = self._pointer_path(job_id, stage)
        self._reject_symlink_components(path, self.root)
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("manifest_version") != self.manifest_version
            or value.get("job_id") != validate_job_id(job_id)
            or value.get("stage") != self._stage(stage)
            or not isinstance(value.get("version"), str)
        ):
            raise ArtifactValidationError("Current artifact pointer is invalid")
        return value

    def _recover_journal(self, job_id: str, journal: Path) -> None:
        self._reject_symlink_components(journal, self.root)
        try:
            value = json.loads(journal.read_text(encoding="utf-8"))
            stage = self._stage(str(value["stage"]))
            version = str(value["new_version"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            journal.unlink(missing_ok=True)
            return
        pointer = self._pointer_or_none(job_id, stage)
        if pointer is None or pointer.get("version") != version:
            self._remove_path(self.path(job_id, "versions", stage, version))
        journal.unlink(missing_ok=True)
        self._fsync_directory(journal.parent)

    def _write_json_file(self, path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._reject_symlink_components(path, self.root)
        partial = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
        try:
            with partial.open("w", encoding="utf-8") as stream:
                json.dump(value, stream, sort_keys=True, indent=2, default=_json_default)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(partial, path)
            self._fsync_directory(path.parent)
        finally:
            partial.unlink(missing_ok=True)

    def _checkpoint(self, name: str) -> None:
        if self._promotion_checkpoint is not None:
            self._promotion_checkpoint(name)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _remove_path(path: Path) -> None:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    @staticmethod
    def _reject_symlink_components(path: Path, root: Path) -> None:
        root = Path(os.path.abspath(root))
        candidate = Path(os.path.abspath(path))
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("Artifact path escapes its generated root") from exc
        current = root
        for part in relative.parts:
            current /= part
            try:
                if stat.S_ISLNK(current.lstat().st_mode):
                    raise ArtifactValidationError("Generated artifact path contains a symlink")
            except FileNotFoundError:
                continue

    def _reject_symlink_tree(self, root: Path) -> None:
        self._reject_symlink_components(root, self.root)
        try:
            mode = root.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode):
            raise ArtifactValidationError("Generated artifact tree is a symlink")
        if not stat.S_ISDIR(mode):
            return
        for current, directories, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in (*directories, *filenames):
                child = current_path / name
                if stat.S_ISLNK(child.lstat().st_mode):
                    raise ArtifactValidationError("Generated artifact tree contains a symlink")

    @staticmethod
    def _stage(stage: str) -> str:
        if not isinstance(stage, str) or not _STAGE_RE.fullmatch(stage):
            raise ValueError("Invalid stage name")
        return stage


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set | tuple):
        return list(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
