"""Safe operational errors, exception classification, and diagnostic redaction."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from api.domain import FailureCategory

if TYPE_CHECKING:
    from api.settings import Settings


_BEARER_RE = re.compile(r"\bbearer\s+[^\s,;]+", re.IGNORECASE)
_COOKIE_RE = re.compile(r"\b(set-cookie|cookie)\s*:\s*[^;\r\n]+", re.IGNORECASE)
_COOKIE_VALUE_RE = re.compile(r"\b(?:session|cookie|auth(?:entication)?)\w*=[^;\s,&]+", re.IGNORECASE)
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:access_token|api[_-]?key|token|secret|password|credential)=[^&#\s]+)",
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


def classify_exception(exc: Exception, operation: str) -> OperationalError:
    """Map known transient/deterministic exceptions to safe operation errors."""
    if isinstance(exc, OperationalError):
        return exc
    technical_detail = f"{type(exc).__name__}: {exc}"
    if _is_transient_exception(exc):
        return TransientFailure(
            f"{operation.capitalize()} could not be completed due to a temporary service failure.",
            code="transient_operation_failure",
            technical_detail=technical_detail,
        )
    if isinstance(exc, ValueError):
        return AttentionRequired(
            f"{operation.capitalize()} needs operator attention because its input is invalid.",
            code="invalid_operation_input",
            category=FailureCategory.VALIDATION,
            technical_detail=technical_detail,
            actions=("fix_input", "retry"),
        )
    if isinstance(exc, (FileNotFoundError, PermissionError)):
        return AttentionRequired(
            f"{operation.capitalize()} needs operator attention because required configuration or files are unavailable.",
            code="operation_configuration_required",
            category=FailureCategory.CONFIGURATION,
            technical_detail=technical_detail,
            actions=("fix_configuration", "retry"),
        )
    return AttentionRequired(
        f"{operation.capitalize()} stopped because of an unexpected error.",
        code="unexpected_operation_error",
        category=FailureCategory.UNEXPECTED,
        technical_detail=technical_detail,
        actions=("retry",),
    )


def sanitize_text(value: object, settings: Settings | None = None) -> str:
    """Remove credentials and workspace paths before diagnostics leave process memory."""
    text = "" if value is None else str(value)
    for secret in _environment_secrets():
        text = text.replace(secret, "[REDACTED]")
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _COOKIE_RE.sub(lambda match: f"{match.group(1)}: [REDACTED]", text)
    text = _COOKIE_VALUE_RE.sub("[REDACTED]", text)
    text = _QUERY_SECRET_RE.sub(_redact_query_secret, text)
    if settings is not None:
        workspace = str(settings.base_dir)
        if workspace:
            text = text.replace(workspace, "[WORKSPACE]")
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


def _is_transient_exception(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    return isinstance(status_code, int) and (status_code in {408, 425, 429} or status_code >= 500)


def _redact_query_secret(match: re.Match[str]) -> str:
    name = match.group(1).split("=", 1)[0]
    return f"{name}=[REDACTED]"
