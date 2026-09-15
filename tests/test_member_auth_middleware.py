"""Tests for the gate that replaced upstream's shared-password middleware.

These opt out of conftest's bypass with `no_auth_bypass`, because they are the
tests that must see the real thing refuse people.

Two properties matter most here. Authentication must fail *closed* — upstream
skipped it entirely when no password was configured, so a deployment that forgot
to set one served everything to anyone. And the admin password must resolve to a
member rather than bypass resolution, or every access check built on top of it in
task 6 would have a hole under it.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_notebook.domain.member import Member
from open_notebook.identity import ADMIN_PROVIDER, ADMIN_SUBJECT, MemberClaims
from open_notebook.identity import middleware as identity_middleware
from open_notebook.identity.middleware import MemberAuthMiddleware

pytestmark = pytest.mark.no_auth_bypass

ADMIN_PASSWORD = "operator-password-correct-horse"


def build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(MemberAuthMiddleware)

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/api/config")
    async def config():
        return {"ok": True}

    @app.get("/api/notebooks")
    async def notebooks(request_obj=None):
        return {"ok": True}

    @app.get("/api/whoami")
    async def whoami(request_obj=None):
        return {"ok": True}

    return app


@pytest.fixture
def resolved_member():
    member = Member(provider=ADMIN_PROVIDER, subject=ADMIN_SUBJECT)
    with patch.object(
        Member, "resolve", new_callable=AsyncMock, return_value=member
    ) as resolve:
        yield resolve


@pytest.fixture
def client():
    return TestClient(build_app())


class TestExcludedPaths:
    """Preserved verbatim from upstream: the frontend reads these before sign-in."""

    @pytest.mark.parametrize("path", ["/health", "/api/config"])
    def test_excluded_paths_need_no_credentials(self, client, path) -> None:
        assert client.get(path).status_code == 200

    def test_everything_else_needs_credentials(self, client) -> None:
        assert client.get("/api/notebooks").status_code == 401


class TestRefusals:
    def test_missing_header_is_refused(self, client) -> None:
        response = client.get("/api/notebooks")
        assert response.status_code == 401
        assert response.headers.get("WWW-Authenticate") == "Bearer"

    def test_non_bearer_scheme_is_refused(self, client) -> None:
        response = client.get(
            "/api/notebooks", headers={"Authorization": "Basic abc123"}
        )
        assert response.status_code == 401

    def test_malformed_header_is_refused(self, client) -> None:
        response = client.get("/api/notebooks", headers={"Authorization": "Bearer"})
        assert response.status_code == 401

    def test_empty_bearer_value_is_refused(self, client) -> None:
        response = client.get("/api/notebooks", headers={"Authorization": "Bearer "})
        assert response.status_code == 401

    def test_unknown_token_is_refused_not_admitted(self, client) -> None:
        """The decisive fail-closed check: with nothing configured, an arbitrary
        token must not be accepted. Upstream would have let this through."""
        with patch.object(identity_middleware, "claims_for_admin_password", return_value=None):
            response = client.get(
                "/api/notebooks", headers={"Authorization": "Bearer any-old-token"}
            )
        assert response.status_code in (401, 500), (
            "an unrecognised token must never reach a route"
        )

    def test_no_credentials_configured_still_refuses(self, client, monkeypatch) -> None:
        monkeypatch.delenv("OPEN_NOTEBOOK_PASSWORD", raising=False)
        monkeypatch.delenv("GOTRUE_JWT_SECRET", raising=False)
        response = client.get(
            "/api/notebooks", headers={"Authorization": "Bearer whatever"}
        )
        assert response.status_code != 200, (
            "authentication must fail closed when identity is unconfigured"
        )


class TestAdminPassword:
    def test_admin_password_resolves_to_a_member(self, client, resolved_member) -> None:
        with patch.object(
            identity_middleware,
            "claims_for_admin_password",
            return_value=MemberClaims(
                provider=ADMIN_PROVIDER, subject=ADMIN_SUBJECT, email=None
            ),
        ):
            response = client.get(
                "/api/notebooks",
                headers={"Authorization": f"Bearer {ADMIN_PASSWORD}"},
            )
        assert response.status_code == 200
        # The point of the whole design: the operator became a member, so task 6's
        # ownership checks will apply to them like anyone else.
        resolved_member.assert_awaited_once()
        assert resolved_member.await_args is not None
        assert resolved_member.await_args.kwargs["provider"] == ADMIN_PROVIDER

    def test_member_token_resolves_when_password_does_not_match(
        self, client, resolved_member
    ) -> None:
        class StubProvider:
            name = "stub"

            def verify(self, token: str) -> MemberClaims:
                return MemberClaims(provider="stub", subject="member-1", email=None)

        with patch.object(
            identity_middleware, "claims_for_admin_password", return_value=None
        ), patch.object(identity_middleware, "get_provider", return_value=StubProvider()):
            response = client.get(
                "/api/notebooks", headers={"Authorization": "Bearer a-member-token"}
            )
        assert response.status_code == 200


class TestIdentityStoreFailures:
    def test_unresolvable_member_is_not_treated_as_empty(self, client) -> None:
        """Requirement 4.5. A database failure must not read as 'this member owns
        nothing', which is the shape that silently passes an access check."""
        with patch.object(
            identity_middleware,
            "claims_for_admin_password",
            return_value=MemberClaims(
                provider=ADMIN_PROVIDER, subject=ADMIN_SUBJECT, email=None
            ),
        ), patch.object(
            Member, "resolve", new_callable=AsyncMock, side_effect=RuntimeError("db down")
        ):
            response = client.get(
                "/api/notebooks", headers={"Authorization": "Bearer pw"}
            )
        assert response.status_code == 503
        assert response.json()["detail"] != []


class TestExclusionListMatchesUpstream:
    def test_exact_upstream_paths(self) -> None:
        """Anything added or removed here silently gains or loses protection."""
        assert identity_middleware.DEFAULT_EXCLUDED_PATHS == [
            "/",
            "/health",
            "/docs",
            "/openapi.json",
            "/redoc",
            "/api/auth/status",
            "/api/config",
        ]
