"""The seam. One dependency, and the only way a route learns who is calling.

A route depends on `current_member` and receives a `Member`. It does not see a
token, a provider, or a claim set, which is what keeps provider specifics from
leaking into 22 routers and what makes ADR 0003's provider swap a change here
rather than everywhere.
"""

from typing import Optional

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from open_notebook.domain.member import Member
from open_notebook.exceptions import AuthenticationError
from open_notebook.identity.provider import GoTrueProvider, IdentityProvider

# auto_error=False so a missing header raises AuthenticationError through the
# application's own handler, which answers 401 with the same shape as every other
# error. FastAPI's own 403 for a missing credential would be both the wrong code
# and a different response body.
_bearer = HTTPBearer(auto_error=False)

_provider: Optional[IdentityProvider] = None


def get_provider() -> IdentityProvider:
    """Return the configured provider, constructed once.

    Constructed lazily rather than at import so that importing this module does
    not require identity configuration — the test suite, and any tooling that
    imports the app without signing anyone in, would otherwise need a secret.
    """
    global _provider
    if _provider is None:
        _provider = GoTrueProvider()
    return _provider


def set_provider(provider: Optional[IdentityProvider]) -> None:
    """Replace the provider. For tests and for a future provider migration."""
    global _provider
    _provider = provider


async def current_member(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Member:
    """Resolve the caller to a member, or refuse the request.

    Raises AuthenticationError — never returns None and never returns an empty
    result — so a route cannot accidentally treat an unauthenticated caller as one
    with no data. That distinction is the whole point: 401 says "you are nobody",
    an empty list says "you own nothing", and confusing them is how access checks
    silently pass.
    """
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Missing bearer token")
    if credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Authorization scheme must be Bearer")

    claims = get_provider().verify(credentials.credentials)
    member = await Member.resolve(
        provider=claims.provider, subject=claims.subject, email=claims.email
    )

    # Stashed for logging and for anything that needs the caller without
    # re-resolving. Reading it is a convenience; depending on `current_member` is
    # what actually enforces anything.
    request.state.member = member
    return member
