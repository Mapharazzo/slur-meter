"""Validated, resumable generation artifacts with atomic promotion."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from PIL import Image

from api.settings import confined_path, validate_job_id

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
    ) -> None:
        self.root = Path(root).resolve()
        self.manifest_version = int(manifest_version)
        self._probe_duration = probe_duration
        self._warning_callback = warning_callback

    def path(self, job_id: str, *parts: str | Path) -> Path:
        """Return a confined generated run path."""
        validated = validate_job_id(job_id)
        return confined_path(self.root, validated, *parts)

    def manifest_path(self, job_id: str, stage: str) -> Path:
        stage = self._stage(stage)
        return self.path(job_id, "manifests", f"{stage}.json")

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
        input_hashes: Mapping[str, str] | None = None,
        config_hash: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate and atomically replace one file plus its manifest."""
        stage = self._stage(stage)
        staged = self._confined_staging(job_id, staged_path)
        kind = media_kind or artifact_kind or "file"
        try:
            artifact = self._inspect_file(staged, kind)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        target = self.path(job_id, stage, final_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = self._backup_path(target)
        had_target = target.exists()
        if had_target:
            os.replace(target, backup)
        try:
            os.replace(staged, target)
            artifact["path"] = self._relative(target)
            manifest = self._manifest(
                job_id,
                stage,
                artifact,
                input_hashes=input_hashes,
                config_hash=config_hash,
                details=details,
            )
            self._write_manifest(job_id, stage, manifest)
        except BaseException:
            target.unlink(missing_ok=True)
            if had_target and backup.exists():
                os.replace(backup, target)
            raise
        backup.unlink(missing_ok=True)
        return manifest

    def record_file(
        self,
        job_id: str,
        stage: str,
        path: str | Path,
        *,
        media_kind: str | None = None,
        artifact_kind: str | None = None,
        input_hashes: Mapping[str, str] | None = None,
        config_hash: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write a manifest for an already atomically produced confined file."""
        stage = self._stage(stage)
        candidate = Path(path).resolve()
        self._require_in_run(job_id, candidate)
        artifact = self._inspect_file(candidate, media_kind or artifact_kind or "file")
        artifact["path"] = self._relative(candidate)
        manifest = self._manifest(
            job_id,
            stage,
            artifact,
            input_hashes=input_hashes,
            config_hash=config_hash,
            details=details,
        )
        self._write_manifest(job_id, stage, manifest)
        return manifest

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
    ) -> dict[str, Any]:
        """Promote a newly rendered exact PNG sequence, removing any stale tail."""
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
        target = self.path(job_id, stage, final_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = self._backup_path(target)
        had_target = target.exists()
        if had_target:
            os.replace(target, backup)
        try:
            os.replace(staged, target)
            artifact["path"] = self._relative(target)
            manifest = self._manifest(
                job_id,
                stage,
                artifact,
                input_hashes=input_hashes,
                config_hash=config_hash,
                details=details,
            )
            self._write_manifest(job_id, stage, manifest)
        except BaseException:
            if target.exists():
                shutil.rmtree(target)
            if had_target and backup.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        return manifest

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
    ) -> dict[str, Any]:
        """Atomically replace a validated generic directory tree."""
        stage = self._stage(stage)
        staged = self._confined_staging(job_id, staged_directory)
        try:
            artifact = self._inspect_directory(staged)
        except BaseException:
            if staged.exists():
                shutil.rmtree(staged)
            raise
        target = self.path(job_id, stage, final_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = self._backup_path(target)
        had_target = target.exists()
        if had_target:
            os.replace(target, backup)
        try:
            os.replace(staged, target)
            artifact["path"] = self._relative(target)
            manifest = self._manifest(
                job_id,
                stage,
                artifact,
                input_hashes=input_hashes,
                config_hash=config_hash,
                details=details,
            )
            self._write_manifest(job_id, stage, manifest)
        except BaseException:
            if target.exists():
                shutil.rmtree(target)
            if had_target and backup.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        return manifest

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
            self._stage(str(manifest.get("stage", "")))
            if input_hashes is not None and dict(
                manifest.get("input_hashes", {})
            ) != dict(input_hashes):
                return False
            if config_hash is not None and manifest.get("config_hash") != config_hash:
                return False
            artifact = manifest.get("artifact")
            if not isinstance(artifact, Mapping):
                return False
            path = (self.root / str(artifact["path"])).resolve()
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
            actual = self._inspect_file(path, str(kind or "file"))
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
        artifact = manifest.get("artifact") or {}
        path = (self.root / str(artifact["path"])).resolve()
        self._require_in_run(str(manifest.get("job_id")), path)
        return json.loads(path.read_text(encoding="utf-8"))

    def artifact_path(self, manifest: Mapping[str, Any]) -> Path:
        artifact = manifest.get("artifact") or {}
        path = (self.root / str(artifact["path"])).resolve()
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
    ) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "job_id": validate_job_id(job_id),
            "stage": stage,
            "input_hashes": dict(sorted((input_hashes or {}).items())),
            "config_hash": str(config_hash),
            "artifact": dict(artifact),
            "details": dict(details or {}),
        }

    def _write_manifest(
        self, job_id: str, stage: str, manifest: Mapping[str, Any]
    ) -> None:
        path = self.manifest_path(job_id, stage)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
        try:
            with partial.open("w", encoding="utf-8") as stream:
                json.dump(
                    manifest, stream, sort_keys=True, indent=2, default=_json_default
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(partial, path)
        finally:
            partial.unlink(missing_ok=True)

    def _inspect_file(self, path: Path, kind: str) -> dict[str, Any]:
        if not path.is_file():
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
        if not directory.is_dir():
            raise ArtifactValidationError("Frame directory is missing")
        expected = [
            f"{prefix}{index:0{digits}d}.png" for index in range(expected_count)
        ]
        actual = sorted(path.name for path in directory.glob("*.png"))
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
        if not directory.is_dir():
            raise ArtifactValidationError("Artifact directory is missing")
        paths = sorted(path for path in directory.rglob("*") if path.is_file())
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
                shell=False,
            )
            return float(result.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self._warn(
                f"ffprobe could not validate {kind} artifact ({type(exc).__name__})."
            )
            return None

    def _warn(self, message: str) -> None:
        if self._warning_callback is not None:
            self._warning_callback(message)

    def _confined_staging(self, job_id: str, path: str | Path) -> Path:
        candidate = Path(path).resolve()
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
        run = self.path(validate_job_id(job_id))
        try:
            path.resolve().relative_to(run)
        except ValueError as exc:
            raise ValueError("Artifact path escapes its generated run") from exc

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    @staticmethod
    def _backup_path(path: Path) -> Path:
        return path.with_name(f".{path.name}.{uuid.uuid4().hex}.backup")

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
