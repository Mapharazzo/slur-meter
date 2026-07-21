import pytest

from api.domain import (
    AttemptTrigger,
    FailureCategory,
    InvalidTransition,
    JobState,
    StageState,
    assert_job_transition,
    assert_stage_transition,
)
from api.settings import canonical_imdb_id, confined_path, validate_job_id


def test_completed_run_can_queue_an_explicit_publish_operation():
    assert_job_transition(JobState.COMPLETED, JobState.QUEUED)


def test_running_run_cannot_skip_to_queued_without_recovery():
    with pytest.raises(InvalidTransition):
        assert_job_transition(JobState.RUNNING, JobState.QUEUED)


def test_restart_recovery_can_requeue_a_running_job():
    assert_job_transition(
        JobState.RUNNING,
        JobState.QUEUED,
        trigger=AttemptTrigger.RESTART_RECOVERY,
    )


def test_queued_job_can_start_running():
    assert_job_transition(JobState.QUEUED, JobState.RUNNING)


def test_stage_can_complete_from_running():
    assert_stage_transition(StageState.RUNNING, StageState.COMPLETED)


def test_stage_cannot_complete_before_running():
    with pytest.raises(InvalidTransition):
        assert_stage_transition(StageState.PENDING, StageState.COMPLETED)


def test_domain_enums_use_durable_wire_values():
    assert JobState.NEEDS_ATTENTION.value == "needs_attention"
    assert StageState.SKIPPED.value == "skipped"
    assert AttemptTrigger.RESTART_RECOVERY.value == "restart_recovery"
    assert FailureCategory.TRANSIENT.value == "transient"


@pytest.mark.parametrize("value", ["../outside", "/tmp/x", "tt12/x", "q key"])
def test_job_ids_reject_path_material(value):
    with pytest.raises(ValueError):
        validate_job_id(value)


def test_job_ids_accept_generated_run_identifier():
    assert validate_job_id("job_3f21d8c1a62e4a90") == "job_3f21d8c1a62e4a90"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("110912", "tt0110912"), ("tt0110912", "tt0110912"), (110912, "tt0110912")],
)
def test_canonical_imdb_id_normalizes_provider_values(value, expected):
    assert canonical_imdb_id(value) == expected


@pytest.mark.parametrize("value", ["tt12/x", "q_4fed600a11", "../110912", "ttabcd"])
def test_canonical_imdb_id_rejects_non_imdb_identifiers(value):
    with pytest.raises(ValueError):
        canonical_imdb_id(value)


def test_confined_path_resolves_only_beneath_root(tmp_path):
    root = tmp_path / "workspace"
    assert confined_path(root, "jobs", "job_3f21d8c1a62e4a90", "output.json") == (
        root / "jobs" / "job_3f21d8c1a62e4a90" / "output.json"
    )


@pytest.mark.parametrize("parts", [("../outside",), ("jobs", "/tmp/x"), ("jobs", "..", "secret")])
def test_confined_path_rejects_traversal_and_absolute_parts(tmp_path, parts):
    with pytest.raises(ValueError):
        confined_path(tmp_path / "workspace", *parts)
