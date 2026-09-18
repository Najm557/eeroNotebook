"""Exchanging credentials for a session, on the member's behalf.

The browser cannot reach the identity provider: it is unpublished and lives on the
stack's internal network (Requirement 13.2). So the application performs the
exchange server-side and hands the session back. The alternative — publishing the
provider to the host — would trade a requirement away for convenience, and would
put an unauthenticated auth API on a shared network.

This module and `provider.py` are the only places that know which provider is in
use. Everything else deals in `Member`.
"""

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from open_notebook.exceptions import (
    AuthenticationError,
    ConfigurationError,
    ExternalServiceError,
)

# Generous but bounded: a slow provider should surface as an error, not as a
# request that hangs until something upstream gives up.
REQUEST_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class Session:
    """What a successful sign-in returns."""

    access_token: str
    refresh_token: Optional[str]
    expires_in: Optional[int]
    email: Optional[str]


def auth_base_url() -> str:
    url = os.environ.get("EERONOTEBOOK_AUTH_URL", "").rstrip("/")
    if not url:
        raise ConfigurationError(
            "EERONOTEBOOK_AUTH_URL is not set, so members cannot sign in"
        )
    return url


def _session_from_payload(payload: Dict[str, Any]) -> Session:
    token = payload.get("access_token")
    if not token:
        # A 200 with no token is a provider contract change, not a bad password.
        raise ExternalServiceError("Identity provider returned no access token")
    user = payload.get("user") or {}
    return Session(
        access_token=str(token),
        refresh_token=payload.get("refresh_token"),
        expires_in=payload.get("expires_in"),
        email=user.get("email"),
    )


async def _post(path: str, body: Dict[str, Any]) -> Session:
    url = f"{auth_base_url()}{path}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=body)
    except httpx.HTTPError as exc:
        # Distinguished from bad credentials on purpose: telling a member their
        # password is wrong when the provider is unreachable sends them in circles.
        logger.error("identity provider unreachable at {}: {}", url, exc)
        raise ExternalServiceError("Identity provider is unreachable") from exc

    if response.status_code in (400, 401, 403):
        # The provider's own message is not forwarded: it distinguishes "no such
        # user" from "wrong password", which is an account-enumeration gift.
        raise AuthenticationError("Invalid credentials")
    if response.status_code == 422:
        raise AuthenticationError("Invalid credentials")
    if response.status_code >= 500:
        logger.error(
            "identity provider error {} at {}: {}",
            response.status_code,
            url,
            response.text[:200],
        )
        raise ExternalServiceError("Identity provider failed")

    try:
        payload = response.json()
    except ValueError as exc:
        raise ExternalServiceError("Identity provider returned an unreadable response") from exc

    return _session_from_payload(payload)


async def sign_in(email: str, password: str) -> Session:
    """Exchange a member's email and password for a session."""
    return await _post(
        "/token?grant_type=password", {"email": email, "password": password}
    )


async def refresh(refresh_token: str) -> Session:
    """Exchange a refresh token for a new session.

    Access tokens expire in an hour where the shared password they replaced never
    did, so without this a member is signed out mid-session.
    """
    return await _post(
        "/token?grant_type=refresh_token", {"refresh_token": refresh_token}
    )
