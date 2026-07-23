"""Configuration and identifier/path safety helpers."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from math import isfinite
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_ALLOWED_ORIGINS = ("http://localhost:5173", "http://localhost:8001")
DEFAULT_RETRY_DELAYS = (1.0, 3.0, 8.0)
_JOB_ID_RE = re.compile(r"job_[0-9a-f]{16,64}\Z")
_CANDIDATE_ID_RE = re.compile(r"candidate_[0-9a-f]{16,64}\Z")
_IMDB_ID_RE = re.compile(r"(?:tt)?([0-9]{1,10})\Z", re.IGNORECASE)


@dataclass(frozen=True)
class Settings:
    """Runtime settings with safe local defaults and testable retry timing."""

    base_dir: Path
    allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS
    admin_api_token: str | None = None
    allow_local_development_auth: bool = False
    retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS
    subtitle_coverage_threshold: float = 0.70
    subtitle_candidates_per_cycle: int = 3
    data_dir: Path | None = None
    output_dir: Path | None = None
    results_dir: Path | None = None

    def __post_init__(self) -> None:
        base_dir = Path(self.base_dir).resolve()
        origins = tuple(
            str(origin).strip()
            for origin in self.allowed_origins
            if str(origin).strip()
        )
        delays = tuple(float(delay) for delay in self.retry_delays)
        if not origins:
            raise ValueError("At least one allowed origin is required")
        if "*" in origins:
            raise ValueError("Wildcard CORS origins are forbidden with credentials")
        if any(not isfinite(delay) or delay < 0 for delay in delays):
            raise ValueError("Retry delays must be finite and non-negative")
        if not 0 < self.subtitle_coverage_threshold <= 1:
            raise ValueError("Subtitle coverage threshold must be between 0 and 1")
        if self.subtitle_candidates_per_cycle < 1:
            raise ValueError("At least one subtitle candidate is required per cycle")
        object.__setattr__(self, "base_dir", base_dir)
        object.__setattr__(self, "allowed_origins", origins)
        object.__setattr__(self, "retry_delays", delays)
        object.__setattr__(
            self, "data_dir", _resolved_or_default(self.data_dir, base_dir / "data")
        )
        object.__setattr__(
            self,
            "output_dir",
            _resolved_or_default(self.output_dir, base_dir / "output"),
        )
        object.__setattr__(
            self,
            "results_dir",
            _resolved_or_default(self.results_dir, base_dir / "results"),
        )

    @classmethod
    def from_env(cls, base_dir: str | Path) -> Settings:
        """Load a project `.env` without replacing deployment environment values."""
        root = Path(base_dir).resolve()
        load_dotenv(root / ".env", override=False)
        origins = _split_csv(os.getenv("ALLOWED_ORIGINS")) or DEFAULT_ALLOWED_ORIGINS
        return cls(
            base_dir=root,
            allowed_origins=origins,
            admin_api_token=os.getenv("ADMIN_API_TOKEN") or None,
            allow_local_development_auth=_env_bool("ALLOW_LOCAL_DEVELOPMENT_AUTH"),
            retry_delays=_parse_delays(os.getenv("RETRY_DELAYS")),
            subtitle_coverage_threshold=float(
                os.getenv("SUBTITLE_COVERAGE_THRESHOLD", "0.70")
            ),
            subtitle_candidates_per_cycle=int(
                os.getenv("SUBTITLE_CANDIDATES_PER_CYCLE", "3")
            ),
            data_dir=_env_path("DATA_DIR", root),
            output_dir=_env_path("OUTPUT_DIR", root),
            results_dir=_env_path("RESULTS_DIR", root),
        )


def validate_job_id(value: object) -> str:
    """Accept only generated opaque run IDs; never path-like external input."""
    if not isinstance(value, str) or not _JOB_ID_RE.fullmatch(value):
        raise ValueError("Invalid job ID")
    return value


def validate_candidate_id(value: object) -> str:
    """Accept only generated opaque subtitle candidate IDs."""
    if not isinstance(value, str) or not _CANDIDATE_ID_RE.fullmatch(value):
        raise ValueError("Invalid subtitle candidate ID")
    return value


def canonical_imdb_id(value: object) -> str:
    """Normalize provider IMDb values to the safe canonical ``tt`` form."""
    if isinstance(value, bool) or value is None:
        raise ValueError("Invalid IMDb ID")
    text = str(value).strip()
    match = _IMDB_ID_RE.fullmatch(text)
    if not match:
        raise ValueError("Invalid IMDb ID")
    return f"tt{match.group(1).zfill(7)}"


def confined_path(root: str | Path, *parts: str | Path) -> Path:
    """Build a path below ``root``, rejecting absolute and traversal input."""
    resolved_root = Path(root).resolve()
    candidate = resolved_root
    for part in parts:
        part_path = Path(part)
        if part_path.is_absolute() or ".." in part_path.parts:
            raise ValueError("Path component escapes its root")
        candidate /= part_path
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Path escapes its root") from exc
    return resolved_candidate


def _resolved_or_default(value: Path | None, default: Path) -> Path:
    return Path(value if value is not None else default).resolve()


def _split_csv(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def _parse_delays(value: str | None) -> tuple[float, ...]:
    return (
        tuple(float(item) for item in _split_csv(value))
        if value
        else DEFAULT_RETRY_DELAYS
    )


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_path(name: str, root: Path) -> Path | None:
    value = os.getenv(name)
    return (
        root / value
        if value and not Path(value).is_absolute()
        else Path(value)
        if value
        else None
    )
