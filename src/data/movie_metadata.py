"""Bounded movie metadata lookup with explicit optional/transient outcomes."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

import requests
from PIL import Image, UnidentifiedImageError

from api.errors import AttentionRequired, TransientFailure
from api.settings import canonical_imdb_id


@dataclass(frozen=True)
class MovieMetadataResult:
    """Verified provider data, or an explicit optional-provider absence."""

    configured: bool
    metadata: dict[str, Any] = field(default_factory=dict)
    poster_bytes: bytes | None = None
    warnings: tuple[str, ...] = ()


class MovieMetadataClient:
    """Fetch TMDB details and optional OMDb awards using injected HTTP state."""

    TMDB_API = "https://api.themoviedb.org/3"
    TMDB_IMAGES = "https://image.tmdb.org/t/p/w780"
    OMDB_API = "https://www.omdbapi.com/"
    TRANSIENT_STATUS = {408, 425, 429}

    def __init__(
        self,
        *,
        tmdb_token: str | None,
        omdb_api_key: str | None = None,
        session: Any | None = None,
        timeout: tuple[float, float] = (3.05, 15.0),
    ) -> None:
        self.tmdb_token = (tmdb_token or "").strip() or None
        self.omdb_api_key = (omdb_api_key or "").strip() or None
        self.session = session or requests.Session()
        self.timeout = timeout

    def fetch(self, imdb_id: str) -> MovieMetadataResult:
        imdb_id = canonical_imdb_id(imdb_id)
        if self.tmdb_token is None:
            return MovieMetadataResult(
                configured=False,
                warnings=(
                    "Movie metadata provider is not configured; continuing without enrichment.",
                ),
            )
        headers = {
            "Authorization": f"Bearer {self.tmdb_token}",
            "Accept": "application/json",
        }
        found = self._json(
            self._get(
                f"{self.TMDB_API}/find/{imdb_id}",
                params={"external_source": "imdb_id"},
                headers=headers,
            ),
            "TMDB lookup",
        )
        results = found.get("movie_results")
        if not isinstance(results, list):
            raise AttentionRequired(
                "Movie metadata response was invalid.",
                code="invalid_metadata_response",
                actions=("retry",),
            )
        if not results:
            return MovieMetadataResult(
                configured=True,
                warnings=("No movie metadata matched the supplied IMDb ID.",),
            )
        movie = results[0]
        movie_id = movie.get("id") if isinstance(movie, dict) else None
        if not isinstance(movie_id, int | str) or not str(movie_id):
            raise AttentionRequired(
                "Movie metadata response was invalid.",
                code="invalid_metadata_response",
                actions=("retry",),
            )
        details = self._json(
            self._get(f"{self.TMDB_API}/movie/{movie_id}", headers=headers),
            "TMDB details",
        )
        credits = self._json(
            self._get(f"{self.TMDB_API}/movie/{movie_id}/credits", headers=headers),
            "TMDB credits",
        )
        title = details.get("title") or movie.get("title")
        if not isinstance(title, str) or not title.strip():
            raise AttentionRequired(
                "Movie metadata response was invalid.",
                code="invalid_metadata_response",
                actions=("retry",),
            )
        crew = credits.get("crew") if isinstance(credits.get("crew"), list) else []
        cast = credits.get("cast") if isinstance(credits.get("cast"), list) else []
        director = next(
            (
                person.get("name", "")
                for person in crew
                if isinstance(person, dict) and person.get("job") == "Director"
            ),
            "",
        )
        actors = [
            person.get("name", "")
            for person in cast[:3]
            if isinstance(person, dict) and person.get("name")
        ]
        runtime = details.get("runtime")
        metadata: dict[str, Any] = {
            "Title": title.strip(),
            "Year": str(details.get("release_date") or movie.get("release_date") or "")[
                :4
            ],
            "Director": director,
            "Actors": ", ".join(actors),
            "imdbRating": _rating(details.get("vote_average")),
            "Runtime": f"{int(runtime)} min"
            if isinstance(runtime, int | float) and runtime > 0
            else "",
            "Awards": "",
            "tmdb_id": str(movie_id),
        }
        warnings: list[str] = []
        if self.omdb_api_key is not None:
            try:
                omdb = self._json(
                    self._get(
                        self.OMDB_API,
                        params={"i": imdb_id, "apikey": self.omdb_api_key},
                    ),
                    "OMDb lookup",
                )
            except TransientFailure:
                raise
            response_flag = str(omdb.get("Response", "")).strip().lower()
            if response_flag == "false":
                error = str(omdb.get("Error", "")).strip()
                if "not found" in error.lower():
                    warnings.append("No OMDb awards metadata matched the movie.")
                else:
                    raise AttentionRequired(
                        "OMDb rejected the metadata request.",
                        code="metadata_request_rejected",
                        actions=("check_metadata_configuration", "retry"),
                    )
            awards = omdb.get("Awards")
            if (
                response_flag != "false"
                and isinstance(awards, str)
                and awards != "N/A"
            ):
                metadata["Awards"] = awards

        poster_bytes = None
        poster_path = details.get("poster_path") or movie.get("poster_path")
        if isinstance(poster_path, str) and poster_path.startswith("/"):
            poster_response = self._get(f"{self.TMDB_IMAGES}{poster_path}")
            content = bytes(poster_response.content)
            if not content:
                warnings.append("Movie poster response was empty.")
            else:
                try:
                    with Image.open(io.BytesIO(content)) as image:
                        image.verify()
                except (OSError, ValueError, UnidentifiedImageError):
                    warnings.append("Movie poster response was not a valid image.")
                else:
                    poster_bytes = content
        return MovieMetadataResult(
            configured=True,
            metadata=metadata,
            poster_bytes=poster_bytes,
            warnings=tuple(warnings),
        )

    def _get(self, url: str, **kwargs: Any):
        try:
            response = self.session.get(url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise TransientFailure(
                "Movie metadata service is temporarily unavailable.",
                code="metadata_service_transient",
            ) from exc
        status = int(getattr(response, "status_code", 200))
        if status in self.TRANSIENT_STATUS or status >= 500:
            raise TransientFailure(
                "Movie metadata service is temporarily unavailable.",
                code="metadata_service_transient",
            )
        if status >= 400:
            raise AttentionRequired(
                "Movie metadata service rejected the request.",
                code="metadata_request_rejected",
                actions=("check_metadata_configuration", "retry"),
            )
        return response

    @staticmethod
    def _json(response: Any, operation: str) -> dict[str, Any]:
        try:
            value = response.json()
        except (TypeError, ValueError) as exc:
            raise AttentionRequired(
                f"{operation} returned an invalid response.",
                code="invalid_metadata_response",
                actions=("retry",),
            ) from exc
        if not isinstance(value, dict):
            raise AttentionRequired(
                f"{operation} returned an invalid response.",
                code="invalid_metadata_response",
                actions=("retry",),
            )
        return value


def _rating(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return str(round(number, 1)) if number > 0 else ""
