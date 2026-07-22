"""Bearer authentication for the operational API."""

from __future__ import annotations

import hmac

from fastapi import Request


def authorized(request: Request, token: str | None, allow_local: bool) -> bool:
    """Return whether an API request has the exact configured bearer token."""
    if token is None:
        return bool(allow_local)
    value = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not value.startswith(prefix) or not value[len(prefix) :]:
        return False
    return hmac.compare_digest(value[len(prefix) :], token)
