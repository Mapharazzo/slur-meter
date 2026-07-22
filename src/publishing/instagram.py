"""Instagram Reels publishing client with explicit typed failures."""

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
from src.publishing.tiktok import (
    _close_browser_resources,
    _parse_count,
    _remote_id,
)
from src.publishing.youtube import _verified_supplemental_stats

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class InstagramClient:
    """Playwright-backed Instagram Reels uploader and statistics reader."""

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
        session_id = str(os.getenv("INSTAGRAM_SESSION_ID") or "").strip()
        if not session_id:
            raise PlatformCredentialsError(
                "Instagram publishing credentials are not configured."
            )
        return {
            "name": "sessionid",
            "value": session_id,
            "domain": ".instagram.com",
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
        """Upload one Reel and require an explicit confirmed shortcode."""
        cookie = self._cookie()
        browser = None
        context = None
        submitted = False
        confirmed = False
        try:
            with self._playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    viewport={"width": 1280, "height": 900}, user_agent=_USER_AGENT
                )
                context.add_cookies([cookie])
                page = context.new_page()
                page.goto(
                    "https://www.instagram.com/reels/create/",
                    wait_until="networkidle",
                )
                file_input = page.query_selector('input[type="file"]')
                if file_input is None:
                    trigger = page.query_selector('[aria-label="New post"]')
                    if trigger is not None:
                        trigger.click()
                        page.wait_for_selector('input[type="file"]', timeout=10_000)
                        file_input = page.query_selector('input[type="file"]')
                if file_input is None:
                    raise PlatformConfirmationError(
                        "Instagram did not expose a video upload control."
                    )
                file_input.set_input_files(str(video_path))
                page.wait_for_selector('[aria-label="Next"]', timeout=120_000)
                page.click('[aria-label="Next"]')
                caption_selector = 'textarea[aria-label="Write a caption..."]'
                page.wait_for_selector(caption_selector, timeout=30_000)
                caption = f"{title}\n\n{description}".strip()
                page.fill(caption_selector, caption)
                share = page.query_selector('[aria-label="Share"]') or page.get_by_role(
                    "button", name="Share"
                )
                if share is None:
                    raise PlatformConfirmationError(
                        "Instagram did not expose a Share control."
                    )
                submitted = True
                share.click()
                page.wait_for_url("**/p/**", timeout=30_000)
                remote_id = _remote_id(page.url, "p")
                if remote_id is None:
                    raise AmbiguousPublishOutcome(
                        "Instagram did not return a post ID; reconcile before retrying."
                    )
                confirmed = True
                return remote_id
        except PlatformConfirmationError:
            raise
        except Exception as exc:
            if submitted:
                raise AmbiguousPublishOutcome(
                    "Instagram may have accepted the upload; reconcile it before retrying.",
                    technical_detail=type(exc).__name__,
                ) from None
            raise PlatformTransientError(technical_detail=type(exc).__name__) from None
        finally:
            cleanup_failed = _close_browser_resources(context, browser)
            if cleanup_failed and sys.exc_info()[0] is None and not confirmed:
                raise PlatformTransientError(
                    "Instagram browser resources could not be closed."
                )

    def get_video_stats(self, post_id: str) -> dict[str, int | float]:
        """Return a complete verified snapshot or raise a typed failure."""
        try:
            normalized = normalized_remote_id(post_id)
        except ValueError:
            raise PlatformConfirmationError(
                "Instagram statistics require a confirmed post ID."
            ) from None
        cookie = self._cookie()
        browser = None
        context = None
        try:
            with self._playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(user_agent=_USER_AGENT)
                context.add_cookies([cookie])
                page = context.new_page()
                page.goto(
                    f"https://www.instagram.com/p/{normalized}/",
                    wait_until="networkidle",
                    timeout=30_000,
                )
                page.wait_for_timeout(3_000)
                values: dict[str, int | float] = {}
                for key, selectors in {
                    "likes": ('section[class*="like"]', '[aria-label*="like"]'),
                    "comments": ('a[href*="/comments/"]',),
                    "views": ('[aria-label*="views"]', 'span[class*="view"]'),
                }.items():
                    element = None
                    for selector in selectors:
                        element = page.query_selector(selector)
                        if element is not None:
                            break
                    if element is None:
                        raise PlatformStatsError(
                            "Instagram returned an incomplete statistics snapshot."
                        )
                    words = element.inner_text().split()
                    if not words:
                        raise PlatformStatsError(
                            "Instagram returned an invalid statistics snapshot."
                        )
                    values[key] = _parse_count(words[0])
                values.update(
                    _verified_supplemental_stats(
                        self._supplemental_stats,
                        normalized,
                        ("shares", "revenue_usd"),
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
                    "Instagram browser resources could not be closed."
                )

    @staticmethod
    def get_video_url(post_id: str) -> str:
        return f"https://www.instagram.com/p/{post_id}/"
