"""TikTok publishing client with explicit typed failures."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from api.errors import AmbiguousPublishOutcome
from src.publishing.errors import (
    PlatformConfirmationError,
    PlatformCredentialsError,
    PlatformStatsError,
    PlatformTransientError,
    normalized_remote_id,
)
from src.publishing.youtube import _verified_supplemental_stats


def _parse_count(text: str) -> int:
    """Parse TikTok-style counts and reject malformed values."""
    normalized = str(text).strip().upper().replace(",", "")
    try:
        if normalized.endswith("K"):
            return int(float(normalized[:-1]) * 1_000)
        if normalized.endswith("M"):
            return int(float(normalized[:-1]) * 1_000_000)
        return int(normalized)
    except (ValueError, IndexError) as exc:
        raise PlatformStatsError("TikTok returned invalid statistics.") from exc


class TikTokClient:
    """Playwright-backed TikTok uploader and statistics reader."""

    def __init__(
        self,
        *,
        playwright_factory: Callable[[], Any] | None = None,
        supplemental_stats: Callable[[str], Mapping[str, Any]] | None = None,
    ) -> None:
        self._playwright_factory = playwright_factory
        self._supplemental_stats = supplemental_stats

    def _playwright(self) -> Any:
        if self._playwright_factory is not None:
            return self._playwright_factory()
        from playwright.sync_api import sync_playwright

        return sync_playwright()

    @staticmethod
    def _cookie() -> dict[str, Any]:
        session_id = str(os.getenv("TIKTOK_SESSION_ID") or "").strip()
        if not session_id:
            raise PlatformCredentialsError(
                "TikTok publishing credentials are not configured."
            )
        return {
            "name": "sessionid",
            "value": session_id,
            "domain": ".tiktok.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
        }

    def upload(
        self,
        video_path: str | Path,
        title: str,
        description: str = "",
        **_: Any,
    ) -> str:
        """Upload one video and require an explicit confirmed remote ID."""
        cookie = self._cookie()
        browser = None
        context = None
        submitted = False
        confirmed = False
        try:
            with self._playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1920, "height": 1080})
                context.add_cookies([cookie])
                page = context.new_page()
                page.goto("https://www.tiktok.com/upload", wait_until="networkidle")
                file_input = page.query_selector('input[type="file"]')
                if file_input is None:
                    raise PlatformConfirmationError(
                        "TikTok did not expose a video upload control."
                    )
                file_input.set_input_files(str(video_path))
                page.wait_for_selector('[data-e2e="upload-button"]', timeout=120_000)
                caption = page.query_selector('[data-e2e="caption-input"]') or page.query_selector(
                    '[contenteditable="true"]'
                )
                if caption is not None:
                    caption.fill(f"{title} {description}".strip())
                submitted = True
                page.click('[data-e2e="upload-button"]')
                page.wait_for_url("**/video/**", timeout=30_000)
                remote_id = _remote_id(page.url, "video")
                if remote_id is None:
                    raise AmbiguousPublishOutcome(
                        "TikTok did not return a video ID; reconcile before retrying."
                    )
                confirmed = True
                return remote_id
        except PlatformConfirmationError:
            raise
        except Exception as exc:
            if submitted:
                raise AmbiguousPublishOutcome(
                    "TikTok may have accepted the upload; reconcile it before retrying.",
                    technical_detail=type(exc).__name__,
                ) from None
            raise PlatformTransientError(technical_detail=type(exc).__name__) from None
        finally:
            cleanup_failed = _close_browser_resources(context, browser)
            if cleanup_failed and sys.exc_info()[0] is None and not confirmed:
                raise PlatformTransientError(
                    "TikTok browser resources could not be closed."
                )

    def get_video_stats(self, video_id: str) -> dict[str, int | float]:
        """Return a complete verified snapshot or raise a typed failure."""
        try:
            normalized = normalized_remote_id(video_id)
        except ValueError:
            raise PlatformConfirmationError(
                "TikTok statistics require a confirmed video ID."
            ) from None
        cookie = self._cookie()
        browser = None
        context = None
        try:
            with self._playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context()
                context.add_cookies([cookie])
                page = context.new_page()
                page.goto(
                    f"https://www.tiktok.com/video/{normalized}",
                    wait_until="networkidle",
                    timeout=30_000,
                )
                page.wait_for_timeout(3_000)
                values: dict[str, int | float] = {}
                for key, selector in {
                    "views": '[data-e2e="video-views"]',
                    "likes": '[data-e2e="like-count"]',
                    "comments": '[data-e2e="comment-count"]',
                    "shares": '[data-e2e="share-count"]',
                }.items():
                    element = page.query_selector(selector)
                    if element is None:
                        raise PlatformStatsError(
                            "TikTok returned an incomplete statistics snapshot."
                        )
                    values[key] = _parse_count(element.inner_text())
                values.update(
                    _verified_supplemental_stats(
                        self._supplemental_stats,
                        normalized,
                        ("revenue_usd",),
                    )
                )
                return values
        except (PlatformConfirmationError, PlatformStatsError):
            raise
        except Exception as exc:
            raise PlatformStatsError(technical_detail=type(exc).__name__) from None
        finally:
            cleanup_failed = _close_browser_resources(context, browser)
            if cleanup_failed and sys.exc_info()[0] is None:
                raise PlatformStatsError(
                    "TikTok browser resources could not be closed."
                )

    @staticmethod
    def get_video_url(video_id: str) -> str:
        return f"https://www.tiktok.com/video/{video_id}"


def _remote_id(url: object, marker: str) -> str | None:
    parts = str(url).rstrip("/").split("/")
    if marker not in parts:
        return None
    index = parts.index(marker) + 1
    if index >= len(parts):
        return None
    return parts[index].strip() or None


def _close_browser_resources(context: Any | None, browser: Any | None) -> bool:
    """Attempt both closes and report failure without masking a primary error."""
    failed = False
    for resource in (context, browser):
        if resource is None:
            continue
        try:
            resource.close()
        except Exception:
            failed = True
    return failed
