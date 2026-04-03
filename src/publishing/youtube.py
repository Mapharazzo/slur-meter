"""YouTube Data API v3 uploader for Shorts.

Requires:
  1. Google Cloud project with YouTube Data API v3 enabled
  2. OAuth2 credentials (client_secret.json) or ADC
  3. First-run browser auth to authorise the app

Usage:
  from src.publishing.youtube import upload_short
  upload_short("output/final.mp4", metadata={...})
"""

import os
from pathlib import Path


def upload_short(
    video_path: str | Path,
    title: str,
    description: str,
    tags: list[str] | None = None,
    category_id: str = "24",  # Entertainment
    privacy_status: str = "public",
):
    """Upload a 9:16 video to YouTube as a Short.

    YouTube auto-classifies ≤60s vertical videos as Shorts.
    """

    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from google_auth_oauthlib.flow import InstalledAppFlow

    # Scopes needed for YouTube uploads
    scopes = ["https://www.googleapis.com/auth/youtube.upload"]

    # Try to use ADC first, fall back to OAuth flow
    flow = InstalledAppFlow.from_client_secrets_file(
        "client_secret.json", scopes
    )
    credentials = flow.run_console()

    youtube = build("youtube", "v3", credentials=credentials)

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
        str(video_path),
        chunksize=-1,
        resumable=True,
        mimetype="video/mp4",
    )

    request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=media,
    )

    print("⬆️ Uploading to YouTube…")
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"   Uploaded {int(status.progress() * 100)}%")

    video_id = response.get("id")
    print(f"✅ Upload complete! https://www.youtube.com/watch?v={video_id}")
    return video_id
