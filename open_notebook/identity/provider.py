"""Provider-specific token verification. The only place that knows about GoTrue.

Everything outside this package depends on `IdentityProvider` and the claims it
returns, never on how a particular provider signs or serves them. That is what
makes Requirement 4.4 achievable: a second provider satisfies this protocol and
nothing else changes.
"""

import os
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

import jwt
from loguru import logger

from open_notebook.exceptions import AuthenticationError, ConfigurationError
from open_notebook.utils.encryption import get_secret_from_env


@dataclass(frozen=True)
class MemberClaims:
    """What a verified token tells us about a person.

    Provider-neutral on purpose: `subject` is whatever that provider calls its
    stable user identifier, and nothing downstream cares which provider it was.
    """

    provider: str
    subject: str
    email: Optional[str] = None


@runtime_checkable
class IdentityProvider(Protocol):
    """Verifies a bearer token and reports who it belongs to.

    Runtime-checkable so a test can assert a replacement provider satisfies it,
    which is the only mechanical check available for Requirement 4.4.
    """

    name: str

    def verify(self, token: str) -> MemberClaims:
        """Return the claims of a valid token, or raise AuthenticationError."""
        ...


class GoTrueProvider:
    """Verifies GoTrue's HS256 access tokens.

    GoTrue signs with a shared secret rather than a rotating public key, so the
    verification material is a single environment value read once at
    construction — there is nothing to fetch per request and nothing to refresh.
    A provider that publishes a JWKS would cache differently behind the same
    protocol.
    """

    name = "gotrue"

    # GoTrue issues access tokens with this audience by default, and the compose
    # file sets it explicitly. Verified rather than ignored: a token minted for a
    # different audience by the same secret is not a token for this application.
    DEFAULT_AUDIENCE = "authenticated"

    def __init__(
        self,
        secret: Optional[str] = None,
        audience: Optional[str] = None,
        issuer: Optional[str] = None,
    ) -> None:
        # Narrowed into a local before assignment: the env helper returns an
        # optional, and an Optional[str] secret would type-check its way into
        # jwt.decode, where an empty key is a verification bypass.
        secret_value = secret or get_secret_from_env("GOTRUE_JWT_SECRET")
        if not secret_value:
            raise ConfigurationError(
                "GOTRUE_JWT_SECRET is not set, so member tokens cannot be verified"
            )
        self._secret: str = secret_value
        self._audience = audience or os.environ.get(
            "GOTRUE_JWT_AUD", self.DEFAULT_AUDIENCE
        )
        # Optional: GoTrue only sets `iss` when configured to. Verifying an issuer
        # that is absent from the token would reject every valid token, so this is
        # checked only when an expected value is supplied.
        self._issuer = issuer or os.environ.get("GOTRUE_JWT_ISS") or None

    def verify(self, token: str) -> MemberClaims:
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                audience=self._audience,
                issuer=self._issuer,
                # Both required: a token without `exp` never expires, and one
                # without `sub` identifies nobody.
                options={"require": ["exp", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError("Token has expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthenticationError("Token audience is not accepted") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthenticationError("Token issuer is not accepted") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise AuthenticationError(f"Token is missing a required claim: {exc}") from exc
        except jwt.InvalidTokenError as exc:
            # Covers a bad signature, malformed input and unsupported algorithms.
            # The reason is logged but not returned: which part of a token failed
            # is useful to an attacker and not to a member.
            logger.debug("rejected a member token: {}", exc)
            raise AuthenticationError("Token is not valid") from exc

        subject = payload.get("sub")
        if not subject:
            raise AuthenticationError("Token carries no subject")

        return MemberClaims(
            provider=self.name,
            subject=str(subject),
            email=payload.get("email"),
        )
