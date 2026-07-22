from pathlib import Path

import pytest

from api.database import OperationStore
from api.errors import AttentionRequired
from api.settings import Settings
from api.subtitles import SubtitleService
from src.data.opensubtitles import SubtitleCache, SubtitleResult

VALID_SRT = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n\n2\n01:20:00,000 --> 01:25:00,000\nBye\n"
SHORT_SRT = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"


class FakeClient:
    def __init__(self, results, payloads):
        self.results = results
        self.payloads = payloads
        self.downloads = []

    def search(self, **kwargs):
        return self.results

    def download(self, file_id, destination):
        self.downloads.append(file_id)
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.payloads[file_id])
        return path


@pytest.fixture
def store(tmp_path):
    result = OperationStore(tmp_path / "operations.db")
    result.initialize()
    return result


@pytest.fixture
def configured(tmp_path):
    return Settings(base_dir=tmp_path, results_dir=tmp_path / "results")


def make_service(store, configured, results, payloads):
    return SubtitleService(
        store, FakeClient(results, payloads), SubtitleCache(configured.results_dir), configured
    )


def make_job(store):
    job, _ = store.create_or_get_active_job("tt0110912", "pulp fiction", "Pulp Fiction")
    return job


def test_discovery_ranks_and_persists_candidates(store, configured):
    results = [
        SubtitleResult("1", "one.srt", "Pulp Fiction", "1994", "en", None, "tt0110912"),
        SubtitleResult("2", "two.srt", "Other", "1990", "fr", None, "tt0000002"),
    ]
    job = make_job(store)
    candidates = make_service(store, configured, results, {}).discover(job["id"])
    assert [candidate["rank"] for candidate in candidates] == [1, 2]
    assert candidates[0]["rank_reasons"] == ["exact_imdb_match", "language_match", "title_match"]


def test_select_stops_after_exactly_three_automatic_candidates_and_records_rejections(store, configured):
    results = [SubtitleResult(str(i), f"{i}.srt", "Pulp Fiction", "1994", "en", None, "tt0110912") for i in range(4)]
    job = make_job(store)
    service = make_service(store, configured, results, {str(i): SHORT_SRT for i in range(4)})
    service.discover(job["id"])
    with pytest.raises(AttentionRequired) as raised:
        service.select(job["id"])
    assert raised.value.actions == ("select_subtitle", "rediscover_subtitles", "upload_subtitle", "cancel")
    detail = store.get_job_detail(job["id"])
    assert len([attempt for attempt in detail["attempts"] if attempt["candidate_id"]]) == 3
    assert all(candidate["rejection_reasons"] for candidate in detail["candidates"][:3])
    assert detail["run"]["state"] == "needs_attention"


def test_manual_selection_records_threshold_override_and_is_idempotent(store, configured):
    result = SubtitleResult("1", "one.srt", "Pulp Fiction", "1994", "en", None, "tt0110912", runtime_seconds=100 * 60)
    job = make_job(store)
    service = make_service(store, configured, [result], {"1": SHORT_SRT})
    candidate = service.discover(job["id"])[0]
    first = service.select(job["id"], manual_candidate_id=candidate["id"])
    second = service.select(job["id"], manual_candidate_id=candidate["id"])
    assert first["selection_method"] == "manual"
    assert second["id"] == first["id"]
    assert "manual_threshold_override" in first["quality_reasons"]
    assert len(store.list_decisions(job["id"])) == 1


def test_selected_cache_replaces_stale_content_only_after_validation(store, configured):
    result = SubtitleResult("1", "one.srt", "Pulp Fiction", "1994", "en", None, "tt0110912", runtime_seconds=100 * 60)
    job = make_job(store)
    cache = SubtitleCache(configured.results_dir)
    stale = configured.results_dir / "stale.srt"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(SHORT_SRT)
    cache.store("tt0110912", stale)
    service = make_service(store, configured, [result], {"1": VALID_SRT})
    service.discover(job["id"])
    selected = service.select(job["id"])
    assert cache.has("tt0110912").read_bytes() == VALID_SRT
    assert selected["content_hash"]


def test_upload_is_confined_and_resume_does_not_repeat_selection(store, configured):
    job = make_job(store)
    service = make_service(store, configured, [], {})
    uploaded = service.upload(job["id"], "../../outside.srt", VALID_SRT)
    assert uploaded["artifact_available"] is True
    selected = service.select(job["id"], manual_candidate_id=uploaded["id"])
    resumed = service.select(job["id"])
    assert resumed["id"] == selected["id"]
    assert not (configured.results_dir.parent / "outside.srt").exists()
