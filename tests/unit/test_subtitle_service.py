from hashlib import sha256
from pathlib import Path

import pytest

from api.database import OperationStore
from api.errors import AttentionRequired
from api.settings import Settings
from api.subtitles import SubtitleService
from src.analysis.engine import ProfanityEngine
from src.data.opensubtitles import SubtitleCache, SubtitleResult

VALID_SRT = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n\n2\n01:20:00,000 --> 01:25:00,000\nBye\n"
SHORT_SRT = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"
CP1252_SRT = (
    "1\n00:00:01,000 --> 00:00:02,000\ncaf\u00e9\n\n"
    "2\n01:20:00,000 --> 01:25:00,000\nBye\n"
).encode("cp1252")


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


def test_exhaustion_message_reports_actual_attempted_candidate_count(store, configured):
    result = SubtitleResult("1", "one.srt", "Pulp Fiction", "1994", "en", None, "tt0110912")
    job = make_job(store)
    service = make_service(store, configured, [result], {"1": SHORT_SRT})
    service.discover(job["id"])

    with pytest.raises(AttentionRequired, match="One subtitle candidate was rejected"):
        service.select(job["id"])


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


def test_cp1252_candidate_promotes_normalized_utf8_for_cache_and_analysis(store, configured):
    result = SubtitleResult("1", "one.srt", "Pulp Fiction", "1994", "en", None, "tt0110912", runtime_seconds=100 * 60)
    job = make_job(store)
    service = make_service(store, configured, [result], {"1": CP1252_SRT})
    service.discover(job["id"])

    selected = service.select(job["id"])

    artifact = Path(store.get_candidate(selected["id"], include_internal=True)["artifact_path"])
    assert artifact.read_bytes() == service.cache.has("tt0110912").read_bytes()
    assert b"caf\xc3\xa9" in artifact.read_bytes()
    assert selected["content_hash"] == sha256(artifact.read_bytes()).hexdigest()
    assert ProfanityEngine({"categories": {"soft": ["caf\u00e9"]}}).analyse_srt(artifact)["summary"]["total_soft"] == 1


def test_upload_hash_describes_the_normalized_utf8_artifact(store, configured):
    job = make_job(store)
    uploaded = make_service(store, configured, [], {}).upload(job["id"], "subtitle.srt", CP1252_SRT)
    artifact = Path(store.get_candidate(uploaded["id"], include_internal=True)["artifact_path"])

    assert uploaded["content_hash"] == sha256(artifact.read_bytes()).hexdigest()
    assert b"caf\xc3\xa9" in artifact.read_bytes()


def test_resume_finishes_interrupted_promotion_before_exposing_selected_candidate(store, configured):
    result = SubtitleResult("1", "one.srt", "Pulp Fiction", "1994", "en", None, "tt0110912", runtime_seconds=100 * 60)
    job = make_job(store)
    service = make_service(store, configured, [result], {"1": VALID_SRT})
    service.discover(job["id"])
    original_store = service.cache.store
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("promotion interrupted")
        return original_store(*args, **kwargs)

    service.cache.store = fail_once
    with pytest.raises(RuntimeError, match="interrupted"):
        service.select(job["id"])

    candidate = store.list_candidates(job["id"])[0]
    assert candidate["status"] == "validated"
    assert store.get_job_detail(job["id"])["stages"][0]["state"] == "running"

    selected = service.select(job["id"])
    detail = store.get_job_detail(job["id"])
    assert selected["status"] == "selected"
    assert detail["stages"][0]["state"] == "completed"
    assert len([event for event in detail["events"] if event["type"] == "subtitle_selected"]) == 1
    assert len(store.list_decisions(job["id"])) == 0
    assert len([attempt for attempt in detail["attempts"] if attempt["candidate_id"]]) == 1


def test_upload_is_confined_and_resume_does_not_repeat_selection(store, configured):
    job = make_job(store)
    service = make_service(store, configured, [], {})
    uploaded = service.upload(job["id"], "../../outside.srt", VALID_SRT)
    assert uploaded["artifact_available"] is True
    selected = service.select(job["id"], manual_candidate_id=uploaded["id"])
    resumed = service.select(job["id"])
    assert resumed["id"] == selected["id"]
    assert not (configured.results_dir.parent / "outside.srt").exists()


def test_download_failure_persists_safe_diagnostic_without_workspace_path(store, configured):
    result = SubtitleResult("1", "one.srt", "Pulp Fiction", "1994", "en", None, "tt0110912")
    job = make_job(store)
    service = make_service(store, configured, [result], {})
    service.client.download = lambda *_: (_ for _ in ()).throw(OSError("/home/operator/secret.srt"))
    service.discover(job["id"])

    with pytest.raises(AttentionRequired):
        service.select(job["id"])

    candidate = store.list_candidates(job["id"])[0]
    assert candidate["parse_error"] == "Subtitle candidate could not be parsed."
    assert "/home/operator" not in repr(candidate)
