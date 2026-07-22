from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from google.auth.exceptions import TransportError

from api.database import OperationStore
from api.errors import AmbiguousPublishOutcome
from api.publishing import PublishingService
from src.publishing.errors import (
    PlatformConfirmationError,
    PlatformCredentialsError,
    PlatformStatsError,
    PlatformTransientError,
)
from src.publishing.instagram import InstagramClient
from src.publishing.tiktok import TikTokClient
from src.publishing.youtube import YouTubeClient


class FakeYouTubeRequest:
    def __init__(self, result):
        self.result = result

    def next_chunk(self):
        if isinstance(self.result, BaseException):
            raise self.result
        return None, self.result

    def execute(self):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeYouTubeVideos:
    def __init__(self, *, upload_result=None, stats_result=None, insert_error=None):
        self.upload_result = upload_result
        self.stats_result = stats_result
        self.insert_error = insert_error
        self.insert_calls = []

    def insert(self, **kwargs):
        self.insert_calls.append(kwargs)
        if self.insert_error is not None:
            raise self.insert_error
        return FakeYouTubeRequest(self.upload_result)

    def list(self, **kwargs):
        return FakeYouTubeRequest(self.stats_result)


class FakeYouTubeService:
    def __init__(self, videos):
        self._videos = videos

    def videos(self):
        return self._videos


def test_youtube_missing_credentials_raise_typed_error_before_sdk_import(monkeypatch):
    for name in (
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(PlatformCredentialsError):
        _ = YouTubeClient().youtube


def test_youtube_missing_credentials_remain_one_attempt_through_service(
    tmp_path, monkeypatch
):
    for name in (
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    client = YouTubeClient(media_upload_factory=lambda *args, **kwargs: object())
    store = OperationStore(
        tmp_path / "youtube-credentials.db",
        clock=lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
    )
    store.initialize()
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    video = tmp_path / "youtube-credentials.mp4"
    video.write_bytes(b"video")
    service = PublishingService(store, {"youtube": client}, sleep=lambda _: None)
    service.request(job["id"], "youtube", title="Pulp Fiction", summary={})

    with pytest.raises(PlatformCredentialsError):
        service.publish(job["id"], "youtube", video)

    detail = store.get_job_detail(job["id"])
    assert len(detail["publishing_attempts"]) == 1
    assert detail["publishing_attempts"][0]["safe_error"]["code"] == (
        "publishing_credentials_required"
    )


def test_youtube_stats_preserve_typed_credentials_error_through_service(
    tmp_path, monkeypatch
):
    for name in (
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    client = YouTubeClient()
    store = OperationStore(
        tmp_path / "youtube-stats-credentials.db",
        clock=lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
    )
    store.initialize()
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.request_publication(job["id"], "youtube", metadata={"title": "Stable"})
    store.upsert_release(
        job["id"],
        "youtube",
        status="uploaded",
        remote_id="remote-1",
        metadata={"title": "Stable"},
    )
    service = PublishingService(store, {"youtube": client})

    with pytest.raises(PlatformCredentialsError):
        service.refresh_stats(job["id"], "youtube")


def test_youtube_supplemental_credentials_error_remains_typed_through_service(
    tmp_path,
):
    videos = FakeYouTubeVideos(
        stats_result={
            "items": [
                {
                    "statistics": {
                        "viewCount": "10",
                        "likeCount": "2",
                        "commentCount": "1",
                    }
                }
            ]
        }
    )

    def missing_supplemental_credentials(_remote_id):
        raise PlatformCredentialsError()

    client = YouTubeClient(
        youtube=FakeYouTubeService(videos),
        supplemental_stats=missing_supplemental_credentials,
    )
    store = OperationStore(tmp_path / "youtube-supplemental-credentials.db")
    store.initialize()
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    store.upsert_release(
        job["id"], "youtube", status="uploaded", remote_id="remote-1"
    )
    service = PublishingService(store, {"youtube": client})

    with pytest.raises(PlatformCredentialsError):
        service.refresh_stats(job["id"], "youtube")

    assert store.list_revenue(job["id"]) == []


def test_youtube_authentication_timeout_is_typed_transient(monkeypatch):
    for name in (
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
    ):
        monkeypatch.setenv(name, "configured")

    def timeout_builder():
        raise TimeoutError("raw authentication response")

    with pytest.raises(PlatformTransientError):
        _ = YouTubeClient(youtube_builder=timeout_builder).youtube


def test_youtube_google_transport_error_is_typed_transient(monkeypatch):
    for name in (
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
    ):
        monkeypatch.setenv(name, "configured")

    def transport_failure():
        raise TransportError("raw transport response")

    with pytest.raises(PlatformTransientError):
        _ = YouTubeClient(youtube_builder=transport_failure).youtube


def test_youtube_defaults_private_and_requires_nonempty_confirmation(tmp_path):
    videos = FakeYouTubeVideos(upload_result={})
    client = YouTubeClient(
        youtube=FakeYouTubeService(videos),
        media_upload_factory=lambda *args, **kwargs: (args, kwargs),
    )
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")

    with pytest.raises(AmbiguousPublishOutcome):
        client.upload(video, title="Title", description="Description")

    assert videos.insert_calls[0]["body"]["status"]["privacyStatus"] == "private"


def test_youtube_post_request_timeout_is_ambiguous_and_not_empty_success(tmp_path):
    videos = FakeYouTubeVideos(upload_result=TimeoutError("Bearer raw-token"))
    client = YouTubeClient(
        youtube=FakeYouTubeService(videos),
        media_upload_factory=lambda *args, **kwargs: object(),
    )
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")

    with pytest.raises(AmbiguousPublishOutcome):
        client.upload(video, title="Title", description="Description")


def test_youtube_generic_pre_submit_failure_is_typed_transient(tmp_path):
    videos = FakeYouTubeVideos(insert_error=RuntimeError("raw upstream body"))
    client = YouTubeClient(
        youtube=FakeYouTubeService(videos),
        media_upload_factory=lambda *args, **kwargs: object(),
    )

    with pytest.raises(PlatformTransientError):
        client.upload(tmp_path / "video.mp4", title="Title", description="Description")


def test_youtube_media_construction_failure_is_typed_transient(tmp_path):
    def fail_media(*args, **kwargs):
        raise RuntimeError("absolute /home/user/private raw body")

    client = YouTubeClient(
        youtube=FakeYouTubeService(FakeYouTubeVideos()),
        media_upload_factory=fail_media,
    )

    with pytest.raises(PlatformTransientError):
        client.upload(tmp_path / "video.mp4", title="Title", description="Description")


def test_youtube_generic_post_submit_failure_is_ambiguous(tmp_path):
    videos = FakeYouTubeVideos(upload_result=RuntimeError("raw upstream body"))
    client = YouTubeClient(
        youtube=FakeYouTubeService(videos),
        media_upload_factory=lambda *args, **kwargs: object(),
    )

    with pytest.raises(AmbiguousPublishOutcome):
        client.upload(tmp_path / "video.mp4", title="Title", description="Description")


def test_youtube_malformed_upload_response_is_typed_confirmation_failure(tmp_path):
    client = YouTubeClient(
        youtube=FakeYouTubeService(FakeYouTubeVideos(upload_result=[])),
        media_upload_factory=lambda *args, **kwargs: object(),
    )

    with pytest.raises(AmbiguousPublishOutcome):
        client.upload(tmp_path / "video.mp4", title="Title", description="Description")


@pytest.mark.parametrize(
    "malformed_id",
    [{"nested": "id"}, object(), "remote\nid", "remote\x00id"],
)
def test_youtube_malformed_remote_id_is_ambiguous_not_stringified(
    tmp_path, malformed_id
):
    client = YouTubeClient(
        youtube=FakeYouTubeService(
            FakeYouTubeVideos(upload_result={"id": malformed_id})
        ),
        media_upload_factory=lambda *args, **kwargs: object(),
    )

    with pytest.raises(AmbiguousPublishOutcome):
        client.upload(tmp_path / "video.mp4", title="Title", description="Description")


def test_youtube_missing_stats_item_raises_instead_of_returning_zero_snapshot():
    videos = FakeYouTubeVideos(stats_result={"items": []})
    client = YouTubeClient(
        youtube=FakeYouTubeService(videos),
        supplemental_stats=lambda remote_id: {"shares": 0, "revenue_usd": 0.0},
    )

    with pytest.raises(PlatformStatsError):
        client.get_video_stats("remote-1")


@pytest.mark.parametrize("missing_field", ["likeCount", "commentCount"])
def test_youtube_partial_stats_raise_instead_of_synthesizing_zero(missing_field):
    statistics = {"viewCount": "10", "likeCount": "2", "commentCount": "1"}
    statistics.pop(missing_field)
    videos = FakeYouTubeVideos(
        stats_result={"items": [{"statistics": statistics}]}
    )
    client = YouTubeClient(
        youtube=FakeYouTubeService(videos),
        supplemental_stats=lambda remote_id: {"shares": 0, "revenue_usd": 0.0},
    )

    with pytest.raises(PlatformStatsError):
        client.get_video_stats("remote-1")


def test_youtube_generic_stats_failure_is_typed():
    videos = FakeYouTubeVideos(stats_result=RuntimeError("raw upstream body"))
    client = YouTubeClient(
        youtube=FakeYouTubeService(videos),
        supplemental_stats=lambda remote_id: {"shares": 0, "revenue_usd": 0.0},
    )

    with pytest.raises(PlatformStatsError):
        client.get_video_stats("remote-1")


def test_youtube_stats_require_remote_id_and_return_complete_snapshot():
    videos = FakeYouTubeVideos(
        stats_result={
            "items": [
                {
                    "statistics": {
                        "viewCount": "10",
                        "likeCount": "2",
                        "commentCount": "1",
                    }
                }
            ]
        }
    )
    client = YouTubeClient(
        youtube=FakeYouTubeService(videos),
        supplemental_stats=lambda remote_id: {"shares": 0, "revenue_usd": 0.0},
    )

    with pytest.raises(PlatformConfirmationError):
        client.get_video_stats("")
    assert client.get_video_stats("remote-1") == {
        "views": 10,
        "likes": 2,
        "comments": 1,
        "revenue_usd": 0.0,
        "shares": 0,
    }


def test_youtube_stats_reject_unavailable_supplemental_dimensions():
    videos = FakeYouTubeVideos(
        stats_result={
            "items": [
                {
                    "statistics": {
                        "viewCount": "10",
                        "likeCount": "2",
                        "commentCount": "1",
                    }
                }
            ]
        }
    )
    client = YouTubeClient(youtube=FakeYouTubeService(videos))

    with pytest.raises(PlatformStatsError):
        client.get_video_stats("remote-1")


class FakeElement:
    def __init__(self, text="0", *, click_error=False):
        self.text = text
        self.filled = []
        self.files = []
        self.clicked = False
        self.click_calls = 0
        self.click_error = click_error

    def set_input_files(self, value):
        self.files.append(value)

    def fill(self, value):
        self.filled.append(value)

    def click(self):
        self.clicked = True
        self.click_calls += 1
        if self.click_error:
            raise RuntimeError("click dispatched before browser error")

    def inner_text(self):
        return self.text


class FakePage:
    def __init__(
        self,
        *,
        platform,
        missing_file=False,
        url_timeout=False,
        stats_timeout=False,
        upload_timeout=False,
        upload_error=False,
        confirmation_error=False,
        new_post_fallback=False,
        missing_remote_id=False,
        irreversible_click_error=False,
        stats_text=None,
    ):
        self.platform = platform
        self.missing_file = missing_file
        self.url_timeout = url_timeout
        self.stats_timeout = stats_timeout
        self.upload_timeout = upload_timeout
        self.upload_error = upload_error
        self.confirmation_error = confirmation_error
        self.new_post_fallback = new_post_fallback
        self.missing_remote_id = missing_remote_id
        self.irreversible_click_error = irreversible_click_error
        self.submit_clicks = 0
        self.file_queries = 0
        self.stats_text = stats_text or {}
        self.url = "https://platform.invalid/upload"
        self.handlers = {}
        self.file = FakeElement()
        self.caption = FakeElement()
        self.share = FakeElement(click_error=irreversible_click_error)

    def goto(self, url, **kwargs):
        if self.stats_timeout and ("/video/" in url or "/p/" in url):
            raise TimeoutError("upstream body Cookie: sessionid=secret")
        if self.upload_timeout and ("upload" in url or "reels/create" in url):
            raise TimeoutError("pre-submit timeout")
        if self.upload_error and ("upload" in url or "reels/create" in url):
            raise RuntimeError("raw upstream body")
        self.url = url

    def query_selector(self, selector):
        if selector == 'input[type="file"]':
            self.file_queries += 1
            if self.new_post_fallback:
                return None if self.file_queries == 1 else self.file
            return None if self.missing_file else self.file
        if selector == '[aria-label="New post"]':
            return FakeElement()
        if "caption" in selector or "contenteditable" in selector:
            return self.caption
        if selector == '[aria-label="Share"]':
            return self.share
        if selector in self.stats_text:
            return FakeElement(self.stats_text[selector])
        return None

    def wait_for_selector(self, selector, **kwargs):
        return None

    def click(self, selector):
        if selector == '[data-e2e="upload-button"]':
            self.submit_clicks += 1
            if self.irreversible_click_error:
                raise RuntimeError("click dispatched before browser error")
        return None

    def fill(self, selector, value):
        self.caption.fill(value)

    def wait_for_url(self, pattern, **kwargs):
        if self.url_timeout:
            raise TimeoutError("confirmation timed out")
        if self.confirmation_error:
            raise RuntimeError("raw upstream body")
        if self.missing_remote_id:
            self.url = "https://platform.invalid/confirmed-without-id"
            return
        suffix = "video/remote-1" if self.platform == "tiktok" else "p/remote-1"
        self.url = f"https://platform.invalid/{suffix}"

    def wait_for_timeout(self, milliseconds):
        return None

    def on(self, name, callback):
        self.handlers[name] = callback

    def get_by_role(self, role, name):
        return self.share


class FakeBrowserContext:
    def __init__(self, page, *, close_error=False):
        self.page = page
        self.closed = False
        self.cookies = []
        self.close_error = close_error

    def add_cookies(self, cookies):
        self.cookies.extend(cookies)

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True
        if self.close_error:
            raise RuntimeError("context cleanup failed")


class FakeBrowser:
    def __init__(self, context):
        self.context = context
        self.closed = False

    def new_context(self, **kwargs):
        return self.context

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser):
        self.browser = browser

    def launch(self, **kwargs):
        return self.browser


class FakePlaywright:
    def __init__(self, browser):
        self.chromium = FakeChromium(browser)


class FakePlaywrightManager:
    def __init__(self, playwright):
        self.playwright = playwright

    def __enter__(self):
        return self.playwright

    def __exit__(self, exc_type, exc, traceback):
        return False


def browser_client(client_class, platform, *, client_kwargs=None, close_error=False, **page_kwargs):
    page = FakePage(platform=platform, **page_kwargs)
    context = FakeBrowserContext(page, close_error=close_error)
    browser = FakeBrowser(context)
    manager = FakePlaywrightManager(FakePlaywright(browser))
    client = client_class(playwright_factory=lambda: manager, **(client_kwargs or {}))
    return client, page, context, browser


@pytest.mark.parametrize(
    ("client_class", "platform", "env_name"),
    [
        (TikTokClient, "tiktok", "TIKTOK_SESSION_ID"),
        (InstagramClient, "instagram", "INSTAGRAM_SESSION_ID"),
    ],
)
def test_browser_clients_raise_typed_credentials_before_launch(
    monkeypatch, client_class, platform, env_name
):
    monkeypatch.delenv(env_name, raising=False)
    launched = False

    def launch_forbidden():
        nonlocal launched
        launched = True
        raise AssertionError("browser must not launch without credentials")

    client = client_class(playwright_factory=launch_forbidden)

    with pytest.raises(PlatformCredentialsError):
        client.upload(Path("unused.mp4"), title="Title")
    assert launched is False


@pytest.mark.parametrize(
    ("client_class", "platform", "env_name"),
    [
        (TikTokClient, "tiktok", "TIKTOK_SESSION_ID"),
        (InstagramClient, "instagram", "INSTAGRAM_SESSION_ID"),
    ],
)
def test_browser_upload_confirmation_failures_are_typed_and_resources_close(
    monkeypatch, client_class, platform, env_name
):
    monkeypatch.setenv(env_name, "safe-session")
    client, _, context, browser = browser_client(
        client_class, platform, missing_file=True
    )

    with pytest.raises(PlatformConfirmationError):
        client.upload(Path("video.mp4"), title="Title")

    assert context.closed is True
    assert browser.closed is True


@pytest.mark.parametrize(
    ("client_class", "platform", "env_name"),
    [
        (TikTokClient, "tiktok", "TIKTOK_SESSION_ID"),
        (InstagramClient, "instagram", "INSTAGRAM_SESSION_ID"),
    ],
)
def test_browser_primary_failure_survives_context_cleanup_failure_and_browser_closes(
    monkeypatch, client_class, platform, env_name
):
    monkeypatch.setenv(env_name, "safe-session")
    client, _, context, browser = browser_client(
        client_class, platform, missing_file=True, close_error=True
    )

    with pytest.raises(PlatformConfirmationError):
        client.upload(Path("video.mp4"), title="Title")

    assert context.closed is True
    assert browser.closed is True


@pytest.mark.parametrize(
    ("client_class", "platform", "env_name"),
    [
        (TikTokClient, "tiktok", "TIKTOK_SESSION_ID"),
        (InstagramClient, "instagram", "INSTAGRAM_SESSION_ID"),
    ],
)
def test_browser_post_submit_timeout_is_ambiguous_and_resources_close(
    monkeypatch, client_class, platform, env_name
):
    monkeypatch.setenv(env_name, "safe-session")
    client, _, context, browser = browser_client(
        client_class, platform, url_timeout=True
    )

    with pytest.raises(AmbiguousPublishOutcome):
        client.upload(Path("video.mp4"), title="Title")

    assert context.closed is True
    assert browser.closed is True


@pytest.mark.parametrize(
    ("client_class", "platform", "env_name"),
    [
        (TikTokClient, "tiktok", "TIKTOK_SESSION_ID"),
        (InstagramClient, "instagram", "INSTAGRAM_SESSION_ID"),
    ],
)
def test_click_that_dispatches_then_raises_is_ambiguous_once_through_service(
    tmp_path, monkeypatch, client_class, platform, env_name
):
    monkeypatch.setenv(env_name, "safe-session")
    client, page, _, _ = browser_client(
        client_class, platform, irreversible_click_error=True
    )
    store = OperationStore(
        tmp_path / f"{platform}-click.db",
        clock=lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
    )
    store.initialize()
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    video = tmp_path / f"{platform}-click.mp4"
    video.write_bytes(b"video")
    service = PublishingService(store, {platform: client}, sleep=lambda _: None)
    service.request(job["id"], platform, title="Pulp Fiction", summary={})

    with pytest.raises(AmbiguousPublishOutcome):
        service.publish(job["id"], platform, video)

    clicks = page.submit_clicks if platform == "tiktok" else page.share.click_calls
    assert clicks == 1
    detail = store.get_job_detail(job["id"])
    assert len(detail["publishing_attempts"]) == 1
    assert detail["releases"][0]["status"] == "needs_attention"


@pytest.mark.parametrize(
    ("client_class", "platform", "env_name"),
    [
        (TikTokClient, "tiktok", "TIKTOK_SESSION_ID"),
        (InstagramClient, "instagram", "INSTAGRAM_SESSION_ID"),
    ],
)
def test_browser_post_submit_missing_remote_id_is_ambiguous(
    monkeypatch, client_class, platform, env_name
):
    monkeypatch.setenv(env_name, "safe-session")
    client, _, context, browser = browser_client(
        client_class, platform, missing_remote_id=True
    )

    with pytest.raises(AmbiguousPublishOutcome):
        client.upload(Path("video.mp4"), title="Title")

    assert context.closed is True
    assert browser.closed is True


@pytest.mark.parametrize(
    ("client_class", "platform", "env_name"),
    [
        (TikTokClient, "tiktok", "TIKTOK_SESSION_ID"),
        (InstagramClient, "instagram", "INSTAGRAM_SESSION_ID"),
    ],
)
def test_real_browser_client_missing_id_is_reconcilable_through_service(
    tmp_path, monkeypatch, client_class, platform, env_name
):
    monkeypatch.setenv(env_name, "safe-session")
    client, _, _, _ = browser_client(
        client_class, platform, missing_remote_id=True
    )
    store = OperationStore(
        tmp_path / f"{platform}.db",
        clock=lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
    )
    store.initialize()
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    video = tmp_path / f"{platform}.mp4"
    video.write_bytes(b"video")
    service = PublishingService(store, {platform: client}, sleep=lambda _: None)
    service.request(job["id"], platform, title="Pulp Fiction", summary={})

    with pytest.raises(AmbiguousPublishOutcome):
        service.publish(job["id"], platform, video)

    release = store.get_job_detail(job["id"])["releases"][0]
    assert release["status"] == "needs_attention"
    assert release["safe_error"]["code"] == "ambiguous_publish_outcome"


def test_real_youtube_missing_id_is_reconcilable_through_service(tmp_path):
    client = YouTubeClient(
        youtube=FakeYouTubeService(FakeYouTubeVideos(upload_result={})),
        media_upload_factory=lambda *args, **kwargs: object(),
    )
    store = OperationStore(
        tmp_path / "youtube.db",
        clock=lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
    )
    store.initialize()
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    video = tmp_path / "youtube.mp4"
    video.write_bytes(b"video")
    service = PublishingService(store, {"youtube": client}, sleep=lambda _: None)
    service.request(job["id"], "youtube", title="Pulp Fiction", summary={})

    with pytest.raises(AmbiguousPublishOutcome):
        service.publish(job["id"], "youtube", video)

    release = store.get_job_detail(job["id"])["releases"][0]
    assert release["status"] == "needs_attention"
    assert release["safe_error"]["code"] == "ambiguous_publish_outcome"


@pytest.mark.parametrize(
    ("client_class", "platform", "env_name"),
    [
        (TikTokClient, "tiktok", "TIKTOK_SESSION_ID"),
        (InstagramClient, "instagram", "INSTAGRAM_SESSION_ID"),
    ],
)
def test_browser_stats_failures_are_typed_and_resources_close(
    monkeypatch, client_class, platform, env_name
):
    monkeypatch.setenv(env_name, "safe-session")
    client, _, context, browser = browser_client(
        client_class, platform, stats_timeout=True
    )

    with pytest.raises(PlatformConfirmationError):
        client.get_video_stats("")
    with pytest.raises(PlatformStatsError):
        client.get_video_stats("remote-1")

    assert context.closed is True
    assert browser.closed is True


@pytest.mark.parametrize(
    ("client_class", "platform", "env_name"),
    [
        (TikTokClient, "tiktok", "TIKTOK_SESSION_ID"),
        (InstagramClient, "instagram", "INSTAGRAM_SESSION_ID"),
    ],
)
def test_browser_pre_submit_timeout_is_typed_transient_and_resources_close(
    monkeypatch, client_class, platform, env_name
):
    monkeypatch.setenv(env_name, "safe-session")
    client, _, context, browser = browser_client(
        client_class, platform, upload_timeout=True
    )

    with pytest.raises(PlatformTransientError):
        client.upload(Path("video.mp4"), title="Title")

    assert context.closed is True
    assert browser.closed is True


@pytest.mark.parametrize(
    ("client_class", "platform", "env_name", "page_kwargs", "error_type"),
    [
        (
            TikTokClient,
            "tiktok",
            "TIKTOK_SESSION_ID",
            {"upload_error": True},
            PlatformTransientError,
        ),
        (
            InstagramClient,
            "instagram",
            "INSTAGRAM_SESSION_ID",
            {"upload_error": True},
            PlatformTransientError,
        ),
        (
            TikTokClient,
            "tiktok",
            "TIKTOK_SESSION_ID",
            {"confirmation_error": True},
            AmbiguousPublishOutcome,
        ),
        (
            InstagramClient,
            "instagram",
            "INSTAGRAM_SESSION_ID",
            {"confirmation_error": True},
            AmbiguousPublishOutcome,
        ),
    ],
)
def test_browser_generic_failures_are_typed_by_submit_boundary(
    monkeypatch, client_class, platform, env_name, page_kwargs, error_type
):
    monkeypatch.setenv(env_name, "safe-session")
    client, _, context, browser = browser_client(client_class, platform, **page_kwargs)

    with pytest.raises(error_type):
        client.upload(Path("video.mp4"), title="Title")

    assert context.closed is True
    assert browser.closed is True


@pytest.mark.parametrize(
    ("client_class", "platform", "env_name"),
    [
        (TikTokClient, "tiktok", "TIKTOK_SESSION_ID"),
        (InstagramClient, "instagram", "INSTAGRAM_SESSION_ID"),
    ],
)
def test_browser_upload_returns_confirmed_remote_id_and_resources_close(
    monkeypatch, client_class, platform, env_name
):
    monkeypatch.setenv(env_name, "safe-session")
    client, _, context, browser = browser_client(client_class, platform)

    assert client.upload(Path("video.mp4"), title="Title") == "remote-1"
    assert context.closed is True
    assert browser.closed is True


@pytest.mark.parametrize(
    ("client_class", "platform", "env_name"),
    [
        (TikTokClient, "tiktok", "TIKTOK_SESSION_ID"),
        (InstagramClient, "instagram", "INSTAGRAM_SESSION_ID"),
    ],
)
def test_confirmed_upload_survives_cleanup_failure_without_retryable_error(
    monkeypatch, client_class, platform, env_name
):
    monkeypatch.setenv(env_name, "safe-session")
    client, _, context, browser = browser_client(
        client_class, platform, close_error=True
    )

    assert client.upload(Path("video.mp4"), title="Title") == "remote-1"
    assert context.closed is True
    assert browser.closed is True


def test_instagram_restores_new_post_fallback_before_confirmation_failure(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_SESSION_ID", "safe-session")
    client, page, context, browser = browser_client(
        InstagramClient, "instagram", new_post_fallback=True
    )

    assert client.upload(Path("video.mp4"), title="Title") == "remote-1"
    assert page.file_queries == 2
    assert context.closed is True
    assert browser.closed is True


def test_tiktok_stats_return_complete_parsed_snapshot_and_resources_close(monkeypatch):
    monkeypatch.setenv("TIKTOK_SESSION_ID", "safe-session")
    client, _, context, browser = browser_client(
        TikTokClient,
        "tiktok",
        client_kwargs={"supplemental_stats": lambda remote_id: {"revenue_usd": 0.0}},
        stats_text={
            '[data-e2e="video-views"]': "1.2K",
            '[data-e2e="like-count"]': "20",
            '[data-e2e="comment-count"]': "3",
            '[data-e2e="share-count"]': "4",
        },
    )

    assert client.get_video_stats("remote-1") == {
        "views": 1200,
        "likes": 20,
        "comments": 3,
        "shares": 4,
        "revenue_usd": 0.0,
    }
    assert context.closed is True
    assert browser.closed is True


def test_instagram_stats_return_complete_parsed_snapshot_and_resources_close(
    monkeypatch,
):
    monkeypatch.setenv("INSTAGRAM_SESSION_ID", "safe-session")
    client, _, context, browser = browser_client(
        InstagramClient,
        "instagram",
        client_kwargs={
            "supplemental_stats": lambda remote_id: {
                "shares": 0,
                "revenue_usd": 0.0,
            }
        },
        stats_text={
            'section[class*="like"]': "2K likes",
            'a[href*="/comments/"]': "30 comments",
            '[aria-label*="views"]': "4.5K views",
        },
    )

    assert client.get_video_stats("remote-1") == {
        "views": 4500,
        "likes": 2000,
        "comments": 30,
        "shares": 0,
        "revenue_usd": 0.0,
    }
    assert context.closed is True
    assert browser.closed is True


@pytest.mark.parametrize(
    ("client_class", "platform", "env_name", "stats_text"),
    [
        (
            TikTokClient,
            "tiktok",
            "TIKTOK_SESSION_ID",
            {
                '[data-e2e="video-views"]': "1",
                '[data-e2e="like-count"]': "1",
                '[data-e2e="comment-count"]': "1",
                '[data-e2e="share-count"]': "1",
            },
        ),
        (
            InstagramClient,
            "instagram",
            "INSTAGRAM_SESSION_ID",
            {
                'section[class*="like"]': "1 like",
                'a[href*="/comments/"]': "1 comment",
                '[aria-label*="views"]': "1 view",
            },
        ),
    ],
)
def test_browser_stats_reject_unavailable_supplemental_dimensions(
    monkeypatch, client_class, platform, env_name, stats_text
):
    monkeypatch.setenv(env_name, "safe-session")
    client, _, context, browser = browser_client(
        client_class, platform, stats_text=stats_text
    )

    with pytest.raises(PlatformStatsError):
        client.get_video_stats("remote-1")

    assert context.closed is True
    assert browser.closed is True
