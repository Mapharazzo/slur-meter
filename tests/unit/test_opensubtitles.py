"""Unit tests — data fetching module."""

import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

from src.data.opensubtitles import (
    OpenSubtitlesClient, SubtitleCache, SubtitleResult,
    _clean_title, _safe_float, _to_srt_name,
)


class TestHelpers:
    def test_clean_title_strips_whitespace(self):
        assert _clean_title("  Pulp   Fiction  ") == "Pulp Fiction"

    def test_clean_title_normal(self):
        assert _clean_title("Django Unchained") == "Django Unchained"

    def test_safe_float_valid(self):
        assert _safe_float("23.976") == 23.976

    def test_safe_float_none(self):
        assert _safe_float(None) is None

    def test_safe_float_invalid(self):
        assert _safe_float("abc") is None

    def test_to_srt_name(self):
        assert _to_srt_name("movie.zip") == "movie.srt"
        assert _to_srt_name("sub.rar") == "sub.srt"
        assert _to_srt_name("already.srt") == "already.srt"


class TestSubtitleResult:
    def test_fields(self):
        r = SubtitleResult(
            file_id="12345", file_name="test.srt",
            movie_title="Test", movie_year="2020",
            language="en", fps=23.976, imdb_id="tt1234567",
        )
        assert r.file_id == "12345"


class TestOpenSubtitlesClient:
    @pytest.fixture
    def client(self):
        return OpenSubtitlesClient(
            api_key="test-key", user_agent="TestBot v1.0"
        )

    @patch("requests.Session.get")
    def test_search_by_imdb(self, mock_get, client):
        mock_get.return_value.json.return_value = {
            "data": [{
                "id": 1001,
                "attributes": {
                    "feature_details": {
                        "title": "Django Unchained",
                        "year": 2012,
                        "imdb_id": "1853728",
                    },
                    "files": [{"file_name": "django.srt"}],
                    "language": "en",
                    "fps": "23.976",
                }
            }]
        }
        mock_get.return_value.raise_for_status = lambda: None
        results = client.search(imdb_id="1853728")
        assert len(results) == 1
        assert results[0].movie_title == "Django Unchained"
        assert results[0].file_id == "1001"

    @patch("requests.Session.get")
    def test_search_by_query(self, mock_get, client):
        mock_get.return_value.json.return_value = {
            "data": [{
                "id": 2001,
                "attributes": {
                    "feature_details": {
                        "title": "Pulp Fiction",
                        "year": 1994,
                        "imdb_id": "0110912",
                    },
                    "files": [{"file_name": "pf.srt"}],
                    "language": "en",
                }
            }]
        }
        mock_get.return_value.raise_for_status = lambda: None
        results = client.search(query="Pulp Fiction")
        assert results[0].movie_title == "Pulp Fiction"

    def test_search_no_params_raises(self, client):
        with pytest.raises(ValueError, match="Provide either"):
            client.search()

    @patch("requests.Session.get")
    def test_search_empty(self, mock_get, client):
        mock_get.return_value.json.return_value = {"data": []}
        mock_get.return_value.raise_for_status = lambda: None
        results = client.search(query="Nonexistent")
        assert results == []


class TestSubtitleCache:
    def test_has_and_store(self, tmp_path):
        cache = SubtitleCache(tmp_path)
        srt = tmp_path / "temp.srt"
        srt.write_text("test")
        stored = cache.store("tt0110912", srt)
        assert cache.has("tt0110912") == stored

    def test_has_missing(self, tmp_path):
        cache = SubtitleCache(tmp_path)
        assert cache.has("tt9999999") is None

    def test_key_sanitization(self, tmp_path):
        cache = SubtitleCache(tmp_path)
        assert cache.key("tt-0110_912") == "tt_0110_912"
