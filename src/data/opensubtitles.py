"""OpenSubtitles API v1 client — search & download subtitle files.

OpenSubtitles requires:
  • An API key (free sign-up at https://www.opensubtitles.com/en/api-docs)
  • A User-Agent header with app name + version
"""

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests
import rarfile


@dataclass
class SubtitleResult:
    """Single subtitle search hit from OpenSubtitles."""

    file_id: str
    file_name: str
    movie_title: str  # cleaned
    movie_year: str | None
    language: str
    fps: float | None
    imdb_id: str | None


class OpenSubtitlesClient:
    """Thin wrapper around the OpenSubtitles REST API v1."""

    BASE_URL = "https://api.opensubtitles.com/api/v1"

    def __init__(self, api_key: str, user_agent: str, jwt: str | None = None):
        self.api_key = api_key
        self.user_agent = user_agent
        self.jwt = jwt
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Api-Key": api_key,
                "User-Agent": user_agent,
            }
        )

    # ──────────────────── Search ────────────────────

    def search(
        self,
        query: str | None = None,
        imdb_id: str | None = None,
        language: str = "en",
        limit: int = 5,
    ) -> list[SubtitleResult]:
        """Search for subtitles. One of query or imdb_id is required."""

        params: dict = {"languages": language, "limit": limit}

        if imdb_id:
            params["imdb_id"] = imdb_id.lstrip("t")  # strip leading 'tt'
        elif query:
            params["query"] = query
        else:
            raise ValueError("Provide either query or imdb_id")

        resp = self.session.get(f"{self.BASE_URL}/subtitles", params=params)
        resp.raise_for_status()
        data = resp.json()

        results: list[SubtitleResult] = []
        for item in data.get("data", []):
            attrs = item.get("attributes", {})
            files = attrs.get("files", [])
            if not files:
                continue
            results.append(
                SubtitleResult(
                    file_id=str(files[0].get("file_id", item["id"])),
                    file_name=files[0].get("file_name", "unknown.srt"),
                    movie_title=_clean_title(attrs.get("feature_details", {}).get("title", "Unknown")),
                    movie_year=str(attrs.get("feature_details", {}).get("year", "")) or None,
                    language=attrs.get("language", language),
                    fps=_safe_float(attrs.get("fps")),
                    imdb_id=f"tt{int(attrs['feature_details']['imdb_id']):07d}" if attrs.get("feature_details", {}).get("imdb_id") else None,
                )
            )
        return results

    # ──────────────────── Download ────────────────────

    def download(self, file_id: str, dest_dir: str | Path = "tmp") -> Path:
        """Request a download URL, fetch the .srt, and return its local path.

        OpenSubtitles files come zipped or rar'd, so we extract the first .srt.
        """

        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Step 1 — get download link
        headers = {"Authorization": f"Bearer {self.jwt}"} if self.jwt else {}
        resp = self.session.post(
            f"{self.BASE_URL}/download",
            json={"file_id": file_id},
            headers=headers,
        )
        resp.raise_for_status()
        dl_data = resp.json()
        url = dl_data["link"]
        file_name = dl_data.get("file_name", f"{file_id}.srt")

        # Step 2 — fetch the archive
        archive_path = dest_dir / file_name
        archive_resp = requests.get(url)
        archive_resp.raise_for_status()
        archive_path.write_bytes(archive_resp.content)

        # Step 3 — extract .srt if compressed
        lower = file_name.lower()
        srt_path = dest_dir / _to_srt_name(file_name)

        if lower.endswith(".srt"):
            return archive_path.resolve()

        try:
            # Try ZIP
            import zipfile
            with zipfile.ZipFile(archive_path) as zf:
                for member in zf.namelist():
                    if member.lower().endswith(".srt"):
                        zf.extract(member, dest_dir)
                        extracted = dest_dir / member
                        # Flatten nested dirs
                        return self._flatten_and_rename(extracted, srt_path)
        except zipfile.BadZipFile:
            pass

        try:
            # Try RAR
            with rarfile.RarFile(str(archive_path)) as rf:
                for member in rf.namelist():
                    if member.lower().endswith(".srt"):
                        rf.extract(member, dest_dir)
                        extracted = dest_dir / member
                        return self._flatten_and_rename(extracted, srt_path)
        except rarfile.BadRarFile:
            pass

        # Fallback: assume it's already an SRT
        if srt_path.exists():
            return srt_path.resolve()

        return archive_path.resolve()

    # ──────────────────── Helpers ────────────────────

    def _flatten_and_rename(self, extracted: Path, target: Path) -> Path:
        """Move/symlink a deeply-nested extracted .srt to the target filename."""
        target.parent.mkdir(parents=True, exist_ok=True)
        if str(extracted.name).lower() != str(target.name).lower():
            extracted.rename(target)
        return target.resolve()


class SubtitleCache:
    """Simple file-system cache so we never re-download the same movie."""

    def __init__(self, cache_dir: str | Path = "results"):
        self._dir = Path(cache_dir) / "subtitles"
        self._dir.mkdir(parents=True, exist_ok=True)

    def key(self, imdb_id: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]", "_", str(imdb_id))

    def has(self, imdb_id: str) -> Path | None:
        path = self._dir / f"{self.key(imdb_id)}.srt"
        return path if path.exists() else None

    def store(self, imdb_id: str, srt_path: Path) -> Path:
        dest = self._dir / f"{self.key(imdb_id)}.srt"
        if not dest.exists():
            dest.write_bytes(srt_path.read_bytes())
        return dest.resolve()


# ──────────────────── Pure helpers ────────────────────

def _clean_title(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.strip())


def _safe_float(val: str | None) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _to_srt_name(file_name: str) -> str:
    base, _ = os.path.splitext(file_name)
    return base + ".srt"
