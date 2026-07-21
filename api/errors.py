"""Safe operational errors, exception classification, and diagnostic redaction."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from api.domain import FailureCategory

if TYPE_CHECKING:
    from api.settings import Settings

try:
    from requests.exceptions import ConnectionError as RequestsConnectionError
    from requests.exceptions import ReadTimeout
except ImportError:  # pragma: no cover - requests is an application dependency.
    _REQUESTS_TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = ()
else:
    _REQUESTS_TRANSIENT_EXCEPTIONS = (ReadTimeout, RequestsConnectionError)


_BEARER_RE = re.compile(r"\bbearer\s+[^\s,;]+", re.IGNORECASE)
_COOKIE_HEADER_RE = re.compile(
    r"^(?P<name>set-cookie|cookie)\s*:\s*[^\r\n]*", re.IGNORECASE | re.MULTILINE
)
_COOKIE_VALUE_RE = re.compile(r"\b(?:session|cookie|auth(?:entication)?)\w*=[^;\s,&]+", re.IGNORECASE)
_QUERY_SECRET_RE = re.compile(
    r"(?P<prefix>[?&])(?P<name>[A-Za-z0-9_-]*(?:access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|api[_-]?key|token|secret|password|passwd|passphrase|credential|"
    r"auth(?:entication|orization)?|cookie|session)[A-Za-z0-9_-]*)=(?P<value>[^&#\s]*)",
    re.IGNORECASE,
)
_HOME_PATH_RE = re.compile(r"/(?:home|Users)/[^/\s]+(?:/[^\s]*)?")
_SENSITIVE_ENV_NAME_RE = re.compile(
    r"(?:token|secret|password|passwd|api[_-]?key|credential|cookie|private[_-]?key)",
    re.IGNORECASE,
)


class OperationalError(Exception):
    """A public-safe operational failure with no serialized source exception."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        category: FailureCategory,
        retryable: bool,
        technical_detail: object = "",
        actions: Iterable[str] = (),
        status_code: int = 500,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.category = category
        self.retryable = retryable
        self.technical_detail = sanitize_text(technical_detail, settings)
        self.actions = tuple(actions)
        self.status_code = status_code
        self.http_status = status_code


class AttentionRequired(OperationalError):  # noqa: N818 - public domain interface
    """A deterministic issue that requires an operator decision or correction."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "attention_required",
        category: FailureCategory = FailureCategory.DETERMINISTIC,
        technical_detail: object = "",
        actions: Iterable[str] = (),
        status_code: int = 422,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            category=category,
            retryable=False,
            technical_detail=technical_detail,
            actions=actions,
            status_code=status_code,
            settings=settings,
        )


class ConfigurationRequired(AttentionRequired):  # noqa: N818 - public domain interface
    """Configuration is missing or invalid and requires operator intervention."""

    def __init__(
        self,
        message: str = "Required configuration is missing or invalid.",
        *,
        code: str = "configuration_required",
        technical_detail: object = "",
        actions: Iterable[str] = ("fix_configuration",),
        status_code: int = 422,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            category=FailureCategory.CONFIGURATION,
            technical_detail=technical_detail,
            actions=actions,
            status_code=status_code,
            settings=settings,
        )


class TransientFailure(OperationalError):  # noqa: N818 - public domain interface
    """A bounded-retry external failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "transient_failure",
        technical_detail: object = "",
        actions: Iterable[str] = ("retry",),
        status_code: int = 503,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            category=FailureCategory.TRANSIENT,
            retryable=True,
            technical_detail=technical_detail,
            actions=actions,
            status_code=status_code,
            settings=settings,
        )


class AmbiguousPublishOutcome(AttentionRequired):
    """A post-submit failure that cannot safely be retried automatically."""

    def __init__(
        self,
        message: str = "The publishing result is unknown and requires reconciliation.",
        *,
        technical_detail: object = "",
        settings: Settings | None = None,
    ) -> None:
        super().__init__(
            message,
            code="ambiguous_publish_outcome",
            category=FailureCategory.AMBIGUOUS_PUBLISH,
            technical_detail=technical_detail,
            actions=("reconcile_publishing",),
            status_code=409,
            settings=settings,
        )


def classify_exception(
    exc: Exception,
    operation: str,
    settings: Settings | None = None,
) -> OperationalError:
    """Map known transient/deterministic exceptions to safe operation errors."""
    if isinstance(exc, OperationalError):
        return exc
    technical_detail = f"{type(exc).__name__}: {exc}"
    if _is_transient_exception(exc):
        return TransientFailure(
            f"{operation.capitalize()} could not be completed due to a temporary service failure.",
            code="transient_operation_failure",
            technical_detail=technical_detail,
            settings=settings,
        )
    if isinstance(exc, KeyError):
        return ConfigurationRequired(
            f"{operation.capitalize()} needs required configuration before it can run.",
            code="operation_configuration_required",
            technical_detail="A required configuration value is missing.",
            settings=settings,
        )
    if isinstance(exc, ValueError):
        return AttentionRequired(
            f"{operation.capitalize()} needs operator attention because its input is invalid.",
            code="invalid_operation_input",
            category=FailureCategory.VALIDATION,
            technical_detail=technical_detail,
            actions=("fix_input", "retry"),
            settings=settings,
        )
    if isinstance(exc, (FileNotFoundError, PermissionError)):
        return ConfigurationRequired(
            f"{operation.capitalize()} needs operator attention because required configuration or files are unavailable.",
            code="operation_configuration_required",
            technical_detail=technical_detail,
            settings=settings,
        )
    return AttentionRequired(
        f"{operation.capitalize()} stopped because of an unexpected error.",
        code="unexpected_operation_error",
        category=FailureCategory.UNEXPECTED,
        technical_detail=technical_detail,
        actions=("retry",),
        settings=settings,
    )


def sanitize_text(value: object, settings: Settings | None = None) -> str:
    """Remove credentials and workspace paths before diagnostics leave process memory."""
    text = "" if value is None else str(value)
    for sensitive_value in (*_environment_secrets(), *_configured_sensitive_values(settings)):
        text = text.replace(sensitive_value, "[REDACTED]")
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _COOKIE_HEADER_RE.sub(lambda match: f"{match.group('name')}: [REDACTED]", text)
    text = _COOKIE_VALUE_RE.sub("[REDACTED]", text)
    text = _QUERY_SECRET_RE.sub(_redact_query_secret, text)
    return _HOME_PATH_RE.sub("[WORKSPACE_PATH]", text)


def error_payload(error: OperationalError, request_id: str) -> dict[str, dict[str, object]]:
    """Return the common public API envelope without technical/source details."""
    return {
        "error": {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "details": {"actions": list(error.actions)},
            "request_id": request_id,
        }
    }


def _environment_secrets() -> tuple[str, ...]:
    return tuple(
        value
        for name, value in os.environ.items()
        if value and _SENSITIVE_ENV_NAME_RE.search(name)
    )


def _configured_sensitive_values(settings: Settings | None) -> tuple[str, ...]:
    if settings is None:
        return ()
    values = [settings.admin_api_token]
    values.extend(
        str(path)
        for path in (settings.base_dir, settings.data_dir, settings.output_dir, settings.results_dir)
        if path is not None
    )
    return tuple(sorted((value for value in values if value), key=len, reverse=True))


def _is_transient_exception(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, *_REQUESTS_TRANSIENT_EXCEPTIONS)):
        return True
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    return isinstance(status_code, int) and (status_code in {408, 425, 429} or status_code >= 500)


def _redact_query_secret(match: re.Match[str]) -> str:
    return f"{match.group('prefix')}{match.group('name')}=[REDACTED]"
