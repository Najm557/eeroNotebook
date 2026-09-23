"""Ownership and Share enforcement: `open_notebook.domain.access` and the routes.

Requirements 7.1, 5.2, 5.4, 5.5. Spec task 6.2.

What is worth testing here is the decisions, not the plumbing. The access module
is small and its whole value is that it refuses things, so these tests are written
around the five ways it could be wrong in a way nobody would notice:

- a missing owner read as "unrestricted" rather than "nobody" (migration 25's
  named risk, and the reason `owner` had to be optional)
- a member with no id matching an unowned notebook, because `None == None`
- a failed database read answered as "no access" instead of as an outage, which
  turns a broken check into a passed one
- a refusal that confirms the record exists to somebody with no access
- write access granted where only read was intended

The route-level checks at the end are deliberately few. They exist to prove the
module is actually *wired in* - an access module nothing calls is worse than none,
because it reads as protection. The exhaustive per-route table is task 6.5's.

These tests opt out of conftest's access bypass with `no_access_bypass`, so the
real decisions run.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from open_notebook.domain import access
from open_notebook.domain.access import NotebookAccess, Role
from open_notebook.domain.member import Member
from open_notebook.exceptions import (
    AccessDeniedError,
    AccessUnavailableError,
    NotFoundError,
)

pytestmark = pytest.mark.no_access_bypass

OWNER = Member(id="member:owner", provider="test", subject="owner")
VIEWER = Member(id="member:viewer", provider="test", subject="viewer")
STRANGER = Member(id="member:stranger", provider="test", subject="stranger")
# A member that was never persisted. Unreachable through the API, since
# Member.resolve saves before returning, but the comparison it forces - a None id
# against an unowned notebook's NONE - is the one that must not match.
UNSAVED = Member(provider="test", subject="nobody")


def fake_db(*, owner=None, exists=True, viewers=(), references=None, insight_source=None):
    """Stand in for SurrealDB, answering the access module's queries by shape.

    Keyed on the query text rather than on call order, so a test does not silently
    pass by feeding the right answer to the wrong question.
    """
    references = references or {}

    async def query(sql, params=None):
        params = params or {}
        if sql.startswith("SELECT id, owner FROM $notebook"):
            if not exists:
                return []
            return [{"id": str(params["notebook"]), "owner": owner}]
        if sql.startswith("SELECT VALUE role FROM share"):
            return ["viewer"] if str(params["member"]) in viewers else []
        if sql.startswith("SELECT VALUE id FROM notebook WHERE owner"):
            return references.get("owned", [])
        if sql.startswith("SELECT VALUE out FROM share WHERE in"):
            return references.get("shared", [])
        if "FROM reference WHERE in" in sql:
            return references.get("reference", [])
        if "FROM artifact WHERE in" in sql:
            return references.get("artifact", [])
        if "FROM refers_to WHERE in" in sql:
            return references.get("refers_to", [])
        if sql.startswith("SELECT VALUE source FROM $insight"):
            return [insight_source] if insight_source else []
        raise AssertionError(f"unexpected access query: {sql}")

    return AsyncMock(side_effect=query)


class TestFailClosedOnAMissingOwner:
    """`owner = NONE` means reachable by nobody. Migration 25 wrote its own tests
    to stop the opposite reading being introduced; these stop it being introduced
    one layer up."""

    @pytest.mark.asyncio
    async def test_an_unowned_notebook_is_reachable_by_nobody(self):
        with patch.object(access, "repo_query", fake_db(owner=None)):
            for member in (OWNER, VIEWER, STRANGER, UNSAVED):
                result = await access.notebook_access(member, "notebook:orphan")
                assert result.role is None, (
                    f"{member.subject} reached a notebook with no owner; a missing "
                    "owner means nobody, never everybody"
                )
                assert not result.can_read
                assert not result.can_write

    @pytest.mark.asyncio
    async def test_a_member_with_no_id_does_not_match_an_unowned_notebook(self):
        """Both sides of the comparison are absent, and they must not be equal."""
        with patch.object(access, "repo_query", fake_db(owner=None)):
            assert (await access.notebook_access(UNSAVED, "notebook:x")).role is None

    @pytest.mark.asyncio
    async def test_a_member_with_no_id_reaches_nothing_it_does_not_own(self):
        with patch.object(access, "repo_query", fake_db(owner="member:owner")):
            assert (await access.notebook_access(UNSAVED, "notebook:x")).role is None

    @pytest.mark.asyncio
    async def test_a_member_with_no_id_has_no_accessible_notebooks(self):
        db = fake_db(references={"owned": ["notebook:a"], "shared": ["notebook:b"]})
        with patch.object(access, "repo_query", db):
            assert await access.accessible_notebook_ids(UNSAVED) == []
            assert db.await_count == 0, (
                "an unsaved member should not be queried for at all; a query whose "
                "parameter is None is where `owner = NONE` starts matching rows"
            )

    @pytest.mark.asyncio
    async def test_a_share_on_an_unowned_notebook_grants_nothing(self):
        """A Share adds to an owner's access rather than replacing it. Otherwise a
        Share would be a way to make an unreachable notebook reachable, which is
        the fail-closed rule inverted."""
        db = fake_db(owner=None, viewers={"member:viewer"})
        with patch.object(access, "repo_query", db):
            assert (await access.notebook_access(VIEWER, "notebook:x")).role is None

    @pytest.mark.asyncio
    async def test_a_notebook_that_does_not_exist_grants_nothing(self):
        with patch.object(access, "repo_query", fake_db(exists=False)):
            assert (await access.notebook_access(OWNER, "notebook:gone")).role is None

    @pytest.mark.asyncio
    async def test_an_unparseable_id_grants_nothing_rather_than_raising(self):
        """A malformed id must not be distinguishable from a well-formed one that
        does not exist, and must not reach the database or 500.

        `notebook:a:b` is the shape that raises out of `RecordID.parse` - too many
        colons - rather than being escaped into a valid-looking record id.
        """
        db = fake_db()
        with patch.object(access, "repo_query", db):
            assert (await access.notebook_access(OWNER, "notebook:a:b")).role is None
            assert (await access.notebook_access(OWNER, "")).role is None
        assert db.await_count == 0

    @pytest.mark.asyncio
    async def test_an_unparseable_content_id_grants_nothing(self):
        db = fake_db()
        with patch.object(access, "repo_query", db):
            with pytest.raises(NotFoundError):
                await access.require_source_read(OWNER, "source:a:b")
            with pytest.raises(NotFoundError):
                await access.require_insight_read(OWNER, "source_insight:a:b")
        assert db.await_count == 0


class TestOwnerAndViewer:
    @pytest.mark.asyncio
    async def test_the_owner_reads_and_writes(self):
        with patch.object(access, "repo_query", fake_db(owner="member:owner")):
            result = await access.notebook_access(OWNER, "notebook:x")
        assert result.role is Role.OWNER
        assert result.can_read and result.can_write

    @pytest.mark.asyncio
    async def test_a_share_holder_reads_and_does_not_write(self):
        """Requirements 6.2, 6.3 and 6.4."""
        db = fake_db(owner="member:owner", viewers={"member:viewer"})
        with patch.object(access, "repo_query", db):
            result = await access.notebook_access(VIEWER, "notebook:x")
        assert result.role is Role.VIEWER
        assert result.can_read
        assert not result.can_write

    @pytest.mark.asyncio
    async def test_a_member_with_neither_reaches_nothing(self):
        db = fake_db(owner="member:owner", viewers={"member:viewer"})
        with patch.object(access, "repo_query", db):
            assert (await access.notebook_access(STRANGER, "notebook:x")).role is None

    @pytest.mark.asyncio
    async def test_the_owner_costs_one_query_and_does_not_consult_shares(self):
        db = fake_db(owner="member:owner")
        with patch.object(access, "repo_query", db):
            await access.notebook_access(OWNER, "notebook:x")
        assert db.await_count == 1

    @pytest.mark.asyncio
    async def test_there_is_no_role_beyond_owner_and_viewer(self):
        """Requirement 6.6 structurally: the schema asserts `role = 'viewer'`, and
        a row carrying anything else must not be honoured here either."""
        async def query(sql, params=None):
            if sql.startswith("SELECT id, owner FROM $notebook"):
                return [{"id": "notebook:x", "owner": "member:owner"}]
            return ["editor"]

        with patch.object(access, "repo_query", AsyncMock(side_effect=query)):
            assert (await access.notebook_access(VIEWER, "notebook:x")).role is None
        assert [r.value for r in Role] == ["owner", "viewer"]


class TestAFailedLookupIsNeverAnEmptyAnswer:
    """The rule task 5.3 established for the identity store, applied to access.

    "The database is unreachable" answered as "you have no access" is an outage
    that reads as a passed check, and nothing downstream can tell the difference.
    """

    @pytest.mark.asyncio
    async def test_a_failed_notebook_read_raises_rather_than_denying(self):
        db = AsyncMock(side_effect=RuntimeError("connection refused"))
        with patch.object(access, "repo_query", db):
            with pytest.raises(AccessUnavailableError):
                await access.notebook_access(OWNER, "notebook:x")

    @pytest.mark.asyncio
    async def test_a_failed_list_read_raises_rather_than_returning_nothing(self):
        db = AsyncMock(side_effect=RuntimeError("connection refused"))
        with patch.object(access, "repo_query", db):
            with pytest.raises(AccessUnavailableError):
                await access.accessible_notebook_ids(OWNER)

    @pytest.mark.asyncio
    async def test_a_failed_share_read_raises_rather_than_denying(self):
        """The second query specifically: the first succeeding and the second
        failing would otherwise silently demote an owner-or-Viewer to nobody."""
        calls = {"n": 0}

        async def query(sql, params=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return [{"id": "notebook:x", "owner": "member:owner"}]
            raise RuntimeError("connection lost")

        with patch.object(access, "repo_query", AsyncMock(side_effect=query)):
            with pytest.raises(AccessUnavailableError):
                await access.notebook_access(VIEWER, "notebook:x")

    @pytest.mark.asyncio
    async def test_an_outage_is_not_reported_as_a_denial(self):
        """AccessUnavailableError must not be a subclass of either refusal, or a
        caller catching "denied" would swallow the outage."""
        assert not issubclass(AccessUnavailableError, AccessDeniedError)
        assert not issubclass(AccessUnavailableError, NotFoundError)


class TestRefusalDoesNotDiscloseExistence:
    @pytest.mark.asyncio
    async def test_no_access_reads_as_not_found(self):
        """Requirement 7.4: the response must not confirm the notebook exists."""
        db = fake_db(owner="member:owner")
        with patch.object(access, "repo_query", db):
            with pytest.raises(NotFoundError):
                await access.require_notebook_read(STRANGER, "notebook:x")
            with pytest.raises(NotFoundError):
                await access.require_notebook_write(STRANGER, "notebook:x")

    @pytest.mark.asyncio
    async def test_a_viewers_write_is_refused_as_forbidden_not_as_missing(self):
        """A Viewer can already see it, so 404 would only read as a defect."""
        db = fake_db(owner="member:owner", viewers={"member:viewer"})
        with patch.object(access, "repo_query", db):
            await access.require_notebook_read(VIEWER, "notebook:x")
            with pytest.raises(AccessDeniedError):
                await access.require_notebook_write(VIEWER, "notebook:x")

    @pytest.mark.asyncio
    async def test_a_missing_notebook_and_an_inaccessible_one_answer_alike(self):
        missing = fake_db(exists=False)
        other = fake_db(owner="member:owner")
        with patch.object(access, "repo_query", missing):
            with pytest.raises(NotFoundError) as absent:
                await access.require_notebook_read(STRANGER, "notebook:gone")
        with patch.object(access, "repo_query", other):
            with pytest.raises(NotFoundError) as present:
                await access.require_notebook_read(STRANGER, "notebook:theirs")
        assert str(absent.value) == str(present.value)


class TestTheNotebookList:
    @pytest.mark.asyncio
    async def test_it_is_owned_plus_shared(self):
        """Requirement 5.5."""
        db = fake_db(
            references={"owned": ["notebook:a"], "shared": ["notebook:b"]}
        )
        with patch.object(access, "repo_query", db):
            assert await access.accessible_notebook_ids(OWNER) == [
                "notebook:a",
                "notebook:b",
            ]

    @pytest.mark.asyncio
    async def test_a_notebook_owned_and_shared_appears_once(self):
        db = fake_db(
            references={"owned": ["notebook:a"], "shared": ["notebook:a"]}
        )
        with patch.object(access, "repo_query", db):
            assert await access.accessible_notebook_ids(OWNER) == ["notebook:a"]

    @pytest.mark.asyncio
    async def test_a_member_with_nothing_gets_an_empty_list(self):
        with patch.object(access, "repo_query", fake_db()):
            assert await access.accessible_notebook_ids(OWNER) == []


class TestContentInheritsFromItsNotebook:
    """Requirement 5.3. Sources, notes and insights carry no owner of their own."""

    @pytest.mark.asyncio
    async def test_a_source_is_readable_through_its_notebook(self):
        db = fake_db(owner="member:owner", references={"reference": ["notebook:x"]})
        with patch.object(access, "repo_query", db):
            assert await access.require_source_read(OWNER, "source:1") == ["notebook:x"]

    @pytest.mark.asyncio
    async def test_a_source_in_an_inaccessible_notebook_is_not_found(self):
        db = fake_db(owner="member:owner", references={"reference": ["notebook:x"]})
        with patch.object(access, "repo_query", db):
            with pytest.raises(NotFoundError):
                await access.require_source_read(STRANGER, "source:1")

    @pytest.mark.asyncio
    async def test_an_orphaned_source_is_reachable_by_nobody(self):
        """The ongoing orphaning task 6.1 measured. A source in no notebook
        inherits from nothing, so no check can grant it - including the
        operator's. The routes that used to create this state now refuse to."""
        db = fake_db(owner="member:owner", references={"reference": []})
        with patch.object(access, "repo_query", db):
            for member in (OWNER, VIEWER, STRANGER):
                with pytest.raises(NotFoundError):
                    await access.require_source_read(member, "source:orphan")

    @pytest.mark.asyncio
    async def test_a_viewer_may_read_a_source_and_not_change_it(self):
        db = fake_db(
            owner="member:owner",
            viewers={"member:viewer"},
            references={"reference": ["notebook:x"]},
        )
        with patch.object(access, "repo_query", db):
            await access.require_source_read(VIEWER, "source:1")
            with pytest.raises(AccessDeniedError):
                await access.require_source_write(VIEWER, "source:1")

    @pytest.mark.asyncio
    async def test_a_note_inherits_through_the_artifact_relation(self):
        db = fake_db(owner="member:owner", references={"artifact": ["notebook:x"]})
        with patch.object(access, "repo_query", db):
            assert await access.require_note_read(OWNER, "note:1") == ["notebook:x"]

    @pytest.mark.asyncio
    async def test_an_insight_inherits_through_its_source(self):
        db = fake_db(
            owner="member:owner",
            references={"reference": ["notebook:x"]},
            insight_source="source:1",
        )
        with patch.object(access, "repo_query", db):
            assert await access.require_insight_read(OWNER, "source_insight:1") == [
                "notebook:x"
            ]

    @pytest.mark.asyncio
    async def test_an_insight_whose_source_is_gone_is_not_found(self):
        db = fake_db(insight_source=None)
        with patch.object(access, "repo_query", db):
            with pytest.raises(NotFoundError):
                await access.require_insight_read(OWNER, "source_insight:1")

    @pytest.mark.asyncio
    async def test_a_source_chat_session_resolves_through_its_source(self):
        db = fake_db(
            owner="member:owner",
            references={"refers_to": ["source:1"], "reference": ["notebook:x"]},
        )
        with patch.object(access, "repo_query", db):
            assert await access.require_chat_session_read(OWNER, "chat_session:1") == [
                "notebook:x"
            ]

    @pytest.mark.asyncio
    async def test_a_notebook_chat_session_resolves_directly(self):
        db = fake_db(owner="member:owner", references={"refers_to": ["notebook:x"]})
        with patch.object(access, "repo_query", db):
            assert await access.require_chat_session_read(OWNER, "chat_session:1") == [
                "notebook:x"
            ]

    @pytest.mark.asyncio
    async def test_an_id_without_its_table_prefix_still_resolves(self):
        """Several chat routes accept a bare id. An access check that raised on
        one would turn a working route into a 500, which is how a check gets
        deleted rather than fixed."""
        db = fake_db(owner="member:owner", references={"refers_to": ["notebook:x"]})
        with patch.object(access, "repo_query", db):
            assert await access.require_chat_session_read(OWNER, "abc123") == [
                "notebook:x"
            ]


class TestDeletingContentSharedAcrossNotebooks:
    """Deleting a source removes it from every notebook referencing it, so owning
    one of them is not permission to remove it from the others."""

    @pytest.mark.asyncio
    async def test_owning_one_of_two_notebooks_permits_an_edit(self):
        async def query(sql, params=None):
            if "FROM reference WHERE in" in sql:
                return ["notebook:mine", "notebook:theirs"]
            if sql.startswith("SELECT id, owner FROM $notebook"):
                owner = (
                    "member:owner"
                    if str(params["notebook"]) == "notebook:mine"
                    else "member:stranger"
                )
                return [{"id": str(params["notebook"]), "owner": owner}]
            return []

        with patch.object(access, "repo_query", AsyncMock(side_effect=query)):
            assert await access.require_source_write(OWNER, "source:1")

    @pytest.mark.asyncio
    async def test_owning_one_of_two_notebooks_does_not_permit_a_delete(self):
        async def query(sql, params=None):
            if "FROM reference WHERE in" in sql:
                return ["notebook:mine", "notebook:theirs"]
            if sql.startswith("SELECT id, owner FROM $notebook"):
                owner = (
                    "member:owner"
                    if str(params["notebook"]) == "notebook:mine"
                    else "member:stranger"
                )
                return [{"id": str(params["notebook"]), "owner": owner}]
            return []

        with patch.object(access, "repo_query", AsyncMock(side_effect=query)):
            with pytest.raises(AccessDeniedError):
                await access.require_source_write(
                    OWNER, "source:1", every_notebook=True
                )


class TestTheRoutesActuallyCallIt:
    """An access module nothing calls is worse than none: it reads as protection.

    A handful of routes rather than all of them - the exhaustive table is task
    6.5's - chosen to cover one read, one write, one list and the two paths that
    used to create unreachable content.
    """

    @pytest.fixture
    def client(self):
        from api.main import app

        return TestClient(app)

    def test_reading_someone_elses_notebook_is_404(self, client):
        db = fake_db(owner="member:someone-else")
        with patch.object(access, "repo_query", db):
            response = client.get("/api/notebooks/notebook:theirs")
        assert response.status_code == 404

    def test_a_viewers_write_is_403(self, client):
        db = fake_db(owner="member:someone-else", viewers={"member:testsuite"})
        with patch.object(access, "repo_query", db):
            response = client.put(
                "/api/notebooks/notebook:shared", json={"name": "renamed"}
            )
        assert response.status_code == 403

    def test_an_access_outage_is_503_and_not_an_empty_list(self, client):
        db = AsyncMock(side_effect=RuntimeError("connection refused"))
        with patch.object(access, "repo_query", db):
            response = client.get("/api/notebooks")
        assert response.status_code == 503, (
            "an unreachable database answered as 200 with an empty list is an "
            "outage that reads exactly like owning nothing"
        )

    def test_the_notebook_list_is_scoped_in_the_query(self, client):
        """Not filtered after the fact: the next projection or count added would
        otherwise be computed over material the caller cannot see."""
        with patch.object(
            access,
            "accessible_notebook_ids",
            AsyncMock(return_value=["notebook:mine"]),
        ):
            with patch(
                "api.routers.notebooks.repo_query", new_callable=AsyncMock
            ) as route_query:
                route_query.return_value = []
                response = client.get("/api/notebooks")
        assert response.status_code == 200
        assert route_query.await_args is not None
        sql, params = route_query.await_args.args
        assert "WHERE id IN $accessible_notebooks" in sql
        assert [str(nb) for nb in params["accessible_notebooks"]] == ["notebook:mine"]

    def test_a_created_notebook_is_owned_by_its_creator(self, client):
        """Requirement 5.1, and the owner comes from the resolved caller rather
        than from anything the client sent."""
        saved = {}

        class FakeNotebook:
            def __init__(self, **kwargs):
                saved.update(kwargs)
                self.id = "notebook:new"
                self.name = kwargs["name"]
                self.description = kwargs["description"]
                self.archived = False
                self.created = "2026-01-01T00:00:00Z"
                self.updated = "2026-01-01T00:00:00Z"

            async def save(self):
                return None

        with patch("api.routers.notebooks.Notebook", FakeNotebook):
            response = client.post(
                "/api/notebooks",
                json={"name": "Mine", "description": "", "owner": "member:someone"},
            )
        assert response.status_code == 200
        assert saved["owner"] == "member:testsuite"

    def test_a_note_cannot_be_created_outside_a_notebook(self, client):
        """One of the two paths that kept producing content nobody could reach."""
        response = client.post("/api/notes", json={"content": "text"})
        assert response.status_code == 400
        assert "notebook_id" in response.json()["detail"]

    def test_a_source_cannot_be_created_outside_a_notebook(self, client):
        response = client.post(
            "/api/sources/json", json={"type": "text", "content": "text"}
        )
        assert response.status_code == 400
        assert "notebook" in response.json()["detail"].lower()

    def test_deleting_a_notebook_refuses_to_strand_its_exclusive_sources(
        self, client
    ):
        """The decision task 6.1 handed to 6.2.

        Deleting a notebook without `delete_exclusive_sources` unlinks its sources
        and keeps them, leaving any source referenced only by this notebook in no
        notebook at all - unreadable and undeletable by every member, the operator
        included. Refused rather than silently deleted, because destroying content
        somebody asked to keep is the worse failure of the two.
        """
        notebook = AsyncMock()
        notebook.id = "notebook:mine"
        notebook.get_delete_preview.return_value = {
            "note_count": 0,
            "exclusive_source_count": 3,
            "shared_source_count": 1,
        }
        db = fake_db(owner="member:testsuite")
        with patch.object(access, "repo_query", db):
            with patch(
                "api.routers.notebooks.Notebook.get",
                new=AsyncMock(return_value=notebook),
            ):
                response = client.delete("/api/notebooks/notebook:mine")
        assert response.status_code == 400
        assert "3 source(s)" in response.json()["detail"]
        notebook.delete.assert_not_awaited()

    def test_deleting_a_notebook_with_only_shared_sources_still_works(self, client):
        """The refusal is narrow: nothing is stranded when every source is also
        referenced elsewhere, so that delete proceeds untouched."""
        notebook = AsyncMock()
        notebook.id = "notebook:mine"
        notebook.get_delete_preview.return_value = {
            "note_count": 2,
            "exclusive_source_count": 0,
            "shared_source_count": 4,
        }
        notebook.delete.return_value = {
            "deleted_notes": 2,
            "deleted_sources": 0,
            "unlinked_sources": 4,
            "deleted_chat_sessions": 0,
        }
        db = fake_db(owner="member:testsuite")
        with patch.object(access, "repo_query", db):
            with patch(
                "api.routers.notebooks.Notebook.get",
                new=AsyncMock(return_value=notebook),
            ):
                response = client.delete("/api/notebooks/notebook:mine")
        assert response.status_code == 200
        assert response.json()["unlinked_sources"] == 4

    def test_unlinking_a_sources_last_notebook_is_refused(self, client):
        """The other way to strand a source, and the same answer."""
        db = fake_db(owner="member:testsuite")
        with patch.object(access, "repo_query", db):
            with patch(
                "api.routers.notebooks.Notebook.get", new=AsyncMock()
            ):
                with patch(
                    "api.routers.notebooks.repo_query", new_callable=AsyncMock
                ) as route_query:
                    route_query.return_value = []  # no other notebook holds it
                    response = client.delete(
                        "/api/notebooks/notebook:mine/sources/source:1"
                    )
        assert response.status_code == 400
        assert "reachable by nobody" in response.json()["detail"]

    def test_the_generic_command_endpoint_is_closed(self, client):
        """It submitted any registered command with any arguments, which reached
        every member's sources and notes around every check in this module."""
        response = client.post(
            "/api/commands/jobs",
            json={"command": "embed_source", "app": "open_notebook", "input": {}},
        )
        assert response.status_code == 403
        response = client.get("/api/commands/jobs")
        assert response.status_code == 403


class TestNotebookAccessValue:
    def test_no_role_means_neither_read_nor_write(self):
        result = NotebookAccess(notebook_id="notebook:x", role=None)
        assert not result.can_read
        assert not result.can_write

    def test_a_viewer_reads_only(self):
        result = NotebookAccess(notebook_id="notebook:x", role=Role.VIEWER)
        assert result.can_read
        assert not result.can_write

    def test_an_owner_reads_and_writes(self):
        result = NotebookAccess(notebook_id="notebook:x", role=Role.OWNER)
        assert result.can_read
        assert result.can_write
