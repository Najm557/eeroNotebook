"""Authentication for the frontend: discovery, sign-in, refresh, and identity.

The identity provider is unpublished, so the browser cannot reach it. These routes
perform the exchange server-side. `/status`, `/login` and `/refresh` are
unauthenticated by necessity — they are what a caller uses when they have no
session yet — and are listed in the middleware's exclusions.
"""

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from open_notebook.domain.member import Member
from open_notebook.exceptions import AuthenticationError
from open_notebook.identity import (
    ADMIN_PROVIDER,
    ADMIN_SUBJECT,
    admin_password,
    claims_for_admin_password,
    current_member,
)
from open_notebook.identity.signin import Session, refresh, sign_in

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """Credentials for a member, or the operator's password alone."""

    password: str = Field(min_length=1)
    # Absent means the operator signing in with the admin credential. Members
    # always supply an address.
    email: Optional[str] = None


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class SessionResponse(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None
    expires_in: Optional[int] = None
    email: Optional[str] = None
    is_admin: bool = False


class MemberResponse(BaseModel):
    id: str
    provider: str
    email: Optional[str] = None
    is_admin: bool


def _session_response(session: Session, is_admin: bool = False) -> SessionResponse:
    return SessionResponse(
        access_token=session.access_token,
        refresh_token=session.refresh_token,
        expires_in=session.expires_in,
        email=session.email,
        is_admin=is_admin,
    )


@router.get("/status")
async def get_auth_status():
    """Describe how a caller should authenticate.

    Deliberately does not report the provider's address: it is unreachable from a
    browser, and publishing it would only invite a client to try. Sign-in happens
    at this API's own `/api/auth/login`.

    `auth_enabled` is retained and always true — authentication is no longer
    optional, and a client reading upstream's field still gets a truthful answer.
    """
    return {
        "auth_enabled": True,
        "method": "member_token",
        "login_url": "/api/auth/login",
        "refresh_url": "/api/auth/refresh",
        # Members are created by the operator; there is no self-service signup.
        "signup_enabled": False,
        # Whether the operator credential exists. Not the credential, and it grants
        # nothing on its own — it resolves to an admin member subject to the same
        # access checks as any other.
        "admin_password_configured": bool(admin_password()),
        "message": "Sign in with your member credentials",
    }


@router.post("/login", response_model=SessionResponse)
async def login(request: LoginRequest) -> SessionResponse:
    """Exchange credentials for a session.

    The operator's password is checked first and locally. That is deliberate: it
    means the operator can still get in when the identity provider is down, which
    is exactly when someone needs to.
    """
    if request.email is None:
        claims = claims_for_admin_password(request.password)
        if claims is None:
            # Refused here rather than falling through to the provider: a bare
            # password with no address is only ever an admin attempt, and
            # forwarding it would send the operator's credential to the provider.
            raise AuthenticationError("Invalid credentials")
        # The admin credential *is* the bearer token the middleware accepts, so no
        # provider exchange is involved and there is nothing to refresh.
        await Member.resolve(provider=ADMIN_PROVIDER, subject=ADMIN_SUBJECT)
        return SessionResponse(
            access_token=request.password,
            refresh_token=None,
            expires_in=None,
            email=None,
            is_admin=True,
        )

    session = await sign_in(request.email, request.password)
    return _session_response(session)


@router.post("/refresh", response_model=SessionResponse)
async def refresh_session(request: RefreshRequest) -> SessionResponse:
    """Exchange a refresh token for a new session."""
    session = await refresh(request.refresh_token)
    return _session_response(session)


@router.get("/me", response_model=MemberResponse)
async def whoami(member: Member = Depends(current_member)) -> MemberResponse:
    """Who the caller is, for the signed-in indicator in the UI."""
    return MemberResponse(
        id=str(member.id),
        provider=member.provider,
        email=member.email,
        is_admin=member.provider == ADMIN_PROVIDER,
    )
