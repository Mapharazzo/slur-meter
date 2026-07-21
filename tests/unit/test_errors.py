import pytest

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
        "Cookie: session=very-secret; request failed at "
        "https://example.test/?access_token=another-secret&safe=value",
        settings,
    )
    assert "very-secret" not in text
    assert "another-secret" not in text
    assert "safe=value" in text


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
