"""YouTube Data API v3 client for Shorts — upload + analytics.

Required env vars:
  YOUTUBE_CLIENT_ID      — OAuth2 client ID from Google Cloud Console
  YOUTUBE_CLIENT_SECRET  — OAuth2 client secret
  YOUTUBE_REFRESH_TOKEN  — Long-lived refresh token (obtain once via OAuth flow)

Usage:
  from src.publishing.youtube import YouTubeClient
  yt = YouTubeClient()
  video_id = yt.upload("output/final.mp4", title="...", description="...")
  stats = yt.get_video_stats(video_id)
"""

import os
from pathlib import Path


class YouTubeClient:
    """Wraps YouTube Data API v3 for uploading Shorts and fetching stats."""

    SCOPES = [
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
    ]
    TOKEN_URI = "https://oauth2.googleapis.com/token"

    def __init__(self):
        self._youtube = None

    def _build(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        client_id = os.environ["YOUTUBE_CLIENT_ID"]
        client_secret = os.environ["YOUTUBE_CLIENT_SECRET"]
        refresh_token = os.environ["YOUTUBE_REFRESH_TOKEN"]

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=self.TOKEN_URI,
            client_id=client_id,
            client_secret=client_secret,
            scopes=self.SCOPES,
        )
        creds.refresh(Request())
        self._youtube = build("youtube", "v3", credentials=creds)

    @property
    def youtube(self):
        if self._youtube is None:
            self._build()
        return self._youtube

    def upload(
        self,
        video_path: str | Path,
        title: str,
        description: str,
        tags: list[str] | None = None,
        category_id: str = "24",  # Entertainment
        privacy_status: str = "public",
        **_,
    ) -> str:
        """Upload a 9:16 video as a YouTube Short. Returns the video ID."""
        from googleapiclient.http import MediaFileUpload

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

        media = MediaFileUpload(
            str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4"
        )

        request = self.youtube.videos().insert(
            part=",".join(body.keys()), body=body, media_body=media
        )

        response = None
        while response is None:
            _, response = request.next_chunk()

        video_id = response["id"]
        print(f"✅ YouTube upload complete: {self.get_video_url(video_id)}")
        return video_id

    def get_video_stats(self, video_id: str) -> dict:
        """Return views, likes, comments for a video.

        revenue_usd is always 0.0 — requires YouTube Partner Program + Analytics API.
        shares are not exposed via the public Data API.
        """
        result = (
            self.youtube.videos()
            .list(part="statistics", id=video_id)
            .execute()
        )
        items = result.get("items", [])
        if not items:
            return {"views": 0, "likes": 0, "comments": 0, "revenue_usd": 0.0, "shares": 0}

        stats = items[0]["statistics"]
        return {
            "views": int(stats.get("viewCount", 0)),
            "likes": int(stats.get("likeCount", 0)),
            "comments": int(stats.get("commentCount", 0)),
            "revenue_usd": 0.0,
            "shares": 0,
        }

    def get_video_url(self, video_id: str) -> str:
        return f"https://www.youtube.com/shorts/{video_id}"
