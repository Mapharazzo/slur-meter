import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ReadTimeout

from api.domain import FailureCategory
from api.errors import (
    AmbiguousPublishOutcome,
    AttentionRequired,
    OperationalError,
    TransientFailure,
    classify_exception,
    error_payload,
    sanitize_text,
)
from api.settings import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(base_dir=tmp_path, retry_delays=(0, 0, 0))


def test_diagnostics_redact_secrets_and_workspace(settings, monkeypatch):
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "super-secret-value")
    text = sanitize_text(
        "Bearer abc /home/mapha/slur-meter/x super-secret-value", settings
    )
    assert "abc" not in text
    assert "super-secret-value" not in text
    assert "/home/mapha" not in text


def test_diagnostics_redact_cookie_and_credential_query_parameter(settings):
    text = sanitize_text(
        "Cookie: first-cookie; later-cookie\n"
        "Set-Cookie: first-set-cookie; later-set-cookie\n"
        "request failed at https://example.test/?refresh_token=refresh-value&"
        "client_secret=client-value&access_token=access-value&safe=value",
        settings,
    )
    assert "first-cookie" not in text
    assert "later-cookie" not in text
    assert "first-set-cookie" not in text
    assert "later-set-cookie" not in text
    assert "refresh-value" not in text
    assert "client-value" not in text
    assert "access-value" not in text
    assert "safe=value" in text


def test_classification_redacts_configured_secrets_and_workspace_paths(tmp_path):
    settings = Settings(
        base_dir=tmp_path / "workspace",
        admin_api_token="configured-admin-token",
        data_dir=tmp_path / "configured-data",
        output_dir=tmp_path / "configured-output",
        results_dir=tmp_path / "configured-results",
    )
    detail = " ".join(
        [
            "configured-admin-token",
            str(settings.base_dir),
            str(settings.data_dir),
            str(settings.output_dir),
            str(settings.results_dir),
        ]
    )

    error = classify_exception(RuntimeError(detail), "diagnostics", settings=settings)

    assert "configured-admin-token" not in error.technical_detail
    assert str(settings.base_dir) not in error.technical_detail
    assert str(settings.data_dir) not in error.technical_detail
    assert str(settings.output_dir) not in error.technical_detail
    assert str(settings.results_dir) not in error.technical_detail


def test_operational_error_keeps_safe_fields_without_source_exception(settings):
    source = RuntimeError("Bearer hidden-token")
    error = OperationalError(
        code="provider_failed",
        message="The subtitle provider failed.",
        category=FailureCategory.TRANSIENT,
        retryable=True,
        technical_detail=str(source),
        actions=("retry",),
        status_code=503,
        settings=settings,
    )

    assert error.code == "provider_failed"
    assert error.retryable is True
    assert "hidden-token" not in error.technical_detail
    assert not hasattr(error, "source")


def test_timeout_is_classified_as_retryable_transient_failure():
    error = classify_exception(TimeoutError("network timed out"), "subtitle discovery")

    assert isinstance(error, TransientFailure)
    assert error.category is FailureCategory.TRANSIENT
    assert error.retryable is True


@pytest.mark.parametrize("exception_type", [ReadTimeout, RequestsConnectionError])
def test_requests_timeouts_and_connection_errors_are_retryable(exception_type):
    error = classify_exception(exception_type("provider unavailable"), "subtitle discovery")

    assert isinstance(error, TransientFailure)
    assert error.category is FailureCategory.TRANSIENT
    assert error.retryable is True


def test_missing_environment_configuration_requires_immediate_attention():
    error = classify_exception(KeyError("OPENSUBTITLES_API_KEY"), "subtitle discovery")

    assert type(error).__name__ == "ConfigurationRequired"
    assert error.category is FailureCategory.CONFIGURATION
    assert error.retryable is False
    assert error.actions == ("fix_configuration",)
    assert "OPENSUBTITLES_API_KEY" not in error.technical_detail


def test_invalid_content_requires_operator_attention():
    error = classify_exception(ValueError("invalid SRT"), "subtitle selection")

    assert isinstance(error, AttentionRequired)
    assert error.category is FailureCategory.VALIDATION
    assert error.retryable is False


def test_ambiguous_publish_outcome_is_never_retried_automatically():
    error = AmbiguousPublishOutcome("The upload may have been accepted.")

    assert error.category is FailureCategory.AMBIGUOUS_PUBLISH
    assert error.retryable is False
    assert "reconcile_publishing" in error.actions


def test_error_payload_uses_public_envelope_and_never_serializes_exception(settings):
    error = OperationalError(
        code="provider_failed",
        message="The subtitle provider failed.",
        category=FailureCategory.TRANSIENT,
        retryable=True,
        technical_detail="Bearer private-token",
        actions=("retry",),
        status_code=503,
        settings=settings,
    )

    payload = error_payload(error, "req_123")

    assert payload == {
        "error": {
            "code": "provider_failed",
            "message": "The subtitle provider failed.",
            "retryable": True,
            "details": {"actions": ["retry"]},
            "request_id": "req_123",
        }
    }


def test_settings_loads_env_without_overriding_deployment_values(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "ALLOWED_ORIGINS=https://from-file.example\nADMIN_API_TOKEN=file-token\n"
    )
    monkeypatch.setenv("ADMIN_API_TOKEN", "deployment-token")

    settings = Settings.from_env(tmp_path)

    assert settings.admin_api_token == "deployment-token"
    assert settings.allowed_origins == ("https://from-file.example",)


def test_settings_default_local_origins_and_test_retry_delays(tmp_path):
    settings = Settings(base_dir=tmp_path, retry_delays=(0, 0, 0))

    assert settings.allowed_origins == ("http://localhost:5173", "http://localhost:8001")
    assert settings.retry_delays == (0, 0, 0)


@pytest.mark.parametrize("delay", [float("nan"), float("inf"), float("-inf")])
def test_settings_reject_nonfinite_retry_delays(tmp_path, delay):
    with pytest.raises(ValueError, match="finite"):
        Settings(base_dir=tmp_path, retry_delays=(delay,))
