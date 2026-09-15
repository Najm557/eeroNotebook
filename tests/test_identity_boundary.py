"""Tests for the identity boundary (spec task 5.2).

Two things are under test. First, that token verification refuses everything it
should — an access check that fails open is worse than none, because it looks
like it is working. Second, that the boundary is actually a boundary: the last
test reads the source tree and fails if provider specifics have leaked out of
`open_notebook/identity/`, because a seam nothing enforces stops being one at the
first convenient import.
"""

import time
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from open_notebook.domain.member import Member
from open_notebook.exceptions import AuthenticationError, ConfigurationError
from open_notebook.identity import (
    GoTrueProvider,
    IdentityProvider,
    MemberClaims,
    set_provider,
)
from open_notebook.identity.dependency import current_member

# At least 32 bytes: PyJWT warns below that for SHA256, and a suite that emits
# warnings trains people to ignore them.
SECRET = "test-secret-long-enough-for-sha256-at-least-32-bytes"
AUDIENCE = "authenticated"


def make_token(
    secret: str = SECRET,
    audience: str = AUDIENCE,
    subject: Optional[str] = "provider-subject-123",
    expires_in: int = 3600,
    issuer: Optional[str] = None,
    email: Optional[str] = "member@example.com",
    algorithm: str = "HS256",
) -> str:
    payload: dict = {"aud": audience, "iat": int(time.time())}
    if subject is not None:
        payload["sub"] = subject
    if expires_in is not None:
        payload["exp"] = int(time.time()) + expires_in
    if issuer is not None:
        payload["iss"] = issuer
    if email is not None:
        payload["email"] = email
    return jwt.encode(payload, secret, algorithm=algorithm)


@pytest.fixture
def provider() -> GoTrueProvider:
    return GoTrueProvider(secret=SECRET, audience=AUDIENCE)


class TestTokenVerification:
    """Requirement 4.5: an unresolvable request is refused, never treated as empty."""

    def test_valid_token_yields_claims(self, provider: GoTrueProvider) -> None:
        claims = provider.verify(make_token())
        assert claims.subject == "provider-subject-123"
        assert claims.email == "member@example.com"
        assert claims.provider == "gotrue"

    def test_expired_token_is_refused(self, provider: GoTrueProvider) -> None:
        with pytest.raises(AuthenticationError, match="expired"):
            provider.verify(make_token(expires_in=-60))

    def test_wrong_signature_is_refused(self, provider: GoTrueProvider) -> None:
        other_secret = "a-different-secret-also-long-enough-for-sha256-32"
        with pytest.raises(AuthenticationError):
            provider.verify(make_token(secret=other_secret))

    def test_malformed_token_is_refused(self, provider: GoTrueProvider) -> None:
        with pytest.raises(AuthenticationError):
            provider.verify("this-is-not-a-jwt")

    def test_wrong_audience_is_refused(self, provider: GoTrueProvider) -> None:
        with pytest.raises(AuthenticationError, match="audience"):
            provider.verify(make_token(audience="some-other-service"))

    def test_wrong_issuer_is_refused(self) -> None:
        strict = GoTrueProvider(
            secret=SECRET, audience=AUDIENCE, issuer="https://expected.example"
        )
        with pytest.raises(AuthenticationError, match="issuer"):
            strict.verify(make_token(issuer="https://someone-else.example"))

    def test_issuer_is_only_checked_when_expected(
        self, provider: GoTrueProvider
    ) -> None:
        """GoTrue omits `iss` unless configured, so verifying it unconditionally
        would reject every valid token."""
        assert provider.verify(make_token(issuer=None)).subject

    def test_missing_subject_is_refused(self, provider: GoTrueProvider) -> None:
        with pytest.raises(AuthenticationError):
            provider.verify(make_token(subject=None))

    def test_unsigned_token_is_refused(self, provider: GoTrueProvider) -> None:
        """`alg: none` is the classic bypass; PyJWT must not accept it here."""
        unsigned = jwt.encode({"sub": "x", "aud": AUDIENCE}, key="", algorithm="none")
        with pytest.raises(AuthenticationError):
            provider.verify(unsigned)

    def test_missing_secret_is_a_configuration_error(self, monkeypatch) -> None:
        """Not an AuthenticationError: nobody's credentials are wrong, the
        deployment is. Failing loudly beats accepting every token."""
        monkeypatch.delenv("GOTRUE_JWT_SECRET", raising=False)
        with pytest.raises(ConfigurationError):
            GoTrueProvider()


class StubProvider:
    """A second provider satisfying the same protocol.

    Requirement 4.4 says replacing the provider must not require application
    changes. The only honest check of that is another provider passing through the
    same seam untouched.
    """

    name = "stub-provider"

    def verify(self, token: str) -> MemberClaims:
        if token != "good-token":
            raise AuthenticationError("Token is not valid")
        return MemberClaims(
            provider=self.name, subject="stub-subject-1", email="stub@example.com"
        )


class TestSeamAcceptsAnyProvider:
    def test_stub_satisfies_the_protocol(self) -> None:
        assert isinstance(StubProvider(), IdentityProvider)

    @pytest.mark.asyncio
    async def test_a_different_provider_resolves_through_the_same_seam(self) -> None:
        resolved = Member(provider="stub-provider", subject="stub-subject-1")
        try:
            set_provider(StubProvider())
            with patch.object(
                Member, "resolve", new_callable=AsyncMock, return_value=resolved
            ) as resolve:
                request = _request()
                member = await current_member(request, _credentials("good-token"))
            assert member is resolved
            assert resolve.await_args is not None
            assert resolve.await_args.kwargs["provider"] == "stub-provider"
            assert request.state.member is resolved
        finally:
            set_provider(None)

    @pytest.mark.asyncio
    async def test_absent_token_is_refused(self) -> None:
        try:
            set_provider(StubProvider())
            with pytest.raises(AuthenticationError, match="Missing bearer token"):
                await current_member(_request(), None)
        finally:
            set_provider(None)

    @pytest.mark.asyncio
    async def test_non_bearer_scheme_is_refused(self) -> None:
        try:
            set_provider(StubProvider())
            with pytest.raises(AuthenticationError, match="Bearer"):
                await current_member(
                    _request(), _credentials("good-token", scheme="Basic")
                )
        finally:
            set_provider(None)


def _request() -> Request:
    """A real Request, not a stand-in: the dependency writes to request.state,
    and a fake with a plain attribute would pass while the real object behaved
    differently."""
    return Request({"type": "http", "headers": [], "state": {}})


def _credentials(
    token: str, scheme: str = "Bearer"
) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme=scheme, credentials=token)


class TestBoundaryIsEnforced:
    """The seam is only real if something fails when it is crossed."""

    IDENTITY_PACKAGE = "open_notebook/identity"
    # Directories that make up the application. Deliberately excludes tests: this
    # file imports jwt to mint tokens, which is the point.
    SEARCH_ROOTS = ("open_notebook", "api", "commands")
    FORBIDDEN_IMPORTS = ("import jwt", "from jwt", "import authlib", "from authlib")
    FORBIDDEN_ENV = ("GOTRUE_",)

    def _application_modules(self) -> list[Path]:
        repo_root = Path(__file__).resolve().parent.parent
        files: list[Path] = []
        for root in self.SEARCH_ROOTS:
            for path in (repo_root / root).rglob("*.py"):
                rel = path.relative_to(repo_root).as_posix()
                if rel.startswith(self.IDENTITY_PACKAGE):
                    continue
                if "__pycache__" in rel:
                    continue
                files.append(path)
        return files

    def test_there_are_modules_to_check(self) -> None:
        """Guards the two tests below: a search that finds nothing would pass
        vacuously and report a boundary that was never examined."""
        assert len(self._application_modules()) > 50

    def test_no_jwt_library_outside_the_boundary(self) -> None:
        offenders = []
        for path in self._application_modules():
            text = path.read_text(encoding="utf-8")
            for needle in self.FORBIDDEN_IMPORTS:
                if needle in text:
                    offenders.append(f"{path.name}: {needle}")
        assert not offenders, (
            "Token handling belongs behind open_notebook/identity/. "
            f"Found: {offenders}"
        )

    def test_no_gotrue_configuration_read_outside_the_boundary(self) -> None:
        offenders = []
        for path in self._application_modules():
            text = path.read_text(encoding="utf-8")
            for needle in self.FORBIDDEN_ENV:
                if needle in text:
                    offenders.append(f"{path.name}: {needle}")
        assert not offenders, (
            "Provider configuration belongs behind open_notebook/identity/, so "
            f"the provider can be replaced there alone. Found: {offenders}"
        )

    def test_member_domain_is_provider_agnostic(self) -> None:
        """Ownership references a member; a member references a provider. The
        domain model must not know which provider that is."""
        text = (
            Path(__file__).resolve().parent.parent
            / "open_notebook/domain/member.py"
        ).read_text(encoding="utf-8")
        assert "gotrue" not in text.lower()
