"""TikTok client via headless Playwright — upload + stats scraping.

Required env var:
  TIKTOK_SESSION_ID  — value of the `sessionid` cookie from a logged-in browser session

Usage:
  from src.publishing.tiktok import TikTokClient
  tt = TikTokClient()
  video_id = tt.upload("output/final.mp4", title="My video", description="#shorts")
  stats = tt.get_video_stats(video_id)
"""

import os


def _parse_count(text: str) -> int:
    """Parse TikTok-style counts like '1.2K', '3.5M' into an integer."""
    text = text.strip().upper().replace(",", "")
    try:
        if text.endswith("K"):
            return int(float(text[:-1]) * 1_000)
        if text.endswith("M"):
            return int(float(text[:-1]) * 1_000_000)
        return int(text)
    except (ValueError, IndexError):
        return 0


def _session_cookie() -> dict:
    return {
        "name": "sessionid",
        "value": os.environ["TIKTOK_SESSION_ID"],
        "domain": ".tiktok.com",
        "path": "/",
        "httpOnly": True,
        "secure": True,
    }


class TikTokClient:
    """Playwright-based TikTok uploader and stats scraper."""

    def upload(
        self,
        video_path,
        title: str,
        description: str = "",
        **_,
    ) -> str:
        """Upload video to TikTok. Returns the video ID, or empty string on failure."""
        from pathlib import Path
        from playwright.sync_api import sync_playwright

        video_id = ""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1920, "height": 1080})
            context.add_cookies([_session_cookie()])
            page = context.new_page()

            page.goto("https://www.tiktok.com/upload", wait_until="networkidle")

            file_input = page.query_selector('input[type="file"]')
            if not file_input:
                raise RuntimeError("Upload input not found on TikTok page")
            file_input.set_input_files(str(video_path))

            page.wait_for_selector('[data-e2e="upload-button"]', timeout=120_000)

            caption_input = (
                page.query_selector('[data-e2e="caption-input"]')
                or page.query_selector('[contenteditable="true"]')
            )
            if caption_input:
                caption_input.fill(f"{title} {description}".strip())

            page.click('[data-e2e="upload-button"]')
            print("⬆️ Submitted to TikTok!")

            try:
                page.wait_for_url("**/video/**", timeout=30_000)
                parts = page.url.rstrip("/").split("/")
                if "video" in parts:
                    video_id = parts[parts.index("video") + 1]
            except Exception:
                pass

            browser.close()

        return video_id

    def get_video_stats(self, video_id: str) -> dict:
        """Scrape stats from a TikTok video page.

        Intercepts the internal API response where available, falls back to DOM scraping.
        revenue_usd is always 0.0 — no public monetisation API.
        """
        from playwright.sync_api import sync_playwright

        base = {"views": 0, "likes": 0, "comments": 0, "revenue_usd": 0.0, "shares": 0}
        if not video_id:
            return base

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            context.add_cookies([_session_cookie()])
            page = context.new_page()

            try:
                api_stats: dict = {}

                def handle_response(response):
                    if "api/item/detail" in response.url or "aweme/detail" in response.url:
                        try:
                            data = response.json()
                            item = data.get("item_info", data.get("aweme_detail", {}))
                            stats = item.get("stats", {})
                            api_stats.update({
                                "views": stats.get("play_count", 0),
                                "likes": stats.get("digg_count", 0),
                                "comments": stats.get("comment_count", 0),
                                "shares": stats.get("share_count", 0),
                            })
                        except Exception:
                            pass

                page.on("response", handle_response)
                page.goto(
                    f"https://www.tiktok.com/video/{video_id}",
                    wait_until="networkidle",
                    timeout=30_000,
                )
                page.wait_for_timeout(3_000)

                if api_stats:
                    base.update(api_stats)
                else:
                    for key, sel in {
                        "views":    '[data-e2e="video-views"]',
                        "likes":    '[data-e2e="like-count"]',
                        "comments": '[data-e2e="comment-count"]',
                        "shares":   '[data-e2e="share-count"]',
                    }.items():
                        el = page.query_selector(sel)
                        if el:
                            base[key] = _parse_count(el.inner_text())

            except Exception:
                pass
            finally:
                browser.close()

        return base

    def get_video_url(self, video_id: str) -> str:
        return f"https://www.tiktok.com/video/{video_id}"
