"""Durable, idempotent publishing orchestration through injected clients."""

from __future__ import annotations

import math
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from api.domain import AttemptTrigger, FailureCategory
from api.errors import AmbiguousPublishOutcome, OperationalError, classify_exception
from src.publishing.errors import (
    PlatformConfirmationError,
    PlatformStatsError,
    normalized_remote_id,
)
from src.publishing.metadata import generate_metadata

_PLATFORMS = frozenset({"youtube", "tiktok", "instagram"})
_YOUTUBE_PRIVACY = frozenset({"private", "unlisted", "public"})
_METRIC_FIELDS = ("views", "likes", "comments", "shares", "revenue_usd")
_LIVE_ATTEMPTS: set[tuple[str, int]] = set()
_LIVE_ATTEMPTS_LOCK = threading.RLock()


class PublishingClient(Protocol):
    """Minimum injected platform interface used by the workflow."""

    def upload(self, video_path: str | Path, **metadata: Any) -> object: ...

    def get_video_stats(self, remote_id: str) -> Mapping[str, Any]: ...


class PublishingStore(Protocol):
    def request_publication(
        self, job_id: str, platform: str, **fields: Any
    ) -> tuple[dict[str, Any], bool]: ...

    def recover_expired_publishing_attempt(
        self, job_id: str, platform: str
    ) -> tuple[dict[str, Any], bool]: ...

    def renew_publishing_attempt_lease(
        self, attempt_id: int, owner: str, *, lease_seconds: float
    ) -> bool: ...

    def claim_publishing_attempt(
        self, job_id: str, platform: str, **fields: Any
    ) -> tuple[dict[str, Any] | None, bool, dict[str, Any]]: ...

    def complete_publishing_attempt(
        self, attempt_id: int, **fields: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...

    def reconcile_publication(
        self, job_id: str, platform: str, **fields: Any
    ) -> dict[str, Any]: ...

    def store_publishing_stats(
        self,
        job_id: str,
        platform: str,
        date: str,
        metrics: Mapping[str, int | float],
    ) -> dict[str, Any]: ...

    def list_releases(self, job_id: str | None = None) -> list[dict[str, Any]]: ...

    def get_job_detail(self, job_id: str) -> dict[str, Any] | None: ...

    def record_event(self, job_id: str, **fields: Any) -> object | None: ...


class PublishingService:
    """Persist and execute explicit, bounded platform publishing operations."""

    def __init__(
        self,
        store: PublishingStore,
        clients: Mapping[str, PublishingClient],
        *,
        metadata_factory: Callable[[str, dict[str, Any]], Mapping[str, Any]] = generate_metadata,
        sleep: Callable[[float], object] = time.sleep,
        retry_delays: tuple[float, ...] = (1.0, 3.0, 8.0),
        date_factory: Callable[[], str] | None = None,
        max_attempts: int = 3,
        lease_seconds: float = 300.0,
        heartbeat_interval: float = 30.0,
    ) -> None:
        if max_attempts != 3:
            raise ValueError("Publishing automatic attempts are fixed at three")
        self.store = store
        self.clients = {str(key).lower(): value for key, value in clients.items()}
        self.metadata_factory = metadata_factory
        self.sleep = sleep
        self.retry_delays = tuple(float(value) for value in retry_delays)
        self.date_factory = date_factory or (
            lambda: datetime.now(UTC).date().isoformat()
        )
        self.max_attempts = max_attempts
        if lease_seconds <= 0 or heartbeat_interval <= 0:
            raise ValueError("Publishing lease and heartbeat durations must be positive")
        if heartbeat_interval >= lease_seconds:
            raise ValueError("Publishing heartbeat must be shorter than its lease")
        self.lease_seconds = float(lease_seconds)
        self.heartbeat_interval = float(heartbeat_interval)
        self._lease_owner = f"publishing-service-{uuid.uuid4().hex}"

    def request(
        self,
        job_id: str,
        platform: str,
        *,
        title: str,
        summary: Mapping[str, Any],
        privacy_status: str | None = None,
    ) -> dict[str, Any]:
        """Persist an explicit publication request and immutable metadata snapshot."""
        normalized = self._platform(platform)

        def create_metadata() -> Mapping[str, Any]:
            metadata = dict(self.metadata_factory(str(title), dict(summary)))
            if not metadata:
                raise ValueError("Publishing metadata generation returned no metadata")
            if normalized == "youtube":
                privacy = privacy_status or "private"
                if privacy not in _YOUTUBE_PRIVACY:
                    raise ValueError("YouTube privacy must be private, unlisted, or public")
                metadata["privacy_status"] = privacy
            return metadata

        release, _ = self.store.request_publication(
            job_id, normalized, metadata_factory=create_metadata
        )
        return release

    def publish(
        self,
        job_id: str,
        platform: str,
        video_path: str | Path,
    ) -> dict[str, Any]:
        """Execute the initial bounded automatic publishing retry cycle."""
        normalized = self._platform(platform)
        release = self._require_release(job_id, normalized)
        if release["status"] in {"uploaded", "uploading", "needs_attention"}:
            return release
        self._validated_video(video_path)
        return self._run_cycle(
            job_id,
            normalized,
            video_path,
            retry_cycle=1,
            trigger=AttemptTrigger.AUTOMATIC,
        )

    def retry(
        self,
        job_id: str,
        platform: str,
        video_path: str | Path,
        *,
        reconciliation: str | None = None,
        reconciled_remote_id: str | None = None,
    ) -> dict[str, Any]:
        """Start a manual cycle only after a safe terminal or reconciliation."""
        normalized = self._platform(platform)
        release = self._require_release(job_id, normalized)
        if release["status"] == "uploaded":
            return release
        if release["status"] == "uploading":
            if reconciliation is None:
                return release
            detail = self.store.get_job_detail(job_id)
            active = next(
                (
                    row
                    for row in (detail or {}).get("publishing_attempts", [])
                    if row["platform"] == normalized and row["finished_at"] is None
                ),
                None,
            )
            if active is not None and _publishing_attempt_is_live(
                self.store, int(active["id"])
            ):
                return release
            release, recovered = self.store.recover_expired_publishing_attempt(
                job_id, normalized
            )
            if not recovered:
                return release

        ambiguous = (
            release["status"] == "needs_attention"
            and (release.get("safe_error") or {}).get("code")
            in {"ambiguous_publish", "ambiguous_publish_outcome"}
        )
        reconciled_absent = False
        if ambiguous:
            if reconciliation is None:
                return release
            if reconciliation == "uploaded":
                return self.store.reconcile_publication(
                    job_id,
                    normalized,
                    outcome="uploaded",
                    remote_id=reconciled_remote_id,
                )
            if reconciliation != "not_uploaded":
                raise ValueError("Unknown publishing reconciliation outcome")
            release = self.store.reconcile_publication(
                job_id, normalized, outcome="not_uploaded"
            )
            reconciled_absent = True
        elif reconciliation is not None:
            raise ValueError("Reconciliation is only valid for an ambiguous publication")

        detail = self.store.get_job_detail(job_id)
        if detail is None:
            raise KeyError("Run was not found")
        platform_attempts = [
            row
            for row in detail["publishing_attempts"]
            if row["platform"] == normalized
        ]
        latest = platform_attempts[-1] if platform_attempts else None
        if not reconciled_absent and (
            release["status"] != "failed"
            or latest is None
            or not latest["retryable"]
        ):
            return release
        self._validated_video(video_path)
        next_cycle = max(
            (int(row["retry_cycle"]) for row in platform_attempts), default=0
        ) + 1
        return self._run_cycle(
            job_id,
            normalized,
            video_path,
            retry_cycle=next_cycle,
            trigger=AttemptTrigger.MANUAL_RETRY,
        )

    def refresh_stats(self, job_id: str, platform: str) -> dict[str, Any]:
        """Fetch and atomically store a complete verified metrics snapshot."""
        normalized = self._platform(platform)
        release = self._require_release(job_id, normalized)
        try:
            remote_id = normalized_remote_id(release.get("remote_id"))
        except ValueError:
            remote_id = None
        if release["status"] != "uploaded" or remote_id is None:
            error = PlatformConfirmationError(
                "Statistics require a confirmed remote publication."
            )
            self._record_stats_failure(job_id, normalized, error)
            raise error
        client = self._client(normalized)
        try:
            raw_metrics = client.get_video_stats(remote_id)
            metrics = _validated_metrics(raw_metrics)
        except Exception as exc:
            error = exc if isinstance(exc, OperationalError) else PlatformStatsError()
            self._record_stats_failure(job_id, normalized, error)
            raise error from None
        return self.store.store_publishing_stats(
            job_id,
            normalized,
            self.date_factory(),
            metrics,
        )

    def _run_cycle(
        self,
        job_id: str,
        platform: str,
        video_path: str | Path,
        *,
        retry_cycle: int,
        trigger: AttemptTrigger,
    ) -> dict[str, Any]:
        client = self._client(platform)
        while True:
            attempt, claimed, release = self.store.claim_publishing_attempt(
                job_id,
                platform,
                retry_cycle=retry_cycle,
                max_attempts=self.max_attempts,
                trigger=trigger,
                lease_owner=self._lease_owner,
                lease_seconds=self.lease_seconds,
            )
            if not claimed or attempt is None:
                return release
            _mark_publishing_attempt_live(self.store, int(attempt["id"]))
            should_retry = False
            try:
                try:
                    remote_id = self._upload_with_heartbeat(
                        client, attempt, video_path
                    )
                    try:
                        remote_id = normalized_remote_id(remote_id)
                    except ValueError:
                        raise AmbiguousPublishOutcome(
                            "The platform did not return a valid remote ID; "
                            "reconcile before retrying."
                        ) from None
                except Exception as exc:
                    error = classify_exception(exc, f"{platform} publishing")
                    is_ambiguous = (
                        error.category is FailureCategory.AMBIGUOUS_PUBLISH
                    )
                    if is_ambiguous:
                        self._complete_owned_attempt(
                            job_id,
                            platform,
                            int(attempt["id"]),
                            outcome="ambiguous",
                            release_status="needs_attention",
                            safe_error_code=error.code,
                            safe_error_message=error.message,
                        )
                        raise error from None
                    final = (
                        not error.retryable
                        or int(attempt["attempt_number"]) >= self.max_attempts
                    )
                    outcome = (
                        "confirmation_failed"
                        if isinstance(error, PlatformConfirmationError)
                        else "failed"
                    )
                    status = (
                        "needs_attention"
                        if not error.retryable
                        else "failed" if final else "retrying"
                    )
                    self._complete_owned_attempt(
                        job_id,
                        platform,
                        int(attempt["id"]),
                        outcome=outcome,
                        release_status=status,
                        retryable=error.retryable,
                        safe_error_code=error.code,
                        safe_error_message=error.message,
                    )
                    if final:
                        raise error from None
                    should_retry = True
                else:
                    _, updated = self._complete_owned_attempt(
                        job_id,
                        platform,
                        int(attempt["id"]),
                        outcome="completed",
                        release_status="uploaded",
                        remote_id=remote_id,
                    )
                    return updated
            finally:
                _unmark_publishing_attempt_live(self.store, int(attempt["id"]))
            if should_retry:
                self.sleep(self._retry_delay(int(attempt["attempt_number"])))

    def _upload_with_heartbeat(
        self,
        client: PublishingClient,
        attempt: Mapping[str, Any],
        video_path: str | Path,
    ) -> object:
        stop = threading.Event()
        lost = threading.Event()

        def heartbeat() -> None:
            while not stop.wait(self.heartbeat_interval):
                try:
                    renewed = self.store.renew_publishing_attempt_lease(
                        int(attempt["id"]),
                        self._lease_owner,
                        lease_seconds=self.lease_seconds,
                    )
                except Exception:
                    lost.set()
                    return
                if not renewed:
                    lost.set()
                    return

        worker = threading.Thread(
            target=heartbeat,
            name=f"publishing-heartbeat-{attempt['id']}",
            daemon=True,
        )
        worker.start()
        try:
            remote_id = client.upload(
                video_path, **_upload_metadata(attempt["metadata"])
            )
        finally:
            stop.set()
            worker.join(timeout=max(self.heartbeat_interval, 1.0))
        if lost.is_set():
            raise AmbiguousPublishOutcome(
                "Publishing ownership expired; reconcile the remote result before retrying."
            )
        return remote_id

    def _complete_owned_attempt(
        self,
        job_id: str,
        platform: str,
        attempt_id: int,
        **fields: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            return self.store.complete_publishing_attempt(
                attempt_id,
                lease_owner=self._lease_owner,
                **fields,
            )
        except PermissionError:
            self.store.recover_expired_publishing_attempt(job_id, platform)
            raise AmbiguousPublishOutcome(
                "Publishing ownership expired; reconcile the remote result before retrying."
            ) from None

    def _retry_delay(self, attempt_number: int) -> float:
        if not self.retry_delays:
            return 0.0
        index = min(max(attempt_number - 1, 0), len(self.retry_delays) - 1)
        return self.retry_delays[index]

    def _release(self, job_id: str, platform: str) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in self.store.list_releases(job_id)
                if row["platform"] == platform
            ),
            None,
        )

    def _require_release(self, job_id: str, platform: str) -> dict[str, Any]:
        release = self._release(job_id, platform)
        if release is None:
            raise ValueError("Publishing must be requested before it can run")
        return release

    def _client(self, platform: str) -> PublishingClient:
        try:
            return self.clients[platform]
        except KeyError:
            raise ValueError(f"No publishing client is configured for {platform}") from None

    @staticmethod
    def _platform(platform: str) -> str:
        normalized = str(platform).strip().lower()
        if normalized not in _PLATFORMS:
            raise ValueError("Unsupported publishing platform")
        return normalized

    @staticmethod
    def _validated_video(video_path: str | Path) -> Path:
        path = Path(video_path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError("Publishing requires a validated video artifact")
        return path

    def _record_stats_failure(
        self, job_id: str, platform: str, error: OperationalError
    ) -> None:
        self.store.record_event(
            job_id,
            event_type="publishing_stats_failed",
            severity="warning",
            message="Platform statistics could not be refreshed; prior metrics were preserved.",
            data={"platform": platform, "code": error.code},
        )


def _upload_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Derive client arguments without mutating the persisted snapshot."""
    description = str(metadata.get("description") or "")
    hashtags = metadata.get("hashtags") or []
    if hashtags:
        description = f"{description}\n\n{' '.join(str(tag) for tag in hashtags)}".strip()
    result = {
        "title": str(metadata.get("video_title") or metadata.get("title") or ""),
        "description": description,
        "tags": list(metadata.get("tags") or []),
    }
    if "privacy_status" in metadata:
        result["privacy_status"] = metadata["privacy_status"]
    return result


def _validated_metrics(value: Mapping[str, Any]) -> dict[str, int | float]:
    if not isinstance(value, Mapping) or any(field not in value for field in _METRIC_FIELDS):
        raise PlatformStatsError("The platform returned an incomplete statistics snapshot.")
    result: dict[str, int | float] = {}
    for field in _METRIC_FIELDS:
        raw = value[field]
        if field == "revenue_usd":
            valid = (
                not isinstance(raw, bool)
                and isinstance(raw, (int, float))
                and math.isfinite(raw)
                and raw >= 0
            )
        else:
            valid = not isinstance(raw, bool) and isinstance(raw, int) and raw >= 0
        if not valid:
            raise PlatformStatsError("The platform returned an invalid statistics snapshot.")
        result[field] = float(raw) if field == "revenue_usd" else int(raw)
    return result


def _publishing_attempt_key(store: PublishingStore, attempt_id: int) -> tuple[str, int]:
    store_path = getattr(store, "path", None)
    identity = str(Path(store_path).resolve()) if store_path is not None else str(id(store))
    return identity, int(attempt_id)


def _mark_publishing_attempt_live(store: PublishingStore, attempt_id: int) -> None:
    with _LIVE_ATTEMPTS_LOCK:
        _LIVE_ATTEMPTS.add(_publishing_attempt_key(store, attempt_id))


def _unmark_publishing_attempt_live(store: PublishingStore, attempt_id: int) -> None:
    with _LIVE_ATTEMPTS_LOCK:
        _LIVE_ATTEMPTS.discard(_publishing_attempt_key(store, attempt_id))


def _publishing_attempt_is_live(store: PublishingStore, attempt_id: int) -> bool:
    with _LIVE_ATTEMPTS_LOCK:
        return _publishing_attempt_key(store, attempt_id) in _LIVE_ATTEMPTS
