"""One-time script to obtain a YouTube OAuth2 refresh token.

Run this once locally, then paste the printed refresh token into .env as
YOUTUBE_REFRESH_TOKEN. The token is long-lived and doesn't expire unless
revoked.

Usage:
  YOUTUBE_CLIENT_ID=xxx YOUTUBE_CLIENT_SECRET=yyy python scripts/get_youtube_token.py
"""

import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

client_config = {
    "installed": {
        "client_id": os.environ["YOUTUBE_CLIENT_ID"],
        "client_secret": os.environ["YOUTUBE_CLIENT_SECRET"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob"],
    }
}

flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
creds = flow.run_console()

print("\n✅ Add this to your .env:")
print(f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}")
