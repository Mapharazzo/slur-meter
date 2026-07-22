"""Typed, operator-safe platform client failures."""

from __future__ import annotations

from collections.abc import Iterable

from api.domain import FailureCategory
from api.errors import AttentionRequired, ConfigurationRequired, TransientFailure


class PlatformCredentialsError(ConfigurationRequired):
    """A platform cannot authenticate with the configured credentials."""

    def __init__(
        self,
        message: str = "Publishing credentials are missing or invalid.",
        *,
        technical_detail: object = "",
    ):
        super().__init__(
            message,
            code="publishing_credentials_required",
            technical_detail=technical_detail,
        )


class PlatformTransientError(TransientFailure):
    """A pre-submit platform operation failed transiently."""

    def __init__(
        self,
        message: str = "Publishing was interrupted by a temporary platform failure.",
        *,
        technical_detail: object = "",
    ):
        super().__init__(
            message,
            code="publishing_transient_failure",
            technical_detail=technical_detail,
        )


class PlatformConfirmationError(AttentionRequired):
    """A platform response did not confirm a remote publication."""

    def __init__(
        self,
        message: str = "The platform did not confirm the publication.",
        *,
        technical_detail: object = "",
        actions: Iterable[str] = ("reconcile_publishing",),
    ):
        super().__init__(
            message,
            code="publishing_confirmation_failed",
            category=FailureCategory.VALIDATION,
            technical_detail=technical_detail,
            actions=actions,
        )


class PlatformStatsError(AttentionRequired):
    """A platform statistics response could not be verified."""

    def __init__(
        self,
        message: str = "Platform statistics could not be refreshed.",
        *,
        technical_detail: object = "",
    ):
        super().__init__(
            message,
            code="publishing_stats_failed",
            technical_detail=technical_detail,
            actions=("refresh_stats",),
        )
