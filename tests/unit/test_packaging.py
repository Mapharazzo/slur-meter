"""Guard rails for the Docker/packaging surface.

These tests are a lightweight scan — not a full dockerignore engine. They assert
that `.dockerignore` is tracked in Git and that it excludes the sensitive and
bulky paths while still shipping `uv.lock` so image builds resolve dependencies
reproducibly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def _tracked_files() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return set(result.stdout.split())


def _ignore_patterns() -> list[str]:
    lines = DOCKERIGNORE.read_text().splitlines()
    return [
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]


def test_dockerignore_is_tracked() -> None:
    assert ".dockerignore" in _tracked_files()


def test_dockerignore_excludes_sensitive_and_bulky_paths() -> None:
    patterns = set(_ignore_patterns())
    required = {
        ".env",  # secrets
        ".git",  # version control
        ".venv",  # python virtualenv
        "__pycache__",  # python caches
        ".pytest_cache",  # test caches
        "results",  # generated artifacts
        "output",  # generated artifacts
        "data",  # runtime database
        "webui/node_modules",  # node modules
        "webui/dist",  # built UI (rebuilt in image)
    }
    missing = required - patterns
    assert not missing, f".dockerignore is missing exclusions for: {sorted(missing)}"


def test_dockerignore_keeps_uv_lock() -> None:
    # uv.lock must NOT be ignored: the image installs from it.
    patterns = _ignore_patterns()
    for pattern in patterns:
        stripped = pattern.lstrip("/").rstrip("/")
        assert stripped != "uv.lock", "uv.lock must not be excluded from the image"

    # And it must actually be committed so the build context contains it.
    assert "uv.lock" in _tracked_files()
