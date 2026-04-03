"""TikTok uploader via headless Playwright browser.

The TikTok web API doesn't have an official upload endpoint for
personal accounts, so we automate browser interaction.

Requires:
  • A stored cookie session from a prior manual login
  • playwright installed and browsers installed (playwright install)

Usage:
  from src.publishing.tiktok import upload_to_tiktok
  upload_to_tiktok("output/final.mp4", "My video title", "#shorts")
"""

from pathlib import Path

from playwright.sync_api import sync_playwright


def upload_to_tiktok(
    video_path: str | Path,
    title: str,
    description: str = "",
    cookies_path: str = "tiktok_cookies.json",
):
    """Upload a video to TikTok using a headless browser session."""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080}
        )

        # Load stored cookies if available
        cookies_file = Path(cookies_path)
        if cookies_file.exists():
            import json
            with open(cookies_file) as f:
                cookies = json.load(f)
            context.add_cookies(cookies)

        page = context.new_context().new_page()

        # Navigate to upload page
        page.goto("https://www.tiktok.com/upload",
                  wait_until="networkidle")

        # Upload file via hidden input
        file_input = page.query_selector('input[type="file"]')
        if file_input:
            file_input.set_input_files(str(video_path))
        else:
            raise RuntimeError("Upload input not found on page")

        # Wait for upload to complete (video preview appears)
        page.wait_for_selector('[data-e2e="upload-button"]',
                               timeout=120_000)

        # Fill caption
        caption_input = page.query_selector(
            '[data-e2e="caption-input"]'
        ) or page.query_selector(
            '[contenteditable="true"]'
        )
        if caption_input:
            caption_input.fill(f"{title} {description}")

        # Post
        page.click('[data-e2e="upload-button"]')
        print("⬆️ Submitted to TikTok!")

        # Save cookies for next time
        cookies = context.cookies()
        import json
        with open(cookies_file, "w") as f:
            json.dump(cookies, f, indent=2)

        browser.close()
