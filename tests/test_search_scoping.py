"""Search is confined to what the caller may read. Spec task 6.4.

Requirements 7.3 (inaccessible material is excluded from all search results),
7.4 (existence is not disclosed) and 2.4 (no Grounded_Answer draws on another
Notebook's Sources).

Why this file is separate from tests/test_access_control.py: that file covers the
access *decision*, which is 6.2's work and is stable. This one covers where the
decision is applied on the search path, which is a different failure mode. An
access check that is never consulted still passes every test in the other file.

Opting out of the conftest bypass with `no_access_bypass` throughout, so the real
resolution runs rather than a stand-in that returns OWNER for everything.

Scope note: the parameterised sweep over the whole route table belongs to task
6.5 and is deliberately not started here.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from surrealdb import RecordID

from open_notebook.domain import access
from open_notebook.domain import notebook as notebook_module
from open_notebook.domain.member import Member
from open_notebook.exceptions import AccessUnavailableError

pytestmark = pytest.mark.no_access_bypass


OWNED = "notebook:owned"
STRANGERS = "notebook:strangers"


@pytest.fixture
def member():
    return Member(id="member:alice", provider="test", subject="alice")


@pytest.fixture
def idless_member():
    """A member that was never saved. It must reach nothing."""
    return Member(provider="test", subject="nobody")


@pytest.fixture
def client():
    from api.main import app

    return TestClient(app)


def _access_rows(owned_ids):
    """Stand in for access.py's two reads, returning `owned_ids` as the member's.

    Patches `access.repo_query` rather than the `require_*` layer, so the fail-
    closed branches inside `accessible_notebook_ids` are the ones under test.
    """

    async def repo_query(query, params=None):
        if "FROM notebook WHERE owner" in query:
            return [RecordID.parse(i) for i in owned_ids]
        if "FROM share WHERE in" in query:
            return []
        raise AssertionError(f"unexpected access query: {query}")

    return repo_query


# ---------------------------------------------------------------------------
# The filter is in the query, and it carries the member's notebooks
# ---------------------------------------------------------------------------


class TestScopeReachesTheQuery:
    @pytest.mark.asyncio
    async def test_text_search_binds_the_members_notebooks(self, member):
        captured = {}

        async def repo_query(query, params=None):
            captured["query"] = query
            captured["params"] = params
            return []

        with (
            patch.object(access, "repo_query", _access_rows([OWNED])),
            patch.object(notebook_module, "repo_query", repo_query),
        ):
            await notebook_module.text_search("photosynthesis", 10, member=member)

        assert "$notebooks" in captured["query"], (
            "the scoping parameter must appear in the SurrealQL, not be applied "
            "to the rows afterwards"
        )
        assert captured["params"]["notebooks"] == [RecordID.parse(OWNED)]

    @pytest.mark.asyncio
    async def test_vector_search_binds_the_members_notebooks(self, member):
        captured = {}

        async def repo_query(query, params=None):
            captured["query"] = query
            captured["params"] = params
            return []

        with (
            patch.object(access, "repo_query", _access_rows([OWNED])),
            patch.object(notebook_module, "repo_query", repo_query),
            patch(
                "open_notebook.utils.embedding.generate_embedding",
                new=AsyncMock(return_value=[0.1, 0.2]),
            ),
        ):
            await notebook_module.vector_search("photosynthesis", 10, member=member)

        assert "$notebooks" in captured["query"]
        assert captured["params"]["notebooks"] == [RecordID.parse(OWNED)]

    @pytest.mark.asyncio
    async def test_notebook_ids_are_bound_as_record_ids_not_strings(self, member):
        """Task 6.3's defect: `IN $x` with string elements matches nothing, silently.

        A scoping filter that matches nothing looks exactly like a working one, so
        this asserts the type rather than only the effect.
        """
        captured = {}

        async def repo_query(query, params=None):
            captured["params"] = params
            return []

        with (
            patch.object(access, "repo_query", _access_rows([OWNED, STRANGERS])),
            patch.object(notebook_module, "repo_query", repo_query),
        ):
            await notebook_module.text_search("x", 10, member=member)

        bound = captured["params"]["notebooks"]
        assert bound, "nothing was bound at all"
        assert all(isinstance(n, RecordID) for n in bound), (
            f"bound as {[type(n).__name__ for n in bound]}; string elements match "
            "nothing in SurrealDB and would read as an empty search"
        )

    @pytest.mark.asyncio
    async def test_a_member_with_no_id_scopes_to_an_empty_set(self, idless_member):
        """Not None, and not everything: an empty list matches nothing."""
        captured = {}

        async def repo_query(query, params=None):
            captured["params"] = params
            return []

        with patch.object(notebook_module, "repo_query", repo_query):
            await notebook_module.text_search("x", 10, member=idless_member)

        assert captured["params"]["notebooks"] == []


# ---------------------------------------------------------------------------
# The member is required, not optional
# ---------------------------------------------------------------------------


class TestMemberIsRequired:
    @pytest.mark.asyncio
    async def test_text_search_without_a_member_is_a_type_error(self):
        with pytest.raises(TypeError):
            await notebook_module.text_search("x", 10)  # type: ignore[call-arg]

    @pytest.mark.asyncio
    async def test_vector_search_without_a_member_is_a_type_error(self):
        with pytest.raises(TypeError):
            await notebook_module.vector_search("x", 10)  # type: ignore[call-arg]

    def test_member_is_keyword_only_so_it_cannot_be_passed_by_accident(self):
        import inspect

        for fn in (notebook_module.text_search, notebook_module.vector_search):
            param = inspect.signature(fn).parameters["member"]
            assert param.kind is inspect.Parameter.KEYWORD_ONLY
            assert param.default is inspect.Parameter.empty, (
                "a default would let a caller search unscoped by omission, and an "
                "unscoped search returns more results rather than failing"
            )


# ---------------------------------------------------------------------------
# A failed access read is 503, never an empty result set
# ---------------------------------------------------------------------------


class TestFailedAccessReadIsNotAnEmptySearch:
    """The rule matters more here than anywhere else in the API.

    An empty result list is exactly what "no matches" looks like, so an access
    read answered as "you can see nothing" is invisible. access.py raises; these
    assert nothing on the search path converts that back into a result set - or
    into a DatabaseOperationError, which the router answers 500 instead of 503.
    """

    @staticmethod
    def _failing_access():
        async def boom(*_args, **_kwargs):
            raise RuntimeError("database is unreachable")

        return boom

    @pytest.mark.asyncio
    async def test_text_search_raises_access_unavailable(self, member):
        with (
            patch.object(access, "repo_query", self._failing_access()),
            patch.object(notebook_module, "repo_query", AsyncMock(return_value=[])),
        ):
            with pytest.raises(AccessUnavailableError):
                await notebook_module.text_search("x", 10, member=member)

    @pytest.mark.asyncio
    async def test_vector_search_raises_access_unavailable(self, member):
        with (
            patch.object(access, "repo_query", self._failing_access()),
            patch.object(notebook_module, "repo_query", AsyncMock(return_value=[])),
            patch(
                "open_notebook.utils.embedding.generate_embedding",
                new=AsyncMock(return_value=[0.1]),
            ),
        ):
            with pytest.raises(AccessUnavailableError):
                await notebook_module.vector_search("x", 10, member=member)

    @pytest.mark.asyncio
    async def test_the_search_query_never_runs_when_access_is_unknown(self, member):
        """Resolved before the try block, so nothing is read on this member's behalf."""
        search = AsyncMock(return_value=[])
        with (
            patch.object(access, "repo_query", self._failing_access()),
            patch.object(notebook_module, "repo_query", search),
        ):
            with pytest.raises(AccessUnavailableError):
                await notebook_module.text_search("x", 10, member=member)
        search.assert_not_awaited()

    def test_access_unavailable_is_not_a_database_operation_error(self):
        """Otherwise the router's `except DatabaseOperationError` answers 500."""
        from open_notebook.exceptions import DatabaseOperationError

        assert not issubclass(AccessUnavailableError, DatabaseOperationError)

    def test_search_route_answers_503_not_an_empty_result(self, client):
        with patch(
            "api.routers.search.text_search",
            new=AsyncMock(side_effect=AccessUnavailableError("db down")),
        ):
            response = client.post(
                "/api/search", json={"query": "x", "type": "text"}
            )
        assert response.status_code == 503, (
            f"got {response.status_code} {response.text}; a 200 with an empty list "
            "would be indistinguishable from a search that matched nothing"
        )


# ---------------------------------------------------------------------------
# The routes pass the member down
# ---------------------------------------------------------------------------


class TestSearchRoutesResolveTheCaller:
    @pytest.mark.parametrize("path", ["/api/search", "/api/search/ask", "/api/search/ask/simple"])
    def test_every_search_route_depends_on_current_member(self, path):
        import inspect

        from fastapi.routing import APIRoute

        from api.main import app

        routes = [
            r for r in app.routes if isinstance(r, APIRoute) and r.path == path
        ]
        assert routes, f"{path} is not registered"
        for route in routes:
            assert "current_member" in inspect.getsource(route.endpoint)

    def test_text_search_route_forwards_the_member(self, client):
        mock = AsyncMock(return_value=[])
        with patch("api.routers.search.text_search", new=mock):
            client.post("/api/search", json={"query": "x", "type": "text"})
        assert mock.await_args is not None, "text_search was never called"
        assert isinstance(mock.await_args.kwargs.get("member"), Member)

    def test_vector_search_route_forwards_the_member(self, client):
        mock = AsyncMock(return_value=[])
        with (
            patch("api.routers.search.vector_search", new=mock),
            patch(
                "api.routers.search.model_manager.get_embedding_model",
                new=AsyncMock(return_value=object()),
            ),
        ):
            client.post("/api/search", json={"query": "x", "type": "vector"})
        assert mock.await_args is not None, "vector_search was never called"
        assert isinstance(mock.await_args.kwargs.get("member"), Member)


# ---------------------------------------------------------------------------
# The ask graph (Requirement 2.4)
# ---------------------------------------------------------------------------


def _sub_state():
    """A complete SubGraphState, so a missing key cannot be what a test proves."""
    from open_notebook.graphs.ask import SubGraphState

    return SubGraphState(
        question="q", term="t", instructions="i", results={}, answer="", ids=[]
    )


class TestAskGraphCarriesTheMember:
    def test_the_graph_refuses_to_run_without_a_member(self):
        from open_notebook.exceptions import OpenNotebookError
        from open_notebook.graphs.ask import _asking_member

        configs: list[dict] = [
            {},
            {"configurable": {}},
            {"configurable": {"member": None}},
            {"configurable": {"member": "member:alice"}},
        ]
        for config in configs:
            with pytest.raises(OpenNotebookError):
                _asking_member(config)  # type: ignore[arg-type]

    def test_a_member_with_no_id_is_refused(self, idless_member):
        from open_notebook.exceptions import OpenNotebookError
        from open_notebook.graphs.ask import _asking_member

        with pytest.raises(OpenNotebookError):
            _asking_member({"configurable": {"member": idless_member}})  # type: ignore[arg-type]

    def test_a_saved_member_is_accepted(self, member):
        from open_notebook.graphs.ask import _asking_member

        assert _asking_member({"configurable": {"member": member}}) is member  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_provide_answer_passes_the_member_to_vector_search(self, member):
        from open_notebook.graphs import ask as ask_module

        mock = AsyncMock(return_value=[])
        with patch.object(ask_module, "vector_search", new=mock):
            result = await ask_module.provide_answer(
                _sub_state(),
                {"configurable": {"member": member}},  # type: ignore[arg-type]
            )

        assert result == {"answers": []}
        assert mock.await_args is not None, "vector_search was never called"
        assert mock.await_args.kwargs.get("member") is member

    @pytest.mark.asyncio
    async def test_provide_answer_does_not_search_at_all_without_a_member(self):
        from open_notebook.exceptions import OpenNotebookError
        from open_notebook.graphs import ask as ask_module

        mock = AsyncMock(return_value=[])
        with patch.object(ask_module, "vector_search", new=mock):
            with pytest.raises(OpenNotebookError):
                await ask_module.provide_answer(
                    _sub_state(),
                    {"configurable": {}},  # type: ignore[arg-type]
                )
        mock.assert_not_awaited()

    def test_both_ask_routes_put_the_member_in_the_graph_config(self):
        """The nodes read `configurable`, so the routes must populate it."""
        import inspect

        from api.routers import search as search_router

        for fn in (
            search_router.stream_ask_response,
            search_router.ask_knowledge_base_simple,
        ):
            source = inspect.getsource(fn)
            assert "member=member" in source, (
                f"{fn.__name__} builds a graph config without the member"
            )

    def test_the_ask_graph_needs_no_event_loop_workaround(self):
        """Every node is async, so chat.py's ThreadPool workaround does not belong.

        Asserted rather than assumed because the task planning for 6.4 expected
        the opposite, and copying that workaround in would add a mechanism the
        backend rules call fragile to a path that never needs it.

        Checked over the parsed syntax tree rather than the file's text: the
        module's own prose explains why the workaround is absent, and a substring
        scan would match the explanation.
        """
        import ast
        import inspect

        from open_notebook.graphs import ask as ask_module

        for name in (
            "call_model_with_messages",
            "provide_answer",
            "write_final_answer",
            "trigger_queries",
        ):
            node = getattr(ask_module, name)
            assert inspect.iscoroutinefunction(node), f"{name} is not async"

        tree = ast.parse(inspect.getsource(ask_module))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        } | {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "asyncio" not in imported
        assert not any("concurrent" in name for name in imported if name)

        called = {
            ast.unparse(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        assert not any("new_event_loop" in name for name in called)
        assert not any("ThreadPoolExecutor" in name for name in called)


# ---------------------------------------------------------------------------
# The migration keeps the filter where the LIMIT is
# ---------------------------------------------------------------------------


class TestMigrationKeepsSearchScoped:
    """The guard against an upstream merge silently unscoping search.

    These functions have been redefined by upstream migrations 1, 3, 4 and 9. If a
    future upstream migration redefines them again, the last definition in the
    chain stops carrying the filter and this fails - which is what Requirement
    14.4 asks for. The application would also fail loudly on the arity, but a red
    test at merge time is cheaper than a red deployment.
    """

    @staticmethod
    def _last_definition(function_name):
        """The body of the last definition of a function across the whole chain.

        Bounded at the next REMOVE/DEFINE FUNCTION, because one migration defines
        both search functions and an unbounded slice would carry the other one's
        body into this one's assertions.
        """
        from open_notebook.database.async_migrate import AsyncMigrationManager

        manager = AsyncMigrationManager()
        marker = f"DEFINE FUNCTION IF NOT EXISTS fn::{function_name}("
        latest = None
        for index, migration in enumerate(manager.up_migrations, start=1):
            if marker not in migration.sql:
                continue
            body = migration.sql.split(marker)[-1]
            for terminator in ("REMOVE FUNCTION", "DEFINE FUNCTION"):
                body = body.split(terminator)[0]
            latest = (index, body)
        assert latest is not None, f"fn::{function_name} is never defined"
        return latest

    def test_text_search_is_defined_with_a_notebooks_parameter(self):
        version, body = self._last_definition("text_search")
        signature = body.split(")")[0]
        assert "$notebooks" in signature, (
            f"the last fn::text_search definition is in migration {version} and "
            f"takes no $notebooks: {signature}"
        )

    def test_vector_search_is_defined_with_a_notebooks_parameter(self):
        version, body = self._last_definition("vector_search")
        signature = body.split(")")[0]
        assert "$notebooks" in signature, (
            f"the last fn::vector_search definition is in migration {version} and "
            f"takes no $notebooks: {signature}"
        )

    # How many branches of each function read member content. Stated so that a
    # branch appearing or disappearing in an upstream merge fails here and gets
    # read, rather than being scoped or unscoped by accident.
    CONTENT_BRANCHES = {"text_search": 6, "vector_search": 3}

    @staticmethod
    def _content_branches(body):
        """Each `let $x = ... FROM <content table> ...` branch of the function.

        The body arrives as one line (AsyncMigration.from_file joins it), so the
        `let $` bindings are the branch boundaries. The two accessible-set lookups
        are excluded: they read the relations, not the content.
        """
        tables = ("source_embedding", "source_insight", "source", "note")
        branches = []
        for fragment in body.split("let $"):
            if "FROM reference" in fragment or "FROM artifact" in fragment:
                continue
            if any(f"FROM {table}" in fragment for table in tables):
                branches.append(fragment)
        return branches

    @pytest.mark.parametrize("function_name", ["text_search", "vector_search"])
    def test_the_accessible_sets_are_constrained_by_the_bound_notebooks(
        self, function_name
    ):
        """The severest mutation, and the one the per-branch check misses.

        Every branch can carry `IN $accessible_sources` and the whole filter still
        be inert, if the two lookups that build those sets forget their own WHERE:
        they would then collect every source and note in the instance. The branches
        would look scoped and the scope would be everything.
        """
        _, body = self._last_definition(function_name)

        for relation, variable in (
            ("reference", "$accessible_sources"),
            ("artifact", "$accessible_notes"),
        ):
            assert f"FROM {relation} WHERE out IN $notebooks" in body, (
                f"{variable} is built from {relation} without constraining it to "
                "$notebooks, so it holds every member's content"
            )

    @pytest.mark.parametrize("function_name", ["text_search", "vector_search"])
    def test_every_branch_of_the_function_is_scoped(self, function_name):
        """Every branch, not most of them.

        A branch left unscoped leaks exactly one content type - other members'
        notes but not their sources, say - which is the easiest kind of leak to
        miss by eye and the easiest for a merge to reintroduce. An earlier version
        of this test counted conjuncts in aggregate and did not notice one branch
        losing its filter; this checks each branch on its own.
        """
        _, body = self._last_definition(function_name)

        reads = body.count("FROM reference") + body.count("FROM artifact")
        assert reads == 2, f"expected two accessible-set lookups, found {reads}"

        branches = self._content_branches(body)

        assert len(branches) == self.CONTENT_BRANCHES[function_name], (
            f"fn::{function_name} now has {len(branches)} content branches, not "
            f"{self.CONTENT_BRANCHES[function_name]}. If that is intended, check "
            "the new one is scoped and update this count."
        )

        for fragment in branches:
            binding = fragment.split("=")[0].strip()
            assert (
                "IN $accessible_sources" in fragment
                or "IN $accessible_notes" in fragment
            ), (
                f"branch ${binding} of fn::{function_name} reads content without a "
                "scoping conjunct, so it returns every member's rows of that type"
            )

    def test_the_scoping_filter_is_inside_the_function_not_around_the_call(self):
        """Where the filter sits is the whole decision. Measured, not assumed.

        Both functions LIMIT their ranked set, and fn::vector_search limits each
        branch as well, so a filter applied to the returned rows narrows a top-N
        chosen across every member's content. On a real database a member asking
        for 3 results got 1 of her own 3 matches back when the filter sat outside.
        """
        import inspect

        source = inspect.getsource(notebook_module.text_search)
        assert "$notebooks" in source
        for leak in ("for r in search_results", "if r[", "filter("):
            assert leak not in source, (
                "results look filtered in Python; the count and the ordering would "
                "still be computed over every member's content"
            )

    def test_the_down_migration_restores_upstreams_own_definitions(self):
        """Down must match what version 25 shipped, not a half-scoped hybrid.

        A partially scoped down path would be the worst of both: the application
        passes $notebooks and would still fail the arity check, but the stored
        function would no longer match any version of the schema.
        """
        from pathlib import Path

        down = Path("open_notebook/database/migrations/26_down.surrealql").read_text(
            encoding="utf-8"
        )
        sql = "\n".join(
            line
            for line in down.splitlines()
            if not line.strip().startswith("--")
        )
        assert "$notebooks" not in sql
        assert "$accessible_sources" not in sql
        for function_name in ("text_search", "vector_search"):
            assert f"DEFINE FUNCTION IF NOT EXISTS fn::{function_name}(" in sql

    def test_migration_26_is_registered_in_both_directions(self):
        from open_notebook.database.async_migrate import AsyncMigrationManager

        manager = AsyncMigrationManager()
        assert len(manager.up_migrations) == 26
        assert len(manager.down_migrations) == 26

    def test_no_migration_file_has_a_trailing_comment_on_a_code_line(self):
        """`AsyncMigration.from_file` joins every line into one (6.1's finding).

        A `--` comment after SQL on the same line would comment out every
        statement after it. Re-checked here because this task adds two files.
        """
        from pathlib import Path

        for path in Path("open_notebook/database/migrations").glob("*.surrealql"):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                stripped = line.strip()
                if stripped.startswith("--") or "--" not in stripped:
                    continue
                raise AssertionError(
                    f"{path.name}:{number} has a trailing comment: {stripped!r}"
                )


# ---------------------------------------------------------------------------
# Existence is not disclosed by a command job
# ---------------------------------------------------------------------------


class TestCommandJobDoesNotDiscloseContent:
    """The gap 6.2 named and left open, closed without a migration.

    A job's `result` can be insight text, so the route answers only for a caller
    who could read the content the job names.
    """

    def test_a_job_about_someone_elses_source_is_404(self, client):
        from open_notebook.exceptions import NotFoundError

        with (
            patch(
                "api.routers.commands.CommandService.content_for_job",
                new=AsyncMock(return_value=("source", "source:theirs")),
            ),
            patch(
                "api.routers.commands.require_source_read",
                new=AsyncMock(side_effect=NotFoundError("Source not found")),
            ),
            patch(
                "api.routers.commands.CommandService.get_command_status",
                new=AsyncMock(return_value={"job_id": "x", "status": "completed"}),
            ) as status,
        ):
            response = client.get("/api/commands/jobs/abc")

        assert response.status_code == 404
        assert status.await_count == 0, (
            "the job was read before access was decided"
        )

    def test_the_refusal_does_not_name_what_it_refused(self, client):
        """One message for every refusal, so it is not an oracle."""
        from open_notebook.exceptions import NotFoundError

        messages = set()
        for content, effect in (
            (None, None),
            (("source", "source:theirs"), NotFoundError("Source not found")),
            (("note", "note:theirs"), NotFoundError("Note not found")),
        ):
            patches = [
                patch(
                    "api.routers.commands.CommandService.content_for_job",
                    new=AsyncMock(return_value=content),
                )
            ]
            if effect is not None:
                for name in ("require_source_read", "require_note_read"):
                    patches.append(
                        patch(
                            f"api.routers.commands.{name}",
                            new=AsyncMock(side_effect=effect),
                        )
                    )
            with patches[0]:
                for extra in patches[1:]:
                    extra.start()
                try:
                    response = client.get("/api/commands/jobs/abc")
                finally:
                    for extra in patches[1:]:
                        extra.stop()
            assert response.status_code == 404
            messages.add(response.json().get("detail"))

        assert len(messages) == 1, f"refusals are distinguishable: {messages}"

    def test_a_job_naming_no_content_is_refused_rather_than_allowed(self, client):
        with patch(
            "api.routers.commands.CommandService.content_for_job",
            new=AsyncMock(return_value=None),
        ):
            response = client.get("/api/commands/jobs/abc")
        assert response.status_code == 404

    def test_a_readable_job_is_returned(self, client):
        with (
            patch(
                "api.routers.commands.CommandService.content_for_job",
                new=AsyncMock(return_value=("source", "source:mine")),
            ),
            patch(
                "api.routers.commands.require_source_read",
                new=AsyncMock(return_value=["notebook:mine"]),
            ),
            patch(
                "api.routers.commands.CommandService.get_command_status",
                new=AsyncMock(
                    return_value={"job_id": "abc", "status": "completed", "result": {}}
                ),
            ),
        ):
            response = client.get("/api/commands/jobs/abc")
        assert response.status_code == 200
        assert response.json()["status"] == "completed"

    @pytest.mark.asyncio
    async def test_content_for_job_reads_the_argument_that_names_content(self):
        from api.command_service import CommandService

        for key, kind in (
            ("source_id", "source"),
            ("note_id", "note"),
            ("notebook_id", "notebook"),
        ):
            with patch(
                "api.command_service.repo_query",
                new=AsyncMock(return_value=[{"args": {key: f"{kind}:abc"}}]),
            ):
                assert await CommandService.content_for_job("command:j") == (
                    kind,
                    f"{kind}:abc",
                )

    @pytest.mark.asyncio
    async def test_content_for_job_falls_back_to_the_source_link(self):
        """process_source is submitted before its Source exists."""
        from api.command_service import CommandService

        calls = []

        async def repo_query(query, params=None):
            calls.append(query)
            if "SELECT args" in query:
                return [{"args": {"url": "https://example.com"}}]
            return [RecordID.parse("source:created")]

        with patch("api.command_service.repo_query", new=repo_query):
            assert await CommandService.content_for_job("command:j") == (
                "source",
                "source:created",
            )
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_content_for_job_raises_rather_than_answering_none_on_failure(self):
        from api.command_service import CommandService

        async def boom(*_args, **_kwargs):
            raise RuntimeError("database is unreachable")

        with patch("api.command_service.repo_query", new=boom):
            with pytest.raises(AccessUnavailableError):
                await CommandService.content_for_job("command:j")

    def test_the_route_resolves_a_member(self):
        import inspect

        from api.routers import commands as commands_router

        assert "current_member" in inspect.getsource(
            commands_router.get_command_job_status
        )
