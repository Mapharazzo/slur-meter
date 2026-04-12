"""Instagram Reels client via headless Playwright — upload + stats scraping.

Required env var:
  INSTAGRAM_SESSION_ID  — value of the `sessionid` cookie from a logged-in browser session

Usage:
  from src.publishing.instagram import InstagramClient
  ig = InstagramClient()
  post_id = ig.upload("output/final.mp4", title="My Reel", description="#shorts")
  stats = ig.get_video_stats(post_id)
"""

import os


def _parse_count(text: str) -> int:
    text = text.strip().upper().replace(",", "")
    try:
        if text.endswith("K"):
            return int(float(text[:-1]) * 1_000)
        if text.endswith("M"):
            return int(float(text[:-1]) * 1_000_000)
        return int(text)
    except (ValueError, IndexError):
        return 0


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _session_cookie() -> dict:
    return {
        "name": "sessionid",
        "value": os.environ["INSTAGRAM_SESSION_ID"],
        "domain": ".instagram.com",
        "path": "/",
        "httpOnly": True,
        "secure": True,
    }


class InstagramClient:
    """Playwright-based Instagram Reels uploader and stats scraper."""

    def upload(
        self,
        video_path,
        title: str,
        description: str = "",
        **_,
    ) -> str:
        """Upload a Reel to Instagram. Returns the post shortcode, or empty string on failure."""
        from playwright.sync_api import sync_playwright

        post_id = ""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=_USER_AGENT,
            )
            context.add_cookies([_session_cookie()])
            page = context.new_page()

            page.goto("https://www.instagram.com/reels/create/", wait_until="networkidle")

            file_input = page.query_selector('input[type="file"]')
            if not file_input:
                trigger = page.query_selector('[aria-label="New post"]')
                if trigger:
                    trigger.click()
                    page.wait_for_selector('input[type="file"]', timeout=10_000)
                    file_input = page.query_selector('input[type="file"]')

            if not file_input:
                raise RuntimeError("Upload input not found on Instagram page")

            file_input.set_input_files(str(video_path))

            page.wait_for_selector('[aria-label="Next"]', timeout=120_000)
            page.click('[aria-label="Next"]')

            page.wait_for_selector(
                'textarea[aria-label="Write a caption..."]', timeout=30_000
            )
            caption = f"{title}\n\n{description}".strip() if description else title
            page.fill('textarea[aria-label="Write a caption..."]', caption)

            share_btn = page.query_selector(
                '[aria-label="Share"]'
            ) or page.get_by_role("button", name="Share")
            if share_btn:
                share_btn.click()
            print("⬆️ Submitted to Instagram!")

            try:
                page.wait_for_url("**/p/**", timeout=30_000)
                parts = page.url.rstrip("/").split("/")
                if "p" in parts:
                    post_id = parts[parts.index("p") + 1]
            except Exception:
                pass

            browser.close()

        return post_id

    def get_video_stats(self, post_id: str) -> dict:
        """Scrape stats from an Instagram post page.

        Intercepts the GraphQL response where available, falls back to DOM scraping.
        revenue_usd is always 0.0 — no public monetisation API.
        """
        from playwright.sync_api import sync_playwright

        base = {"views": 0, "likes": 0, "comments": 0, "revenue_usd": 0.0, "shares": 0}
        if not post_id:
            return base

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=_USER_AGENT)
            context.add_cookies([_session_cookie()])
            page = context.new_page()

            try:
                api_stats: dict = {}

                def handle_response(response):
                    if "graphql/query" in response.url or "/api/v1/media/" in response.url:
                        try:
                            data = response.json()
                            media = data.get("data", {}).get("shortcode_media", {})
                            if media:
                                api_stats["likes"] = media.get("edge_media_preview_like", {}).get("count", 0)
                                api_stats["comments"] = media.get("edge_media_to_comment", {}).get("count", 0)
                                api_stats["views"] = media.get("video_view_count", 0)
                        except Exception:
                            pass

                page.on("response", handle_response)
                page.goto(
                    f"https://www.instagram.com/p/{post_id}/",
                    wait_until="networkidle",
                    timeout=30_000,
                )
                page.wait_for_timeout(3_000)

                if api_stats:
                    base.update(api_stats)
                else:
                    like_el = page.query_selector('section[class*="like"]') or \
                              page.query_selector('[aria-label*="like"]')
                    if like_el:
                        text = like_el.inner_text().split()
                        if text:
                            base["likes"] = _parse_count(text[0])

                    comment_link = page.query_selector('a[href*="/comments/"]')
                    if comment_link:
                        text = comment_link.inner_text().split()
                        if text:
                            base["comments"] = _parse_count(text[0])

                    view_el = (
                        page.query_selector('[aria-label*="views"]')
                        or page.query_selector('span[class*="view"]')
                    )
                    if view_el:
                        text = view_el.inner_text().split()
                        if text:
                            base["views"] = _parse_count(text[0])

            except Exception:
                pass
            finally:
                browser.close()

        return base

    def get_video_url(self, post_id: str) -> str:
        return f"https://www.instagram.com/p/{post_id}/"
