"""Authentication for every request, replacing upstream's shared-password gate.

Division of labour, kept deliberately sharp:

- This middleware answers *who is calling*. It refuses anyone it cannot resolve, so
  no route ever runs for an unidentified caller.
- Routes answer *what that member may reach*, by depending on `current_member`
  (spec task 6.2).

A middleware rather than a global dependency because it must refuse before routing,
matching the behaviour and the exact exclusion list upstream had — anything that
silently moved in or out of that list would silently gain or lose protection.
"""

from typing import List, Optional

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from open_notebook.domain.member import Member
from open_notebook.exceptions import AuthenticationError, ConfigurationError
from open_notebook.identity.admin import claims_for_admin_password
from open_notebook.identity.dependency import get_provider

# Exactly upstream's list. Preserved verbatim on purpose: `/api/config` and
# `/api/auth/status` are read by the frontend before anyone has signed in, and the
# documentation routes are how an operator inspects a deployment that is refusing
# their credentials.
DEFAULT_EXCLUDED_PATHS: List[str] = [
    "/",
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/api/auth/status",
    "/api/config",
    # Added for task 5.4, and the only additions to upstream's list. These are
    # what a caller uses when they have no session yet, so requiring one would be
    # circular. They are not a hole: /login and /refresh verify credentials
    # themselves and hand back a session, rather than granting access to data.
    "/api/auth/login",
    "/api/auth/refresh",
]


class MemberAuthMiddleware(BaseHTTPMiddleware):
    """Resolves every request to a member, or refuses it."""

    def __init__(
        self, app: ASGIApp, excluded_paths: Optional[List[str]] = None
    ) -> None:
        super().__init__(app)
        self.excluded_paths: List[str] = (
            list(excluded_paths) if excluded_paths is not None else list(DEFAULT_EXCLUDED_PATHS)
        )

    @staticmethod
    def _unauthorised(detail: str) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"detail": detail},
            headers={"WWW-Authenticate": "Bearer"},
        )

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path in self.excluded_paths:
            return await call_next(request)

        # CORS preflight carries no credentials by design.
        if request.method == "OPTIONS":
            return await call_next(request)

        header = request.headers.get("Authorization")
        if not header:
            return self._unauthorised("Missing authorization header")

        try:
            scheme, credentials = header.split(" ", 1)
        except ValueError:
            return self._unauthorised("Invalid authorization header format")
        if scheme.lower() != "bearer" or not credentials:
            return self._unauthorised("Invalid authorization header format")

        # The admin password is checked first and resolved as a member, not as a
        # bypass, so the operator is subject to the same access checks as anyone.
        claims = claims_for_admin_password(credentials)

        if claims is None:
            try:
                claims = get_provider().verify(credentials)
            except AuthenticationError as exc:
                return self._unauthorised(str(exc))
            except ConfigurationError as exc:
                # No signing secret configured. A deployment fault, not a caller's:
                # say so at 500 rather than telling everyone their token is bad.
                logger.error("identity is misconfigured: {}", exc)
                return JSONResponse(
                    status_code=500,
                    content={"detail": "Identity provider is not configured"},
                )

        try:
            member = await Member.resolve(
                provider=claims.provider, subject=claims.subject, email=claims.email
            )
        except Exception as exc:
            logger.error("could not resolve an authenticated caller: {}", exc)
            return JSONResponse(
                status_code=503,
                content={"detail": "Identity store is unavailable"},
            )

        # Routes read this through `current_member` rather than re-resolving.
        request.state.member = member
        return await call_next(request)
