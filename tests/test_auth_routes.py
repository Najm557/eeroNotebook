"""Tests for the sign-in proxy (spec task 5.4).

The browser cannot reach the identity provider — it is unpublished — so these
routes perform the exchange. Two properties are load-bearing: a failure to reach
the provider must not be reported as a bad password, and the provider's own
messages must not be forwarded, because they distinguish "no such user" from
"wrong password" and hand out account enumeration for free.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.main import app
from open_notebook.domain.member import Member
from open_notebook.exceptions import AuthenticationError, ExternalServiceError
from open_notebook.identity import ADMIN_PROVIDER, ADMIN_SUBJECT, MemberClaims
from open_notebook.identity.signin import Session

ADMIN_PASSWORD = "operator-password-correct-horse"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def resolved_member():
    member = Member(provider=ADMIN_PROVIDER, subject=ADMIN_SUBJECT)
    with patch.object(
        Member, "resolve", new_callable=AsyncMock, return_value=member
    ) as resolve:
        yield resolve


class TestStatus:
    def test_describes_how_to_authenticate(self, client) -> None:
        body = client.get("/api/auth/status").json()
        assert body["auth_enabled"] is True
        assert body["method"] == "member_token"
        assert body["login_url"] == "/api/auth/login"
        assert body["signup_enabled"] is False

    def test_does_not_leak_the_provider_address(self, client) -> None:
        """It is unreachable from a browser, and advertising it only invites a
        client to try."""
        body = client.get("/api/auth/status").json()
        assert "auth_url" not in body
        assert not any(
            isinstance(v, str) and "9999" in v for v in body.values()
        ), "the provider's internal address must not appear"

    def test_reports_no_secret(self, client) -> None:
        body = client.get("/api/auth/status").json()
        assert isinstance(body["admin_password_configured"], bool)
        assert ADMIN_PASSWORD not in str(body)


class TestMemberLogin:
    def test_successful_sign_in_returns_a_session(self, client) -> None:
        session = Session(
            access_token="access-abc",
            refresh_token="refresh-def",
            expires_in=3600,
            email="person1@example.invalid",
        )
        with patch(
            "api.routers.auth.sign_in", new_callable=AsyncMock, return_value=session
        ) as signin:
            response = client.post(
                "/api/auth/login",
                json={"email": "person1@example.invalid", "password": "pw"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["access_token"] == "access-abc"
        assert body["refresh_token"] == "refresh-def"
        assert body["expires_in"] == 3600
        assert body["is_admin"] is False
        signin.assert_awaited_once()

    def test_bad_credentials_are_401(self, client) -> None:
        with patch(
            "api.routers.auth.sign_in",
            new_callable=AsyncMock,
            side_effect=AuthenticationError("Invalid credentials"),
        ):
            response = client.post(
                "/api/auth/login",
                json={"email": "person1@example.invalid", "password": "wrong"},
            )
        assert response.status_code == 401

    def test_unreachable_provider_is_not_reported_as_bad_credentials(
        self, client
    ) -> None:
        """Telling a member their password is wrong when the provider is down sends
        them round in circles resetting a password that was fine."""
        with patch(
            "api.routers.auth.sign_in",
            new_callable=AsyncMock,
            side_effect=ExternalServiceError("Identity provider is unreachable"),
        ):
            response = client.post(
                "/api/auth/login",
                json={"email": "person1@example.invalid", "password": "pw"},
            )
        assert response.status_code != 401
        assert response.status_code >= 500

    def test_empty_password_is_rejected_by_validation(self, client) -> None:
        response = client.post(
            "/api/auth/login", json={"email": "a@b.invalid", "password": ""}
        )
        assert response.status_code == 422


class TestAdminLogin:
    def test_password_alone_signs_in_the_operator(self, client, resolved_member) -> None:
        with patch(
            "api.routers.auth.claims_for_admin_password",
            return_value=MemberClaims(
                provider=ADMIN_PROVIDER, subject=ADMIN_SUBJECT, email=None
            ),
        ):
            response = client.post("/api/auth/login", json={"password": ADMIN_PASSWORD})
        assert response.status_code == 200
        body = response.json()
        assert body["is_admin"] is True
        assert body["access_token"] == ADMIN_PASSWORD
        assert body["refresh_token"] is None
        # The operator becomes a member like anyone else, so task 6's checks apply.
        resolved_member.assert_awaited_once()

    def test_wrong_admin_password_is_401(self, client) -> None:
        with patch("api.routers.auth.claims_for_admin_password", return_value=None):
            response = client.post("/api/auth/login", json={"password": "nope"})
        assert response.status_code == 401

    def test_a_bare_password_never_reaches_the_provider(self, client) -> None:
        """Forwarding it would send the operator's credential to the provider."""
        with patch(
            "api.routers.auth.claims_for_admin_password", return_value=None
        ), patch("api.routers.auth.sign_in", new_callable=AsyncMock) as signin:
            client.post("/api/auth/login", json={"password": "nope"})
        signin.assert_not_awaited()

    def test_operator_can_sign_in_while_the_provider_is_down(
        self, client, resolved_member
    ) -> None:
        """Checked locally on purpose: the provider being down is exactly when
        somebody needs to get in."""
        with patch(
            "api.routers.auth.claims_for_admin_password",
            return_value=MemberClaims(
                provider=ADMIN_PROVIDER, subject=ADMIN_SUBJECT, email=None
            ),
        ), patch(
            "api.routers.auth.sign_in",
            new_callable=AsyncMock,
            side_effect=ExternalServiceError("down"),
        ):
            response = client.post("/api/auth/login", json={"password": ADMIN_PASSWORD})
        assert response.status_code == 200


class TestRefresh:
    def test_refresh_returns_a_new_session(self, client) -> None:
        session = Session(
            access_token="new-access",
            refresh_token="new-refresh",
            expires_in=3600,
            email="person1@example.invalid",
        )
        with patch(
            "api.routers.auth.refresh", new_callable=AsyncMock, return_value=session
        ):
            response = client.post(
                "/api/auth/refresh", json={"refresh_token": "old-refresh"}
            )
        assert response.status_code == 200
        assert response.json()["access_token"] == "new-access"

    def test_expired_refresh_token_is_401(self, client) -> None:
        with patch(
            "api.routers.auth.refresh",
            new_callable=AsyncMock,
            side_effect=AuthenticationError("Invalid credentials"),
        ):
            response = client.post(
                "/api/auth/refresh", json={"refresh_token": "stale"}
            )
        assert response.status_code == 401

    def test_missing_refresh_token_is_rejected(self, client) -> None:
        assert client.post("/api/auth/refresh", json={}).status_code == 422


class TestWhoami:
    def test_reports_the_resolved_member(self, client) -> None:
        """Runs under conftest's bypass, so the member here is the test member."""
        response = client.get("/api/auth/me")
        assert response.status_code == 200
        body = response.json()
        assert body["provider"] == "test-suite"
        assert body["is_admin"] is False
