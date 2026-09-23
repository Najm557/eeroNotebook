"""Share management: create, list and revoke. Spec task 6.3.

Requirements 6.1, 6.2, 6.3, 6.4 and 6.6.

Enforcement itself belongs to task 6.2 and is covered by tests/test_access_control.py
— these tests are about the three things that are new here and could each be wrong
in a way nobody would notice:

- **Requirement 6.6 held structurally.** No request can name more than one member,
  and no request can name a role. That has to be a property of the request model
  and of the SurrealQL, not a rule somebody remembers, so it is asserted against
  both rather than by sending one well-formed request and calling it proved.
- **Revocation leaves contents alone** (Requirement 6.5) and takes effect on the
  next request. The failure to catch is a revoke that reaches a content table, so
  the statement is inspected, not just its effect on one fixture.
- **Owner-only on all three routes**, including the list — a Viewer reading it
  would learn the other Viewers' addresses, and visibility never runs sideways.

Naming a member is the decision this task had to make, and the tests pin what was
chosen: an address resolves against this instance's own `member` table, and an
address with no local row is refused with a message that does not claim the account
does not exist, because the lookup cannot tell "no account" from "has one and has
never signed in" (task 5.6).

The SurrealQL these tests mock was separately verified against a real SurrealDB
2.6.5 running the real migration chain to version 25 — 29 checks, including
case-insensitive matching, the UNIQUE index rejecting a second grant, the schema
refusing a role other than `viewer`, a revoke leaving Sources and notes in place,
and access ending on the next check.
"""

import inspect
from contextlib import ExitStack
from typing import get_args, get_origin
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.models import NotebookResponse, ShareCreate, ShareResponse
from open_notebook.domain import access, share
from open_notebook.domain.access import Role, role_from_owner
from open_notebook.domain.member import Member
from open_notebook.exceptions import (
    AccessUnavailableError,
    InvalidInputError,
)

pytestmark = pytest.mark.no_access_bypass

# `member:testsuite` is the caller conftest resolves every request to.
CALLER = Member(id="member:testsuite", provider="test", subject="caller")
TARGET = Member(
    id="member:student", provider="test", subject="student", email="student@example.invalid"
)


@pytest.fixture
def client():
    return TestClient(app)


def owned_db(*, owner="member:testsuite", viewers=(), shares=None, members=None):
    """SurrealDB stood in for by query shape, as tests/test_access_control.py does.

    Keyed on the query text so a test cannot pass by answering the wrong question.
    """
    shares = list(shares or [])
    members = list(members or [])
    seen: list = []

    async def query(sql, params=None):
        params = params or {}
        seen.append((sql, params))
        if sql.startswith("SELECT id, owner FROM $notebook"):
            return [{"id": str(params["notebook"]), "owner": owner}]
        if sql.startswith("SELECT VALUE role FROM share"):
            return ["viewer"] if str(params.get("member")) in viewers else []
        if sql.startswith("SELECT * FROM member WHERE email"):
            return [
                m
                for m in members
                if (m.get("email") or "").lower() == params.get("email")
            ][:1]
        if sql.startswith("SELECT * FROM share WHERE out"):
            return shares
        if sql.startswith("SELECT * FROM share WHERE in"):
            return [
                s for s in shares if str(s["in"]) == str(params.get("member"))
            ][:1]
        if sql.startswith("SELECT id, email FROM member WHERE id IN"):
            wanted = {str(m) for m in params.get("members", [])}
            return [m for m in members if str(m["id"]) in wanted]
        if sql.startswith("RELATE $member->share->$notebook"):
            shares.append(
                {
                    "in": str(params["member"]),
                    "out": str(params["notebook"]),
                    "role": "viewer",
                    "created": "2026-01-01T00:00:00Z",
                }
            )
            return []
        if sql.startswith("DELETE share WHERE in"):
            removed = [s for s in shares if str(s["in"]) == str(params["member"])]
            for row in removed:
                shares.remove(row)
            return removed
        raise AssertionError(f"unexpected share query: {sql}")

    db = AsyncMock(side_effect=query)
    db.seen = seen
    return db


MEMBER_ROW = {
    "id": "member:student",
    "email": "student@example.invalid",
    "provider": "test",
    "subject": "student",
}
CALLER_ROW = {
    "id": "member:testsuite",
    "email": "owner@example.invalid",
    "provider": "test",
    "subject": "caller",
}
SHARE_ROW = {
    "in": "member:student",
    "out": "notebook:mine",
    "role": "viewer",
    "created": "2026-01-01T00:00:00Z",
}


def _both(db):
    """One stand-in database behind both modules.

    Share management and access enforcement read through the same fake so a test
    exercises the real sequence: the router resolves access first, and only then
    does the share module run. A separate mock per module would let a test pass
    with the ownership check answered by something the route never called.
    """
    stack = ExitStack()
    stack.enter_context(patch.object(access, "repo_query", db))
    stack.enter_context(patch.object(share, "repo_query", db))
    return stack


class TestOnlyTheOwnerManagesShares:
    """All three routes, not just the writes. Requirements 6.1 and 6.4."""

    def test_the_owner_lists_shares(self, client):
        db = owned_db(shares=[dict(SHARE_ROW)], members=[MEMBER_ROW])
        with _both(db):
            response = client.get("/api/notebooks/notebook:mine/shares")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["email"] == "student@example.invalid"
        assert body[0]["role"] == "viewer"

    def test_a_viewer_cannot_list_shares(self, client):
        """A Viewer reading the list would learn the other Viewers' addresses.
        Visibility runs to the owner and never sideways between Viewers."""
        db = owned_db(owner="member:someone-else", viewers={"member:testsuite"})
        with _both(db):
            response = client.get("/api/notebooks/notebook:shared/shares")
        assert response.status_code == 403

    def test_a_stranger_gets_404_rather_than_403(self, client):
        """Requirement 7.4: the refusal must not confirm the notebook exists."""
        db = owned_db(owner="member:someone-else")
        with _both(db):
            for call in (
                lambda: client.get("/api/notebooks/notebook:theirs/shares"),
                lambda: client.post(
                    "/api/notebooks/notebook:theirs/shares",
                    json={"email": "student@example.invalid"},
                ),
                lambda: client.delete(
                    "/api/notebooks/notebook:theirs/shares/member:student"
                ),
            ):
                assert call().status_code == 404

    def test_a_viewer_cannot_create_or_revoke(self, client):
        db = owned_db(
            owner="member:someone-else",
            viewers={"member:testsuite"},
            members=[MEMBER_ROW],
        )
        with _both(db):
            created = client.post(
                "/api/notebooks/notebook:shared/shares",
                json={"email": "student@example.invalid"},
            )
            revoked = client.delete(
                "/api/notebooks/notebook:shared/shares/member:student"
            )
        assert created.status_code == 403
        assert revoked.status_code == 403

    def test_a_viewer_is_refused_before_any_address_is_looked_up(self, client):
        """The access check runs first, so a Viewer cannot use the share endpoint
        as an address oracle on somebody else's notebook."""
        db = owned_db(
            owner="member:someone-else",
            viewers={"member:testsuite"},
            members=[MEMBER_ROW],
        )
        with _both(db):
            client.post(
                "/api/notebooks/notebook:shared/shares",
                json={"email": "student@example.invalid"},
            )
        assert not any(
            sql.startswith("SELECT * FROM member WHERE email") for sql, _ in db.seen
        )


class TestCreatingAShare:
    def test_the_owner_grants_viewer_access(self, client):
        """Requirements 6.1 and 6.2."""
        db = owned_db(members=[MEMBER_ROW])
        with _both(db):
            response = client.post(
                "/api/notebooks/notebook:mine/shares",
                json={"email": "student@example.invalid"},
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["member_id"] == "member:student"
        assert body["notebook_id"] == "notebook:mine"
        assert body["role"] == "viewer"

    def test_the_relation_is_written_with_the_role_as_a_literal(self, client):
        """`role` must not be reachable from a request. It is written into the
        statement rather than bound, so there is no parameter to influence."""
        db = owned_db(members=[MEMBER_ROW])
        with _both(db):
            client.post(
                "/api/notebooks/notebook:mine/shares",
                json={"email": "student@example.invalid"},
            )
        relates = [
            (sql, params)
            for sql, params in db.seen
            if sql.startswith("RELATE $member->share->$notebook")
        ]
        assert len(relates) == 1
        sql, params = relates[0]
        assert "SET role = 'viewer'" in sql
        assert "role" not in params
        assert set(params) == {"member", "notebook"}

    def test_the_address_is_matched_case_insensitively(self, client):
        db = owned_db(members=[MEMBER_ROW])
        with _both(db):
            response = client.post(
                "/api/notebooks/notebook:mine/shares",
                json={"email": "  Student@Example.Invalid  "},
            )
        assert response.status_code == 200, response.text

    def test_an_address_with_no_local_member_is_refused_without_claiming_absence(
        self, client
    ):
        """The decision this task had to make.

        The lookup is against this instance's `member` table, which only gains a
        row on a person's first sign-in (task 5.6), so "no row" covers both "no
        such account" and "has an account, has never signed in here". The message
        must not pick one, and it must tell the owner what to do instead.
        """
        db = owned_db(members=[MEMBER_ROW])
        with _both(db):
            response = client.post(
                "/api/notebooks/notebook:mine/shares",
                json={"email": "nobody@example.invalid"},
            )
        assert response.status_code == 404
        detail = response.json()["detail"]
        assert "sign in once" in detail
        assert "no such" not in detail.lower()
        assert "does not exist" not in detail.lower()

    def test_sharing_with_yourself_is_refused(self, client):
        db = owned_db(members=[CALLER_ROW])
        with _both(db):
            response = client.post(
                "/api/notebooks/notebook:mine/shares",
                json={"email": "owner@example.invalid"},
            )
        assert response.status_code == 400
        assert "already own" in response.json()["detail"]

    def test_granting_twice_does_not_create_a_second_share(self, client):
        """The UNIQUE index on (in, out) would reject it, and two Shares for one
        pair would make revocation partial. An owner clicking twice has not done
        anything wrong, so this is idempotent rather than an error."""
        db = owned_db(shares=[dict(SHARE_ROW)], members=[MEMBER_ROW])
        with _both(db):
            response = client.post(
                "/api/notebooks/notebook:mine/shares",
                json={"email": "student@example.invalid"},
            )
        assert response.status_code == 200
        assert not any(sql.startswith("RELATE") for sql, _ in db.seen)

    def test_a_malformed_address_is_a_400_not_a_lookup(self, client):
        db = owned_db(members=[MEMBER_ROW])
        with _both(db):
            response = client.post(
                "/api/notebooks/notebook:mine/shares", json={"email": "not-an-address"}
            )
        assert response.status_code == 400
        assert not any(
            sql.startswith("SELECT * FROM member WHERE email") for sql, _ in db.seen
        )


class TestRequirement66IsStructural:
    """No single action makes a notebook visible to the team at large.

    Asserted against the request model and the module surface rather than by
    sending one request, because the failure mode is a field added later.
    """

    def test_the_request_carries_exactly_one_address(self):
        assert list(ShareCreate.model_fields) == ["email"]

    def test_the_request_has_no_collection_field(self):
        for name, field in ShareCreate.model_fields.items():
            origin = get_origin(field.annotation)
            assert origin not in (list, set, tuple, frozenset), (
                f"ShareCreate.{name} accepts a collection; one request must grant "
                "one member access to one notebook"
            )

    @pytest.mark.parametrize(
        "body",
        [
            {"emails": ["a@example.invalid", "b@example.invalid"]},
            {"email": "a@example.invalid", "role": "editor"},
            {"email": "a@example.invalid", "members": ["b@example.invalid"]},
            {"email": "a@example.invalid", "group": "everyone"},
            {"email": "*"},
        ],
    )
    def test_a_request_naming_more_than_one_member_is_rejected(self, client, body):
        """Extra fields are forbidden rather than ignored, so a plural field
        cannot be added to the client and quietly do nothing until somebody adds
        it to the server too. `*` is refused by address validation."""
        db = owned_db(members=[MEMBER_ROW])
        with _both(db):
            response = client.post("/api/notebooks/notebook:mine/shares", json=body)
        assert response.status_code in (400, 422), response.text
        assert not any(sql.startswith("RELATE") for sql, _ in db.seen)

    def test_there_is_no_plural_grant_in_the_module(self):
        """A `grant_viewers` or `share_with_all` would make Requirement 6.6 a
        matter of which function a route happened to call."""
        public = [
            name
            for name, obj in vars(share).items()
            if not name.startswith("_")
            and inspect.isfunction(obj)
            and obj.__module__ == share.__name__
        ]
        assert sorted(public) == [
            "find_share",
            "grant_viewer",
            "list_shares",
            "member_for_email",
            "normalise_email",
            "revoke",
        ], public

    def test_grant_viewer_takes_one_member(self):
        params = list(inspect.signature(share.grant_viewer).parameters)
        assert params == ["notebook_id", "member_id"]

    def test_the_only_role_is_viewer(self):
        assert share.VIEWER_ROLE == "viewer"
        assert [r.value for r in Role] == ["owner", "viewer"]
        assert get_args(ShareResponse.model_fields["role"].annotation) == ("viewer",)


class TestRevocation:
    def test_the_owner_revokes(self, client):
        db = owned_db(shares=[dict(SHARE_ROW)], members=[MEMBER_ROW])
        with _both(db):
            response = client.delete(
                "/api/notebooks/notebook:mine/shares/member:student"
            )
        assert response.status_code == 200

    def test_revoking_touches_only_the_share_relation(self, client):
        """Requirement 6.5. The statement names both endpoints of the relation, so
        it can remove at most that one grant and cannot reach a content table."""
        db = owned_db(shares=[dict(SHARE_ROW)], members=[MEMBER_ROW])
        with _both(db):
            client.delete("/api/notebooks/notebook:mine/shares/member:student")
        deletes = [sql for sql, _ in db.seen if "DELETE" in sql.upper()]
        assert deletes == [
            "DELETE share WHERE in = $member AND out = $notebook RETURN BEFORE"
        ]
        for sql in deletes:
            for table in ("source", "note", "notebook", "member", "source_embedding"):
                assert f"DELETE {table}" not in sql
                assert f"DELETE FROM {table}" not in sql

    def test_revoking_a_share_that_does_not_exist_is_404(self, client):
        """Reported rather than answered as success: an owner told "revoked" when
        nothing was revoked would stop looking."""
        db = owned_db(members=[MEMBER_ROW])
        with _both(db):
            response = client.delete(
                "/api/notebooks/notebook:mine/shares/member:student"
            )
        assert response.status_code == 404

    def test_a_malformed_member_id_reads_as_no_such_share(self, client):
        """Not a 500. A malformed id must answer the same way as a well-formed one
        holding no share."""
        db = owned_db(members=[MEMBER_ROW])
        with _both(db):
            response = client.delete("/api/notebooks/notebook:mine/shares/a:b:c")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_access_ends_on_the_next_check(self):
        """Requirement 6.5's "immediately". Nothing caches a role, so the next
        `notebook_access` is the next request."""
        db = owned_db(
            owner="member:owner", viewers={"member:student"}, shares=[dict(SHARE_ROW)]
        )
        viewer = Member(id="member:student", provider="test", subject="student")
        with _both(db):
            before = await access.notebook_access(viewer, "notebook:mine")
            assert before.role is Role.VIEWER

            revoked = await share.revoke("notebook:mine", "member:student")
            assert revoked is True

        # The share row is gone, so the role query answers empty on the next read.
        db_after = owned_db(owner="member:owner")
        with _both(db_after):
            after = await access.notebook_access(viewer, "notebook:mine")
        assert after.role is None

    @pytest.mark.asyncio
    async def test_a_revoke_that_cannot_run_is_not_reported_as_done(self):
        """An outage answered as "nothing to revoke" would tell an owner access had
        already ended. Same rule task 6.2 applied to access decisions."""
        db = AsyncMock(side_effect=RuntimeError("connection refused"))
        with patch.object(share, "repo_query", db):
            with pytest.raises(AccessUnavailableError):
                await share.revoke("notebook:mine", "member:student")


class TestAFailedReadIsNeverAnEmptyList:
    @pytest.mark.asyncio
    async def test_listing_raises_rather_than_reporting_nobody(self):
        """"Nobody has access" is something an owner acts on; an outage must not
        look like it."""
        db = AsyncMock(side_effect=RuntimeError("connection refused"))
        with patch.object(share, "repo_query", db):
            with pytest.raises(AccessUnavailableError):
                await share.list_shares("notebook:mine")

    @pytest.mark.asyncio
    async def test_an_address_lookup_raises_rather_than_answering_unknown(self):
        """An outage read as "no such member" would send an owner to ask their
        student to sign in again for no reason."""
        db = AsyncMock(side_effect=RuntimeError("connection refused"))
        with patch.object(share, "repo_query", db):
            with pytest.raises(AccessUnavailableError):
                await share.member_for_email("student@example.invalid")

    def test_a_list_outage_is_503(self, client):
        db = AsyncMock(side_effect=RuntimeError("connection refused"))
        with _both(db):
            response = client.get("/api/notebooks/notebook:mine/shares")
        assert response.status_code == 503


class TestNormaliseEmail:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Student@Example.Invalid", "student@example.invalid"),
            ("  student@example.invalid  ", "student@example.invalid"),
        ],
    )
    def test_it_trims_and_lowercases(self, raw, expected):
        assert share.normalise_email(raw) == expected

    @pytest.mark.parametrize(
        "raw", ["", "   ", "*", "nobody", "a@b@c", "@example.invalid", "a@", "a b@c.d"]
    )
    def test_it_refuses_what_cannot_be_an_address(self, raw):
        with pytest.raises(InvalidInputError):
            share.normalise_email(raw)


class TestTheUiCanTellOwnerFromViewer:
    """The consequence task 6.2 left: owner-only actions must not be offered to a
    Viewer, so a client has to be able to tell which it is."""

    def test_a_notebook_read_reports_the_callers_role(self, client):
        db = owned_db(owner="member:someone-else", viewers={"member:testsuite"})
        with _both(db):
            with patch(
                "api.routers.notebooks.repo_query", new_callable=AsyncMock
            ) as route_query:
                route_query.return_value = [
                    {
                        "id": "notebook:shared",
                        "name": "Theirs",
                        "description": "",
                        "archived": False,
                        "created": "2026-01-01",
                        "updated": "2026-01-01",
                        "source_count": 1,
                        "note_count": 0,
                    }
                ]
                response = client.get("/api/notebooks/notebook:shared")
        assert response.status_code == 200
        assert response.json()["role"] == "viewer"

    def test_an_owners_read_reports_owner(self, client):
        db = owned_db()
        with _both(db):
            with patch(
                "api.routers.notebooks.repo_query", new_callable=AsyncMock
            ) as route_query:
                route_query.return_value = [
                    {
                        "id": "notebook:mine",
                        "name": "Mine",
                        "description": "",
                        "archived": False,
                        "created": "2026-01-01",
                        "updated": "2026-01-01",
                        "source_count": 0,
                        "note_count": 0,
                    }
                ]
                response = client.get("/api/notebooks/notebook:mine")
        assert response.json()["role"] == "owner"

    def test_the_list_reports_a_role_per_notebook(self, client):
        with patch.object(
            access,
            "accessible_notebook_ids",
            AsyncMock(return_value=["notebook:mine", "notebook:theirs"]),
        ):
            with patch(
                "api.routers.notebooks.repo_query", new_callable=AsyncMock
            ) as route_query:
                route_query.return_value = [
                    {
                        "id": "notebook:mine",
                        "name": "Mine",
                        "owner": "member:testsuite",
                        "description": "",
                        "archived": False,
                        "created": "2026-01-01",
                        "updated": "2026-01-01",
                        "source_count": 0,
                        "note_count": 0,
                    },
                    {
                        "id": "notebook:theirs",
                        "name": "Theirs",
                        "owner": "member:someone-else",
                        "description": "",
                        "archived": False,
                        "created": "2026-01-01",
                        "updated": "2026-01-01",
                        "source_count": 0,
                        "note_count": 0,
                    },
                ]
                response = client.get("/api/notebooks")
        assert response.status_code == 200
        assert {nb["id"]: nb["role"] for nb in response.json()} == {
            "notebook:mine": "owner",
            "notebook:theirs": "viewer",
        }

    def test_the_list_drops_a_notebook_nobody_can_reach(self, client):
        """An unowned notebook is reachable by nobody, so `notebook_access` 404s
        it. Listing it with an invented role would be a claim the UI acts on."""
        with patch.object(
            access,
            "accessible_notebook_ids",
            AsyncMock(return_value=["notebook:orphan"]),
        ):
            with patch(
                "api.routers.notebooks.repo_query", new_callable=AsyncMock
            ) as route_query:
                route_query.return_value = [
                    {
                        "id": "notebook:orphan",
                        "name": "Unowned",
                        "owner": None,
                        "description": "",
                        "archived": False,
                        "created": "2026-01-01",
                        "updated": "2026-01-01",
                        "source_count": 0,
                        "note_count": 0,
                    }
                ]
                response = client.get("/api/notebooks")
        assert response.json() == []

    def test_the_response_does_not_disclose_the_owner(self):
        """"What may I do here" is the only question a client needs answered; who
        owns a notebook is not the caller's business."""
        assert "owner" not in NotebookResponse.model_fields

    def test_role_from_owner_is_fail_closed(self):
        assert role_from_owner(CALLER, None) is None, (
            "an unowned notebook must resolve to no role, never to viewer"
        )
        assert role_from_owner(Member(provider="t", subject="s"), "member:x") is None
        assert role_from_owner(CALLER, "member:testsuite") is Role.OWNER
        assert role_from_owner(CALLER, "member:other") is Role.VIEWER

    def test_granted_role_refuses_rather_than_inventing_one(self):
        from open_notebook.exceptions import NotFoundError

        with pytest.raises(NotFoundError):
            _ = access.NotebookAccess(notebook_id="notebook:x", role=None).granted_role
        assert (
            access.NotebookAccess(
                notebook_id="notebook:x", role=Role.VIEWER
            ).granted_role
            is Role.VIEWER
        )


class TestTheRouterIsRegistered:
    """A router nothing includes is a file, not an endpoint."""

    @staticmethod
    def _share_routes():
        return [
            route
            for route in app.routes
            if "/shares" in str(getattr(route, "path", ""))
        ]

    def test_all_three_routes_are_mounted(self):
        mounted = {
            (str(getattr(route, "path", "")), method)
            for route in app.routes
            for method in getattr(route, "methods", set())
        }
        assert ("/api/notebooks/{notebook_id}/shares", "GET") in mounted
        assert ("/api/notebooks/{notebook_id}/shares", "POST") in mounted
        assert (
            "/api/notebooks/{notebook_id}/shares/{member_id}",
            "DELETE",
        ) in mounted

    def test_there_is_no_route_that_edits_a_share(self):
        """Revoking is the only edit. An update route would exist only to carry a
        role the schema refuses."""
        for route in self._share_routes():
            assert not getattr(route, "methods", set()) & {"PUT", "PATCH"}

    def test_every_share_route_resolves_a_member(self):
        for route in self._share_routes():
            names = {
                dep.call.__name__
                for dep in getattr(route, "dependant").dependencies
                if getattr(dep, "call", None) is not None
            }
            assert "current_member" in names, getattr(route, "path", "")
