"""YouTube Data API client with explicit publication confirmation failures."""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from google.auth.exceptions import TransportError
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

from api.errors import AmbiguousPublishOutcome, OperationalError
from src.publishing.errors import (
    PlatformConfirmationError,
    PlatformCredentialsError,
    PlatformStatsError,
    PlatformTransientError,
    normalized_remote_id,
)


class YouTubeClient:
    """Wrap YouTube upload and statistics calls behind a testable boundary."""

    SCOPES = [
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
    ]
    TOKEN_URI = "https://oauth2.googleapis.com/token"

    def __init__(
        self,
        *,
        youtube: Any | None = None,
        media_upload_factory: Callable[..., Any] | None = None,
        youtube_builder: Callable[[], Any] | None = None,
        supplemental_stats: Callable[[str], Mapping[str, Any]] | None = None,
    ) -> None:
        self._youtube = youtube
        self._media_upload_factory = media_upload_factory
        self._youtube_builder = youtube_builder
        self._supplemental_stats = supplemental_stats

    def _build(self) -> None:
        missing = [
            name
            for name in (
                "YOUTUBE_CLIENT_ID",
                "YOUTUBE_CLIENT_SECRET",
                "YOUTUBE_REFRESH_TOKEN",
            )
            if not str(os.getenv(name) or "").strip()
        ]
        if missing:
            raise PlatformCredentialsError(
                "YouTube publishing credentials are not configured."
            )

        try:
            if self._youtube_builder is not None:
                self._youtube = self._youtube_builder()
                if self._youtube is None:
                    raise ValueError("YouTube builder returned no service")
                return
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials(
                token=None,
                refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
                token_uri=self.TOKEN_URI,
                client_id=os.environ["YOUTUBE_CLIENT_ID"],
                client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
                scopes=self.SCOPES,
            )
            creds.refresh(Request())
            self._youtube = build("youtube", "v3", credentials=creds)
        except OperationalError:
            raise
        except (
            TimeoutError,
            ConnectionError,
            TransportError,
            RequestsConnectionError,
            RequestsTimeout,
        ) as exc:
            raise PlatformTransientError(technical_detail=type(exc).__name__) from None
        except Exception as exc:
            raise PlatformCredentialsError(technical_detail=type(exc).__name__) from None

    @property
    def youtube(self) -> Any:
        if self._youtube is None:
            self._build()
        return self._youtube

    def upload(
        self,
        video_path: str | Path,
        title: str,
        description: str,
        tags: list[str] | None = None,
        category_id: str = "24",
        privacy_status: str = "private",
        **_: Any,
    ) -> str:
        """Upload a Short and require an explicit non-empty video ID."""
        if privacy_status not in {"private", "unlisted", "public"}:
            raise ValueError("YouTube privacy must be private, unlisted, or public")
        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags or [],
                "categoryId": category_id,
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": False,
            },
        }
        try:
            media_factory = self._media_upload_factory
            if media_factory is None:
                from googleapiclient.http import MediaFileUpload

                media_factory = MediaFileUpload
            media = media_factory(
                str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4"
            )
            request = self.youtube.videos().insert(
                part=",".join(body), body=body, media_body=media
            )
        except OperationalError:
            raise
        except Exception as exc:
            raise PlatformTransientError(technical_detail=type(exc).__name__) from None
        response: Mapping[str, Any] | None = None
        try:
            while response is None:
                _, response = request.next_chunk()
        except OperationalError:
            raise
        except Exception as exc:
            raise AmbiguousPublishOutcome(
                "YouTube may have accepted the upload; reconcile it before retrying.",
                technical_detail=type(exc).__name__,
            ) from None
        if not isinstance(response, Mapping):
            raise AmbiguousPublishOutcome(
                "YouTube returned an invalid upload confirmation; reconcile before retrying."
            )
        try:
            video_id = normalized_remote_id(response.get("id"))
        except ValueError:
            raise AmbiguousPublishOutcome(
                "YouTube did not return a video ID; reconcile before retrying."
            ) from None
        return video_id

    def get_video_stats(self, video_id: str) -> dict[str, int | float]:
        """Return a complete snapshot or raise instead of synthesizing success."""
        try:
            normalized = normalized_remote_id(video_id)
        except ValueError:
            raise PlatformConfirmationError(
                "YouTube statistics require a confirmed video ID.",
                actions=("reconcile_publishing",),
            ) from None
        try:
            result = (
                self.youtube.videos()
                .list(part="statistics", id=normalized)
                .execute()
            )
        except OperationalError:
            raise
        except Exception as exc:
            raise PlatformStatsError(technical_detail=type(exc).__name__) from None
        items = result.get("items", []) if isinstance(result, Mapping) else []
        if not items:
            raise PlatformStatsError(
                "YouTube did not return statistics for the confirmed video."
            )
        try:
            stats = items[0]["statistics"]
            views = int(stats["viewCount"])
            likes = int(stats["likeCount"])
            comments = int(stats["commentCount"])
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise PlatformStatsError(
                "YouTube returned an invalid statistics snapshot.",
                technical_detail=type(exc).__name__,
            ) from None
        if min(views, likes, comments) < 0:
            raise PlatformStatsError("YouTube returned negative statistics.")
        supplemental = _verified_supplemental_stats(
            self._supplemental_stats, normalized, ("shares", "revenue_usd")
        )
        return {
            "views": views,
            "likes": likes,
            "comments": comments,
            "revenue_usd": float(supplemental["revenue_usd"]),
            "shares": int(supplemental["shares"]),
        }

    @staticmethod
    def get_video_url(video_id: str) -> str:
        return f"https://www.youtube.com/shorts/{video_id}"


def _verified_supplemental_stats(
    provider: Callable[[str], Mapping[str, Any]] | None,
    remote_id: str,
    fields: tuple[str, ...],
) -> Mapping[str, int | float]:
    if provider is None:
        raise PlatformStatsError(
            "Complete platform statistics are not configured."
        )
    try:
        values = provider(remote_id)
    except OperationalError:
        raise
    except PlatformStatsError:
        raise
    except Exception as exc:
        raise PlatformStatsError(technical_detail=type(exc).__name__) from None
    if not isinstance(values, Mapping) or any(field not in values for field in fields):
        raise PlatformStatsError(
            "The supplemental statistics snapshot is incomplete."
        )
    result: dict[str, int | float] = {}
    for field in fields:
        value = values[field]
        if field == "revenue_usd":
            valid = (
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(value)
                and value >= 0
            )
        else:
            valid = (
                not isinstance(value, bool)
                and isinstance(value, int)
                and value >= 0
            )
        if not valid:
            raise PlatformStatsError(
                "The supplemental statistics snapshot is invalid."
            )
        result[field] = value
    return result
