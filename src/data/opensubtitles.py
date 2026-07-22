"""Bounded OpenSubtitles search and safe subtitle download helpers."""

from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import rarfile
import requests


class UnsafeArchiveError(ValueError):
    """Raised when remote subtitle content cannot be safely used."""


@dataclass(frozen=True)
class SubtitleResult:
    """Provider metadata for a possible subtitle; filenames are never paths."""

    file_id: str
    file_name: str
    movie_title: str
    movie_year: str | None
    language: str
    fps: float | None
    imdb_id: str | None
    provider_rating: float | None = None
    download_count: int | None = None
    runtime_seconds: float | None = None


class OpenSubtitlesClient:
    """Small API client with bounded network and extraction behavior."""

    BASE_URL = "https://api.opensubtitles.com/api/v1"
    DEFAULT_TIMEOUT = (3.05, 15.0)
    MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

    def __init__(
        self,
        api_key: str,
        user_agent: str,
        jwt: str | None = None,
        username: str | None = None,
        password: str | None = None,
        *,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        max_download_bytes: int = MAX_DOWNLOAD_BYTES,
    ):
        self.api_key = api_key
        self.user_agent = user_agent
        self.jwt = jwt
        self.username = username
        self.password = password
        self.timeout = timeout
        self.max_download_bytes = int(max_download_bytes)
        self.session = requests.Session()
        self.session.headers.update({"Api-Key": api_key, "User-Agent": user_agent})

    def login(self) -> str:
        if not self.username or not self.password:
            raise ValueError("Username and password are required for login")
        response = self.session.post(
            f"{self.BASE_URL}/login",
            json={"username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        response.raise_for_status()
        self.jwt = response.json()["token"]
        return self.jwt

    def search(
        self,
        query: str | None = None,
        imdb_id: str | None = None,
        language: str = "en",
        limit: int = 5,
    ) -> list[SubtitleResult]:
        """Search provider metadata using a bounded request."""
        params: dict[str, object] = {"languages": language, "limit": limit}
        if imdb_id:
            params["imdb_id"] = imdb_id.lstrip("t")
        elif query:
            params["query"] = query
        else:
            raise ValueError("Provide either query or imdb_id")
        response = self.session.get(
            f"{self.BASE_URL}/subtitles", params=params, timeout=self.timeout
        )
        response.raise_for_status()
        results: list[SubtitleResult] = []
        for item in response.json().get("data", []):
            attrs = item.get("attributes", {})
            files = attrs.get("files", [])
            if not files:
                continue
            details = attrs.get("feature_details", {})
            file = files[0]
            results.append(
                SubtitleResult(
                    file_id=str(file.get("file_id", item["id"])),
                    file_name=str(file.get("file_name", "unknown.srt")),
                    movie_title=_clean_title(details.get("title", "Unknown")),
                    movie_year=str(details.get("year", "")) or None,
                    language=str(attrs.get("language", language)),
                    fps=_safe_float(attrs.get("fps")),
                    imdb_id=safe_imdb_id(details.get("imdb_id")),
                    provider_rating=_safe_float(attrs.get("ratings") or attrs.get("rating")),
                    download_count=_safe_int(attrs.get("download_count")),
                    runtime_seconds=_safe_duration(details.get("duration")),
                )
            )
        return results

    def download(
        self,
        file_id: str,
        destination: str | Path | None = None,
        *,
        dest_dir: str | Path | None = None,
    ) -> Path:
        """Write the first safe SRT to a caller-generated destination path."""
        if destination is None:
            if dest_dir is None:
                raise ValueError("A generated subtitle destination is required")
            destination = Path(dest_dir) / f"{file_id}.srt"
        if dest_dir is not None:
            raise ValueError("Use destination rather than both destination and dest_dir")
        target = Path(destination).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        response = self._download_link(file_id)
        response.raise_for_status()
        data = response.json()
        remote = requests.get(data["link"], stream=True, timeout=self.timeout)
        remote.raise_for_status()
        content = self._read_bounded(remote)
        name = str(data.get("file_name", "")).lower()
        if name.endswith(".srt"):
            target.write_bytes(content)
            return target
        subtitle = _archive_subtitle(content)
        target.write_bytes(subtitle)
        return target

    def _download_link(self, file_id: str):
        def request_link(headers: dict[str, str]):
            return self.session.post(
                f"{self.BASE_URL}/download",
                json={"file_id": file_id},
                headers=headers,
                timeout=self.timeout,
            )

        response = request_link({"Authorization": f"Bearer {self.jwt}"} if self.jwt else {})
        if response.status_code != 401:
            return response
        if self.username and self.password:
            try:
                self.login()
                response = request_link({"Authorization": f"Bearer {self.jwt}"})
            except requests.RequestException:
                pass
        return request_link({}) if response.status_code == 401 else response

    def _read_bounded(self, response) -> bytes:
        length = response.headers.get("content-length")
        if length and int(length) > self.max_download_bytes:
            raise UnsafeArchiveError("Subtitle download exceeds the size limit")
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > self.max_download_bytes:
                raise UnsafeArchiveError("Subtitle download exceeds the size limit")
            chunks.append(chunk)
        return b"".join(chunks)


class SubtitleCache:
    """Small content cache with explicit promotion after validation."""

    def __init__(self, cache_dir: str | Path = "results"):
        self._dir = Path(cache_dir) / "subtitles"
        self._dir.mkdir(parents=True, exist_ok=True)

    def key(self, imdb_id: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]", "_", str(imdb_id))

    def has(self, imdb_id: str) -> Path | None:
        path = self._dir / f"{self.key(imdb_id)}.srt"
        return path if path.exists() else None

    def store(self, imdb_id: str, srt_path: Path, *, replace: bool = False) -> Path:
        dest = self._dir / f"{self.key(imdb_id)}.srt"
        if replace or not dest.exists():
            partial = dest.with_suffix(".partial")
            partial.write_bytes(srt_path.read_bytes())
            partial.replace(dest)
        return dest.resolve()


def _archive_subtitle(content: bytes) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return _read_safe_member(archive, archive.infolist())
    except zipfile.BadZipFile:
        pass
    try:
        with rarfile.RarFile(io.BytesIO(content)) as archive:
            return _read_safe_member(archive, archive.infolist())
    except (rarfile.BadRarFile, rarfile.NeedFirstVolume):
        pass
    raise UnsafeArchiveError("Subtitle archive has no safe SRT member")


def _read_safe_member(archive, members) -> bytes:
    for member in members:
        name = member.filename
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            raise UnsafeArchiveError("Subtitle archive member escapes its destination")
        if not name.lower().endswith(".srt"):
            continue
        if getattr(member, "file_size", 0) > OpenSubtitlesClient.MAX_DOWNLOAD_BYTES:
            raise UnsafeArchiveError("Subtitle archive member exceeds the size limit")
        with archive.open(member) as stream:
            data = stream.read(OpenSubtitlesClient.MAX_DOWNLOAD_BYTES + 1)
        if len(data) > OpenSubtitlesClient.MAX_DOWNLOAD_BYTES:
            raise UnsafeArchiveError("Subtitle archive member exceeds the size limit")
        return data
    raise UnsafeArchiveError("Subtitle archive has no SRT member")


def _clean_title(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.strip())


def _safe_float(val: object) -> float | None:
    try:
        return None if val is None else float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val: object) -> int | None:
    try:
        return None if val is None else int(val)
    except (ValueError, TypeError):
        return None


def _safe_duration(value: object) -> float | None:
    number = _safe_float(value)
    return number if number is not None and number > 0 else None


def safe_imdb_id(val: object) -> str | None:
    if val is None or val == "":
        return None
    text = str(val).strip()
    if not text:
        return None
    if text.lower().startswith("tt"):
        return text
    try:
        return f"tt{int(text):07d}"
    except (ValueError, TypeError):
        return text


def _to_srt_name(file_name: str) -> str:
    base, _ = os.path.splitext(file_name)
    return base + ".srt"
