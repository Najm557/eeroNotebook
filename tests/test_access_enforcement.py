"""Access enforcement, swept over every route the application registers.

Requirements 7.1 (enforced on every REST route), 7.3 (inaccessible material is
excluded from search), 7.4 (existence is not disclosed), 5.4 and 5.5 (a Notebook
is visible only to its owner and Share holders), 6.4 (a Viewer may not write) and
6.5 (a revoked Share ends access). Spec task 6.5.

**This file is infrastructure, not assurance.** Requirement 14.4 re-runs it after
every upstream merge, and the design names access enforcement the highest-
consequence divergence in the fork. So it is written to fail on a route that does
not exist yet: the route table is derived from `app.routes` at collection time and
every route in it must land in exactly one named category. A route added later -
by us or by an upstream merge - lands in the category that requires scoping, and
fails by name if it has none. A suite that listed today's routes by hand would
pass forever while the codebase drifted.

Three mechanisms, because no one of them is sufficient:

1. **The partition** (`TestTheRouteTable`). Every method/path pair is classified.
   The categories that are *not* required to scope are explicit allowlists with a
   reason attached to each entry, and each entry is checked to still exist and to
   still be unscoped - so removing a gap later is a one-line deletion and adding
   one is a visible diff.

2. **Static reach** (`TestEveryScopedRouteReachesTheAccessModule`). For each
   route that must scope, the call closure from its handler through the `api`
   layer must reach a scoping primitive. This is what catches a new route with no
   check at all, before anyone has written a behavioural test for it.

3. **Behaviour** (the sweeps below). Static reach cannot tell a check that runs
   from one that is called and ignored, so each route that must scope is actually
   driven: a stranger gets 404 and a body carrying nothing but `detail`, a Viewer's
   writes get 403, an unauthenticated caller gets 401, and an access outage gets
   503 rather than an empty list.

Relationship to the three suites that came before it, which this one does not
repeat:

- `tests/test_access_control.py` (6.2) covers the access *decision* - the module's
  fail-closed rules - and says its own route checks are deliberately few.
- `tests/test_shares.py` (6.3) covers Share management: granting, listing,
  revoking, and the enumeration boundary.
- `tests/test_search_scoping.py` (6.4) covers the search *functions* and the
  migration that scopes them. This file covers the search *routes*.

Opting out of conftest's access bypass with `no_access_bypass` throughout, or the
real decisions would not run and every sweep below would assert nothing. The
authentication bypass is left in place except in `TestAuthenticationIsRequired`,
which opts out of that one too.

**Nothing here may reach a real database.** `open_notebook.database.repository.
db_connection` is replaced with one that raises, so a handler that reads content
*before* deciding access fails loudly instead of quietly passing against whatever
happens to be in a developer's local SurrealDB.
"""

import ast
import contextlib
import inspect
import textwrap
import typing
from typing import (
    Any,
    Dict,
    List,
    Literal,
    NamedTuple,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
    get_args,
    get_origin,
)
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import params as fastapi_params
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel
from surrealdb import RecordID

from open_notebook.domain import access
from open_notebook.domain import notebook as notebook_module
from open_notebook.domain import share as share_module
from open_notebook.domain.member import Member

pytestmark = pytest.mark.no_access_bypass


# The caller, for every dynamic sweep. conftest's `authenticated_requests`
# resolves every request to this member, so what varies between sweeps is the
# *access* answer the stand-in database gives, never who is asking.
CALLER_ID = "member:testsuite"
SOMEBODY_ELSE = "member:someone-else"

THEIR_NOTEBOOK = "notebook:theirs"
THEIR_SOURCE = "source:theirs"
THEIR_NOTE = "note:theirs"
THEIR_INSIGHT = "source_insight:theirs"
THEIR_SESSION = "chat_session:theirs"


# ---------------------------------------------------------------------------
# Deriving the route table
# ---------------------------------------------------------------------------
#
# Mechanically, from the application object, at import time. The three tasks
# before this one each counted routes by hand and reported 44, 45, 48 and 51 in
# turn; none of those numbers is written down here, because a number carried by
# hand is a number that goes stale between the measurement and the merge.


class Route(NamedTuple):
    """One method/path pair, with everything the sweeps need to reason about it."""

    method: str
    path: str
    module: str
    handler: str
    endpoint: Any
    resolves_member: bool
    scoping_reach: Tuple[str, ...]

    @property
    def key(self) -> Tuple[str, str]:
        return (self.method, self.path)

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def is_write(self) -> bool:
        """By HTTP method, not by what the handler happens to do.

        `GET /api/notebooks/{id}/delete-preview` is a read by method and is gated
        on write on purpose (6.3), so method alone cannot decide what response to
        expect - `VIEWER_IS_REFUSED` records the exceptions.
        """
        return self.method in {"POST", "PUT", "PATCH", "DELETE"}


# What counts as scoping. Every name here is either a function in
# `open_notebook.domain.access` - the one module that decides what a member may
# reach - or an entry point that cannot be called unscoped.
ACCESS_PRIMITIVES: Tuple[str, ...] = (
    "require_notebook_read",
    "require_notebook_write",
    "require_notebooks_write",
    "require_source_read",
    "require_source_write",
    "require_note_read",
    "require_note_write",
    "require_insight_read",
    "require_insight_write",
    "require_chat_session_read",
    "require_chat_session_write",
    "accessible_notebook_ids",
    "accessible_notebook_records",
    "notebook_access",
    "role_from_owner",
)

# `text_search` and `vector_search` take `member` as a required keyword-only
# argument and bind the accessible Notebooks inside the SurrealQL (migration 26),
# so reaching either of them is reaching a scoped query. A caller that forgets the
# member gets a TypeError rather than an instance-wide search - asserted in
# tests/test_search_scoping.py, which is why naming them here is safe.
SEARCH_PRIMITIVES: Tuple[str, ...] = ("text_search", "vector_search")

# The ask graph refuses to run without a member (`_asking_member`) and every
# search inside it is scoped, so invoking it *with* a member in the config is
# scoping. The `member=` check is part of the primitive: without it the graph
# raises, which is fail-closed but is not a route that works.
GRAPH_PRIMITIVE = "ask_graph.astream"


class _Names(NamedTuple):
    referenced: Set[str]
    """Every name the function mentions, called or not."""

    called: Set[str]
    """Only the names it actually calls."""

    called_with_member: Set[str]
    """The subset of `called` that was handed a `member`."""


def _names_in(func: Any) -> _Names:
    """What a function mentions, parsed rather than matched as text.

    `referenced` is deliberately wider than `called`: a handler can hold the
    checks in a mapping and dispatch through it, which
    `commands.get_command_job_status` does -

        checks = {"source": require_source_read, ...}
        await checks[kind](member, record_id)

    - and a call-only scan reports that route as having no check at all. So a
    primitive is *detected* from any reference, while delegation is *followed*
    only through real calls, which keeps the closure from wandering.

    Text scanning was the alternative and is the trap task 6.4 hit: a docstring
    explaining why a mechanism is absent reads exactly like the mechanism being
    present.
    """
    try:
        source = inspect.getsource(func)
    except (OSError, TypeError):
        return _Names(set(), set(), set())
    tree = ast.parse(textwrap.dedent(source))

    referenced: Set[str] = set()
    called: Set[str] = set()
    with_member: Set[str] = set()

    def record(into: Set[str], rendered: str) -> None:
        into.add(rendered)
        into.add(rendered.rsplit(".", maxsplit=1)[-1])

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            record(referenced, ast.unparse(node))
        if not isinstance(node, ast.Call):
            continue
        rendered = ast.unparse(node.func)
        record(called, rendered)
        record(referenced, rendered)
        hands_over_a_member = any(
            keyword.arg == "member" or "member=" in ast.unparse(keyword)
            for keyword in node.keywords
        ) or any(ast.unparse(arg) == "member" for arg in node.args)
        if hands_over_a_member:
            record(with_member, rendered)
    return _Names(referenced, called, with_member)


def _scoping_reach(endpoint: Any, depth: int = 4) -> Tuple[str, ...]:
    """The scoping primitives reachable from this handler through the `api` layer.

    Bounded to `api.*` deliberately. Following calls into `open_notebook.*` would
    eventually reach the access module from almost anywhere and report every route
    as scoped; stopping at the api boundary means a router either checks access
    itself or delegates to another router/service function that does. The two
    primitives that live outside `api` - the search functions and the ask graph -
    are named rather than followed.
    """
    found: List[str] = []
    seen: Set[Any] = set()
    frontier = [(endpoint, 0)]

    while frontier:
        func, level = frontier.pop()
        if func in seen or level > depth:
            continue
        seen.add(func)

        names = _names_in(func)
        for primitive in ACCESS_PRIMITIVES + SEARCH_PRIMITIVES:
            if primitive in names.referenced:
                found.append(primitive)
        if GRAPH_PRIMITIVE in names.called_with_member:
            found.append(GRAPH_PRIMITIVE)

        module = inspect.getmodule(func)
        if module is None:
            continue
        for name in names.called:
            candidate = getattr(module, name.split(".")[0], None)
            if not inspect.isfunction(candidate):
                continue
            if not getattr(candidate, "__module__", "").startswith("api."):
                continue
            frontier.append((candidate, level + 1))

    return tuple(sorted(set(found)))


def _route_table() -> Tuple[Route, ...]:
    from api.main import app

    rows: List[Route] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        resolves_member = any(
            getattr(dependency.call, "__name__", "") == "current_member"
            for dependency in route.dependant.dependencies
        )
        reach = _scoping_reach(route.endpoint)
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            rows.append(
                Route(
                    method=method,
                    path=route.path,
                    module=route.endpoint.__module__.rsplit(".", maxsplit=1)[-1],
                    handler=route.endpoint.__name__,
                    endpoint=route.endpoint,
                    resolves_member=resolves_member,
                    scoping_reach=reach,
                )
            )
    return tuple(sorted(rows, key=lambda r: (r.path, r.method)))


ROUTES: Tuple[Route, ...] = _route_table()
BY_KEY: Dict[Tuple[str, str], Route] = {route.key: route for route in ROUTES}


# ---------------------------------------------------------------------------
# The categories that are not required to scope, each with its reason
# ---------------------------------------------------------------------------

# Routers whose every route configures the instance and carries no Notebook.
# Declared per module rather than per route: 56 hand-listed pairs would be 56
# things to keep in step with upstream, and the exact-count assertion in
# `TestTheRouteTable` is what stops a Notebook-touching route being added to one
# of them unnoticed.
#
# Task 6.2 flagged the standing consequence and it is unchanged here: any member
# can change instance configuration. That is real, and it is not Requirement 7.1,
# which is about Notebook ownership and Share access. It needs an operator role
# the design does not have, so it is a decision rather than a fix.
CONFIGURATION_ROUTERS: Dict[str, str] = {
    "auth": "sign-in, refresh, status, and the caller's own identity",
    "capabilities": "which optional content processors are installed",
    "config": "version and database status",
    "credentials": "provider credentials; never returns a key value",
    "episode_profiles": "podcast episode profiles (a v1 Non-Goal)",
    "languages": "the static language list",
    "models": "registered models and the instance defaults",
    "providers": "the static provider registry",
    "settings": "instance settings",
    "speaker_profiles": "podcast speaker profiles (a v1 Non-Goal)",
    "transformations": "transformation prompt templates; executes against text the caller supplied",
}
CONFIGURATION_ROUTE_COUNT = 56

# Unauthenticated by design, and excluded in MemberAuthMiddleware.
PUBLIC: Dict[Tuple[str, str], str] = {
    ("GET", "/"): "liveness banner, no data",
    ("GET", "/health"): "the health endpoint Requirement 13.6 asks for",
}

# Every path the authentication middleware lets through. The two above plus the
# four the frontend reads before anybody has signed in - upstream's list, kept
# verbatim. Recorded here so that a path *added* to it shows up as a diff on this
# file, and checked against the middleware rather than restated.
EXCLUDED_FROM_AUTHENTICATION: Dict[str, str] = {
    "/": PUBLIC[("GET", "/")],
    "/health": PUBLIC[("GET", "/health")],
    "/api/config": "version and database status, read by the sign-in page",
    "/api/auth/status": "tells the client how to sign in",
    "/api/auth/login": "exchanges a password for a token",
    "/api/auth/refresh": "exchanges a refresh token; carries its own credential",
}

# Closed rather than scoped: they answer 403 to everybody. A command record
# carries no Notebook, and every capability these reached has a scoped route of
# its own (task 6.2).
CLOSED_TO_EVERYBODY: Dict[Tuple[str, str], str] = {
    ("POST", "/api/commands/jobs"): "submitted any command with any arguments",
    ("GET", "/api/commands/jobs"): "listed every job with its args and result",
    ("DELETE", "/api/commands/jobs/{job_id}"): "cancelled any member's job",
}

# Ownership is assigned here rather than checked: there is no existing record to
# scope against. The owner comes from the resolved caller and never from the
# request body, which `TestCreationAssignsOwnershipRatherThanCheckingIt` asserts.
CREATES_THE_SCOPE: Dict[Tuple[str, str], str] = {
    ("POST", "/api/notebooks"): "creates the Notebook whose owner it sets",
}

# **The open gaps.** Each is recorded as a deliberate decision by the task that
# found it, so a sweep asserting "every route is scoped" would fail against them.
# Naming them here rather than widening the sweep keeps two properties: removing a
# gap later is a one-line deletion, and adding one is a visible diff on a file
# whose whole subject is access.
#
# `TestTheRouteTable` checks each of these still exists and still has no scoping
# reach, so an entry that has quietly been fixed fails and gets deleted.
KNOWN_GAPS: Dict[Tuple[str, str], str] = {
    ("GET", "/api/podcasts/episodes"): (
        "an episode's content is verbatim Source text, and `episode` is SCHEMAFULL "
        "with no Notebook and no owner. Needs a migration (a later one starts at "
        "29) plus a decision about who owns episodes predating it. Podcasts are a "
        "v1 Non-Goal and the deployment has generated zero episodes"
    ),
    ("GET", "/api/podcasts/episodes/{episode_id}"): "as above",
    ("GET", "/api/podcasts/episodes/{episode_id}/audio"): "as above",
    ("POST", "/api/podcasts/episodes/{episode_id}/retry"): "as above",
    ("DELETE", "/api/podcasts/episodes/{episode_id}"): "as above",
    ("GET", "/api/podcasts/jobs/{job_id}"): "as above; reports on an episode job",
    ("GET", "/api/commands/registry/debug"): (
        "returns registered command names and no content"
    ),
    ("POST", "/api/embeddings/rebuild"): (
        "instance-wide by design, discloses nothing, rewrites vectors "
        "idempotently. A resource-consumption route any member can trigger, not an "
        "access leak; confining it needs an operator role the design has not got"
    ),
    ("GET", "/api/embeddings/rebuild/{command_id}/status"): (
        "counts and progress for the rebuild above; names no content"
    ),
}


def _configuration_routes() -> Tuple[Route, ...]:
    return tuple(r for r in ROUTES if r.module in CONFIGURATION_ROUTERS)


def _allowlisted() -> Set[Tuple[str, str]]:
    return (
        {r.key for r in _configuration_routes()}
        | set(PUBLIC)
        | set(CLOSED_TO_EVERYBODY)
        | set(CREATES_THE_SCOPE)
        | set(KNOWN_GAPS)
    )


MUST_SCOPE: Tuple[Route, ...] = tuple(
    r for r in ROUTES if r.key not in _allowlisted()
)
MUST_SCOPE_IDS = [r.label for r in MUST_SCOPE]


# ---------------------------------------------------------------------------
# Driving a route to its access check
# ---------------------------------------------------------------------------
#
# Values, not assertions. The sweeps need each route to *reach* its access
# decision; a route that cannot be driven that far is reported by
# `TestTheSweepsActuallyDriveTheRoutes` rather than skipped silently, because a
# sweep that quietly stops covering a route is worse than one that never did.

PATH_VALUES: Dict[str, str] = {
    "notebook_id": THEIR_NOTEBOOK,
    "source_id": THEIR_SOURCE,
    "note_id": THEIR_NOTE,
    "insight_id": THEIR_INSIGHT,
    "session_id": THEIR_SESSION,
    "member_id": "member:someone",
    "job_id": "command:theirs",
    "command_id": "command:theirs",
    "episode_id": "episode:theirs",
}

# Body and query fields that name the content a route acts on. Filled even when
# the model marks them optional, because several routes require them in the
# handler (a note must belong to a Notebook - task 6.2) and an omitted one answers
# 400 before access is consulted.
FIELD_VALUES: Dict[str, Any] = {
    "notebook_id": THEIR_NOTEBOOK,
    "notebook_ids": [THEIR_NOTEBOOK],
    "source_id": THEIR_SOURCE,
    "note_id": THEIR_NOTE,
    "insight_id": THEIR_INSIGHT,
    "session_id": THEIR_SESSION,
    "item_id": THEIR_SOURCE,
    "item_type": "source",
    "email": "somebody@example.com",
}

# Routes that cannot be driven to their access decision from here, with the reason
# and where the behaviour is covered instead. Asserted to be exactly this set, so
# it cannot grow by accident.
UNDRIVABLE: Dict[Tuple[str, str], str] = {
    ("POST", "/api/sources"): (
        "multipart form body. The same handler is driven as JSON through "
        "POST /api/sources/json, which shares its access check"
    ),
    ("POST", "/api/search/ask"): (
        "validates three model records and an embedding model before any access "
        "decision. The graph's own refusal without a member is covered in "
        "tests/test_search_scoping.py; the scope it applies is covered by "
        "TestSearchRoutesAreScoped below"
    ),
    ("POST", "/api/search/ask/simple"): "as above",
}


def _fill(annotation: Any, *, min_length: int = 0) -> Any:
    origin = get_origin(annotation)
    if origin is Literal:
        return get_args(annotation)[0]
    if origin is Union:
        present = [a for a in get_args(annotation) if a is not type(None)]
        return _fill(present[0], min_length=min_length) if present else None
    if origin in (list, typing.List, set, typing.Set, tuple):
        return []
    if origin in (dict, typing.Dict):
        return {}
    if annotation is bool:
        return False
    if annotation is int:
        return 1
    if annotation is float:
        return 1.0
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _body_for(annotation)
    return "x" * max(min_length, 1)


def _min_length(field) -> int:
    for constraint in getattr(field, "metadata", ()):
        value = getattr(constraint, "min_length", None)
        if isinstance(value, int):
            return value
    return 0


def _body_for(model: Type[BaseModel]) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    for name, field in model.model_fields.items():
        if name in FIELD_VALUES:
            body[name] = FIELD_VALUES[name]
        elif field.is_required():
            body[name] = _fill(field.annotation, min_length=_min_length(field))
    return body


def _endpoint_params(route: Route):
    return inspect.signature(route.endpoint).parameters.values()


def _body_model(route: Route) -> Optional[Type[BaseModel]]:
    for param in _endpoint_params(route):
        if isinstance(param.default, fastapi_params.Depends):
            continue
        annotation = param.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return annotation
    return None


def _is_required(param: inspect.Parameter) -> bool:
    """Whether FastAPI will refuse the request without this parameter.

    Two shapes, and missing the second is what made `GET /api/chat/sessions`
    answer 422 instead of reaching its access check: a bare annotation with no
    default, and `Query(...)`, whose default is a `Query` object holding the
    ellipsis rather than being absent.
    """
    default = param.default
    if isinstance(default, fastapi_params.Param):
        inner = default.default
        return inner is Ellipsis or repr(inner) == "PydanticUndefined"
    return default is inspect.Parameter.empty


def _query_params(route: Route) -> Dict[str, Any]:
    """Required query parameters, so a route does not 422 before its check."""
    in_path = {segment.split("}")[0] for segment in route.path.split("{")[1:]}
    supplied: Dict[str, Any] = {}
    for param in _endpoint_params(route):
        if param.name in in_path or isinstance(param.default, fastapi_params.Depends):
            continue
        annotation = param.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            continue
        if not _is_required(param):
            continue
        supplied[param.name] = FIELD_VALUES.get(param.name, _fill(annotation))
    return supplied


def _url_for(route: Route) -> str:
    url = route.path
    for name, value in PATH_VALUES.items():
        url = url.replace("{" + name + "}", value)
    return url


# ---------------------------------------------------------------------------
# One stand-in database behind the access module and the share module
# ---------------------------------------------------------------------------


class World:
    """The access answers a sweep needs, and a count of everything asked of it.

    Keyed on query shape rather than on call order, so a test cannot pass by
    feeding the right answer to the wrong question. `shares` is mutable so that
    `share.revoke` - the real function from task 6.3 - can remove a grant and the
    next access read sees it gone.

    `access_reads` and `content_reads` are what the sweeps actually assert on, and
    they exist because **the status code is not sufficient evidence**. Upstream
    wraps a failed `Source.get` as `NotFoundError`, so a route that skipped its
    access check entirely and then failed to read the record answers 404 as well -
    the same code a correct refusal gives. Counting reads separates the two: a
    refusal must cost zero content reads, and an owner getting past the same check
    on the same URL must cost at least one.
    """

    def __init__(
        self,
        *,
        owner: Optional[str],
        shares: Optional[Set[str]] = None,
        notebook_exists: bool = True,
        content_is_orphaned: bool = False,
    ):
        self.owner = owner
        self.shares: Set[str] = set(shares or ())
        # The two other reasons a refusal can happen, as constructor options
        # rather than as subclasses that override `query`: an override that
        # short-circuits before the counters run makes `access_reads` read zero and
        # a sweep asserting on it silently stops asserting.
        self.notebook_exists = notebook_exists
        self.content_is_orphaned = content_is_orphaned
        self.access_reads = 0
        self.content_reads = 0
        self.seen: List[str] = []

    async def query(self, sql: str, params: Optional[dict] = None) -> List:
        self.access_reads += 1
        self.seen.append(sql)
        params = params or {}
        if sql.startswith("SELECT id, owner FROM $notebook"):
            if not self.notebook_exists:
                return []
            return [{"id": str(params["notebook"]), "owner": self.owner}]
        if sql.startswith("SELECT VALUE role FROM share"):
            return ["viewer"] if str(params["member"]) in self.shares else []
        if sql.startswith("SELECT VALUE id FROM notebook WHERE owner"):
            return [RecordID.parse(THEIR_NOTEBOOK)] if self.owner == CALLER_ID else []
        if sql.startswith("SELECT VALUE out FROM share WHERE in"):
            return (
                [RecordID.parse(THEIR_NOTEBOOK)]
                if str(params["member"]) in self.shares
                else []
            )
        if (
            " FROM reference WHERE in" in sql
            or " FROM artifact WHERE in" in sql
            or " FROM refers_to WHERE in" in sql
        ):
            return [] if self.content_is_orphaned else [RecordID.parse(THEIR_NOTEBOOK)]
        if sql.startswith("SELECT VALUE source FROM $insight"):
            return [RecordID.parse(THEIR_SOURCE)]
        if sql.startswith("DELETE share WHERE"):
            member = str(params["member"])
            if member not in self.shares:
                return []
            self.shares.discard(member)
            return [{"in": member, "out": str(params["notebook"])}]
        # The share-management reads (task 6.3). Answered empty rather than left
        # to the fallback below, so that a route reaching them is not mistaken for
        # a route asking an access question this stand-in has not been taught.
        if sql.startswith("SELECT * FROM share") or sql.startswith(
            "SELECT id, email FROM member"
        ):
            return []
        if sql.startswith("SELECT * FROM member WHERE email"):
            return []
        raise AssertionError(f"unexpected access query: {sql}")


def _no_real_database(world: Optional[World] = None):
    """Any content read reaches this, is counted, and fails loudly.

    `db_connection` is the single choke point every `repo_*` function goes
    through, so this covers modules that imported `repo_query` by name as well.
    The point is not tidiness: without it a handler that reads content before
    deciding access would pass against whatever is in a developer's local
    SurrealDB, which is the one environment where every check appears to work.
    """

    def boom(*_args, **_kwargs):
        if world is not None:
            world.content_reads += 1
        raise AssertionError(
            "a real database connection was opened; a route read content before "
            "it decided access"
        )

    return patch("open_notebook.database.repository.db_connection", new=boom)


@pytest.fixture
def client():
    from api.main import app

    return TestClient(app, raise_server_exceptions=False)


def _request_kwargs(route: Route) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {"params": _query_params(route)}
    body_model = _body_model(route)
    if body_model is not None:
        kwargs["json"] = _body_for(body_model)
    return kwargs


@contextlib.contextmanager
def _access_answered_by(world: "World"):
    """The access module and the share module read from `world`; nothing else reads.

    Two lookups are resolved alongside them because they sit *in front of* an
    access check and would otherwise decide the response before the check runs.
    Neither is an access decision: `get_embedding_model` gates `POST /api/embed`
    and vector search, and `content_for_job` is task 6.4's job-to-content
    resolution, tested there. Stubbing them is what makes the *access* answer the
    thing each sweep measures.
    """
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(access, "repo_query", new=world.query))
        stack.enter_context(patch.object(share_module, "repo_query", new=world.query))
        stack.enter_context(_no_real_database(world))
        stack.enter_context(
            patch(
                "open_notebook.ai.models.model_manager.get_embedding_model",
                new=AsyncMock(return_value=object()),
            )
        )
        stack.enter_context(
            patch(
                "api.routers.commands.CommandService.content_for_job",
                new=AsyncMock(return_value=("source", THEIR_SOURCE)),
            )
        )
        yield


def _drive(client, route: Route, world: "World"):
    """Issue the request with the access module answering from `world`."""
    with _access_answered_by(world):
        return client.request(route.method, _url_for(route), **_request_kwargs(route))


def _payload(response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _carries_no_content(response) -> bool:
    """An error shape and nothing else.

    A refusal that also returned a partial record would satisfy a status-code
    assertion while disclosing the thing it refused, so the body is checked too.
    """
    payload = _payload(response)
    if isinstance(payload, dict):
        return set(payload) <= {"detail"}
    return not payload


# ---------------------------------------------------------------------------
# 1. The partition
# ---------------------------------------------------------------------------


class TestTheRouteTable:
    """Every registered route lands in exactly one category.

    This is the mechanism the task asks for: a route added later cannot avoid
    being classified, and the only category that needs no scoping is one of the
    explicit allowlists above.
    """

    def test_the_table_is_derived_from_the_application(self):
        assert ROUTES, "no routes were derived; the sweeps below would all be vacuous"
        assert len(ROUTES) == len(BY_KEY), "two routes share a method and path"

    def test_every_route_is_in_exactly_one_category(self):
        categories = {
            "configuration": {r.key for r in _configuration_routes()},
            "public": set(PUBLIC),
            "closed": set(CLOSED_TO_EVERYBODY),
            "creates": set(CREATES_THE_SCOPE),
            "gap": set(KNOWN_GAPS),
            "must-scope": {r.key for r in MUST_SCOPE},
        }
        for route in ROUTES:
            holding = [name for name, keys in categories.items() if route.key in keys]
            assert len(holding) == 1, (
                f"{route.label} is in {holding or 'no category'}; every route must "
                "be classified exactly once"
            )

    def test_no_allowlist_entry_is_stale(self):
        """A route that has been renamed, removed or fixed must not stay listed.

        Without this a gap closed upstream would keep its entry, and the file
        would claim an exposure that no longer exists.
        """
        for name, entries in (
            ("PUBLIC", PUBLIC),
            ("CLOSED_TO_EVERYBODY", CLOSED_TO_EVERYBODY),
            ("CREATES_THE_SCOPE", CREATES_THE_SCOPE),
            ("KNOWN_GAPS", KNOWN_GAPS),
            ("UNDRIVABLE", UNDRIVABLE),
        ):
            for key in entries:
                assert key in BY_KEY, (
                    f"{name} lists {key[0]} {key[1]}, which is not a registered "
                    "route. Delete the entry."
                )

    def test_every_known_gap_is_still_a_gap(self):
        """The entry is deleted by the task that closes it, not left behind."""
        for key, reason in KNOWN_GAPS.items():
            route = BY_KEY[key]
            assert not route.scoping_reach, (
                f"{route.label} now reaches {route.scoping_reach}, so it is no "
                f"longer the gap recorded as {reason!r}. Remove it from KNOWN_GAPS "
                "so the sweeps start covering it."
            )

    def test_the_configuration_routers_have_not_grown(self):
        """An exact count, because the allowlist is by module rather than by route.

        Adding a Notebook-touching route to one of these routers would otherwise
        be allowlisted silently. Failing here is the review moment: either the new
        route carries no Notebook and the count moves, or it belongs in the swept
        set.
        """
        found = _configuration_routes()
        assert len(found) == CONFIGURATION_ROUTE_COUNT, (
            f"{len(found)} routes now live in the configuration routers "
            f"{sorted(CONFIGURATION_ROUTERS)}, not {CONFIGURATION_ROUTE_COUNT}. "
            "If the new one carries no Notebook, update the count; if it does, "
            "move it out of these routers or scope it.\n"
            + "\n".join(f"  {r.label} ({r.module})" for r in found)
        )

    def test_no_configuration_route_touches_notebook_content(self):
        """Cheap corroboration of the allowlist's premise.

        A route in these modules that resolves content through the access module
        is not instance configuration, whatever module it sits in.
        """
        for route in _configuration_routes():
            assert not route.scoping_reach, (
                f"{route.label} is allowlisted as instance configuration but "
                f"reaches {route.scoping_reach}, so it does touch member content"
            )

    def test_the_swept_set_is_the_bulk_of_the_member_facing_api(self):
        """A floor, so the sweeps cannot be hollowed out by allowlist growth.

        Not an exact count - routes come and go - but if fewer than 40 routes are
        being swept, something has moved a large part of the API into an
        allowlist and that is worth failing over.
        """
        assert len(MUST_SCOPE) >= 40, (
            f"only {len(MUST_SCOPE)} routes are swept: {MUST_SCOPE_IDS}"
        )

    def test_every_swept_route_resolves_a_member(self):
        """Requirement 4.2. A route that scopes without knowing who is calling
        cannot be scoping against anything."""
        for route in MUST_SCOPE:
            assert route.resolves_member, (
                f"{route.label} is expected to enforce access but does not depend "
                "on current_member, so it has no caller to scope against"
            )


# ---------------------------------------------------------------------------
# 2. Static reach
# ---------------------------------------------------------------------------


class TestEveryScopedRouteReachesTheAccessModule:
    @pytest.mark.parametrize("route", MUST_SCOPE, ids=MUST_SCOPE_IDS)
    def test_the_handler_reaches_a_scoping_primitive(self, route: Route):
        assert route.scoping_reach, (
            f"{route.label} ({route.module}.{route.handler}) reaches no access "
            "check. Every route that touches a Notebook, a Source, a note, an "
            "insight, a chat session or search must call one of "
            f"{ACCESS_PRIMITIVES + SEARCH_PRIMITIVES} or run the ask graph with a "
            "member. If the route genuinely carries no Notebook, add it to an "
            "allowlist with a reason instead of leaving it unclassified."
        )

    def test_the_reach_analysis_would_notice_a_route_with_no_check(self):
        """The analysis has to be able to fail, or the sweep above is decorative.

        A stand-in handler shaped exactly like a real one, with the access call
        left out.
        """

        async def unscoped_handler(notebook_id: str, member: Member):
            from open_notebook.domain.notebook import Notebook

            return await Notebook.get(notebook_id)

        assert _scoping_reach(unscoped_handler) == ()

    def test_the_reach_analysis_follows_delegation_within_the_api_layer(self):
        """POST /api/sources/json holds no check of its own and is not a gap.

        It calls `create_source` directly, so FastAPI resolves no dependencies for
        it; the check it relies on is one function away. If the analysis stopped at
        depth zero this route would read as unscoped and the allowlist would grow
        to hide it.
        """
        route = BY_KEY[("POST", "/api/sources/json")]
        assert "require_notebooks_write" in route.scoping_reach
        assert "require_notebooks_write" not in _names_in(route.endpoint).referenced


# ---------------------------------------------------------------------------
# 3. A member with no access gets 404, and never content
# ---------------------------------------------------------------------------


STRANGER_SWEEP = tuple(r for r in MUST_SCOPE if r.key not in UNDRIVABLE)
STRANGER_IDS = [r.label for r in STRANGER_SWEEP]

# Routes that answer a member with no access from their own scope rather than by
# refusing one record: a list returns an empty page and search returns no matches,
# both of which are correct and neither of which is a 404. Covered by
# `TestScopedListsAndSearchExcludeWhatTheCallerCannotReach`.
COLLECTION_ROUTES: Dict[Tuple[str, str], str] = {
    ("GET", "/api/notebooks"): "the caller's own Notebooks",
    ("GET", "/api/recently-viewed"): "the caller's own recent items",
    ("GET", "/api/sources"): "Sources in the caller's Notebooks",
    ("GET", "/api/notes"): "notes in the caller's Notebooks",
    ("POST", "/api/search"): "matches within the caller's Notebooks",
}


class TestAMemberWithNoAccessIsRefused:
    """Requirements 5.4, 7.1 and 7.4, swept over the derived table.

    The stranger owns nothing here and holds no Share, so every route addressing a
    record must answer 404 - not 403, which would confirm the record exists - and
    must return an error body carrying no part of it.
    """

    @pytest.mark.parametrize("route", STRANGER_SWEEP, ids=STRANGER_IDS)
    def test_a_stranger_is_refused_and_told_nothing(self, client, route: Route):
        world = World(owner=SOMEBODY_ELSE)
        response = _drive(client, route, world)

        assert world.access_reads > 0, (
            f"{route.label} answered without asking an access question at all"
        )

        if route.key in COLLECTION_ROUTES:
            # A collection answers this member from their own scope rather than by
            # refusing a record, so neither a 404 nor "no query ran" is the right
            # expectation: the query *does* run, bound to an empty set of
            # Notebooks. That binding is what has to be right, and
            # TestScopedListsAndSearchExcludeWhatTheCallerCannotReach asserts it.
            return

        assert world.content_reads == 0, (
            f"{route.label} read content {world.content_reads} time(s) on behalf of "
            "a member with no access. Whatever it answered, the check ran too late "
            f"or not at all: {_payload(response)}"
        )
        assert response.status_code == 404, (
            f"{route.label} answered {response.status_code} to a member with no "
            f"ownership and no Share, not 404: {_payload(response)}. A 403 would "
            "confirm the record exists (Requirement 7.4)."
        )
        assert _carries_no_content(response), (
            f"{route.label} refused but returned {_payload(response)}"
        )

    @pytest.mark.parametrize(
        "route",
        tuple(r for r in STRANGER_SWEEP if r.key not in COLLECTION_ROUTES),
        ids=[r.label for r in STRANGER_SWEEP if r.key not in COLLECTION_ROUTES],
    )
    def test_the_response_depends_on_the_access_answer(self, client, route: Route):
        """The guard that stops the sweep above passing for the wrong reason.

        A 404 from a mistyped URL is indistinguishable from a 404 from a refusal,
        and so is a 404 from a record the blocked database could not return. So the
        same request is issued twice with *only* the access answer changed, and
        something about what the application did must differ: the status, the
        number of content reads, or the sequence of access questions it asked.

        All three are needed. Content routes differ in reads; the Share routes do
        their post-check work inside the `share` relation, which this stand-in
        answers rather than the blocked database, so they differ only in the query
        trace; and `GET /api/commands/jobs/{job_id}` differs only in status,
        because its post-check read goes through the command library.
        """
        refused_world = World(owner=SOMEBODY_ELSE)
        refused = _drive(client, route, refused_world)
        allowed_world = World(owner=CALLER_ID)
        allowed = _drive(client, route, allowed_world)

        differs = (
            refused.status_code != allowed.status_code
            or refused_world.content_reads != allowed_world.content_reads
            or refused_world.seen != allowed_world.seen
        )
        assert differs, (
            f"{route.label} behaved identically for a stranger and for the owner - "
            f"both answered {allowed.status_code} with {allowed_world.content_reads} "
            "content read(s) and the same access questions - so its response does "
            f"not depend on access at all: {_payload(allowed)}"
        )


class TestTheSweepsActuallyDriveTheRoutes:
    def test_the_undrivable_set_is_exactly_what_is_recorded(self, client):
        """A sweep that silently stops covering a route is worse than one that
        never covered it.

        "Drivable" is measured directly rather than inferred from a status code:
        the route reached its access check if and only if it asked the access
        module a question. Inferring it from 400/422 was the first version of this
        test and it was wrong - the two ask routes answer *404* when their model
        lookup fails, which reads exactly like a refusal while the access check has
        not run.
        """
        measured: Dict[Tuple[str, str], int] = {}
        for route in MUST_SCOPE:
            world = World(owner=SOMEBODY_ELSE)
            response = _drive(client, route, world)
            if world.access_reads == 0:
                measured[route.key] = response.status_code

        assert set(measured) == set(UNDRIVABLE), (
            "the set of routes that cannot be driven to their access check has "
            f"changed.\n  now undrivable: {sorted(measured)}\n  recorded: "
            f"{sorted(UNDRIVABLE)}\nAdd a value to FIELD_VALUES so the route can be "
            "driven, or record it in UNDRIVABLE with a reason and where its "
            "behaviour is covered instead."
        )


# ---------------------------------------------------------------------------
# 4. A Viewer's writes are refused
# ---------------------------------------------------------------------------


# Reads that are gated on write on purpose, so a Viewer is refused them too.
VIEWER_IS_REFUSED: Dict[Tuple[str, str], str] = {
    ("GET", "/api/notebooks/{notebook_id}/delete-preview"): (
        "an owner-only dialog; task 6.3 stopped the UI offering it to a Viewer"
    ),
    ("GET", "/api/notebooks/{notebook_id}/shares"): (
        "listing Shares would tell a Viewer the other Viewers' addresses "
        "(task 6.3: visibility runs to the owner, never sideways)"
    ),
}

# **Writes by HTTP method that a Viewer is deliberately allowed**, because
# Requirement 6.3 makes asking questions a Viewer's own right. Task 6.2 set the
# rule: creating and using a chat session needs *read* access, while renaming or
# deleting one needs ownership, since that would be renaming somebody else's.
#
# Listed here rather than left out of the sweep by a method-shaped rule, because
# "which POST is a read" is the judgement a future route will get wrong, and this
# is where it has to be argued.
VIEWER_MAY: Dict[Tuple[str, str], str] = {
    ("POST", "/api/chat/sessions"): "starting a conversation about a shared Notebook",
    ("POST", "/api/chat/execute"): "asking a question (Requirement 6.3)",
    ("POST", "/api/chat/context"): "assembling the context a question is asked against",
    ("POST", "/api/sources/{source_id}/chat/sessions"): (
        "the same, scoped to one Source"
    ),
    ("POST", "/api/sources/{source_id}/chat/sessions/{session_id}/messages"): (
        "asking a question of one Source"
    ),
}

VIEWER_SWEEP = tuple(
    r
    for r in MUST_SCOPE
    if r.key not in UNDRIVABLE
    and r.key not in COLLECTION_ROUTES
    and r.key not in VIEWER_MAY
    and (r.is_write or r.key in VIEWER_IS_REFUSED)
)
VIEWER_IDS = [r.label for r in VIEWER_SWEEP]
VIEWER_MAY_SWEEP = tuple(r for r in MUST_SCOPE if r.key in VIEWER_MAY)
VIEWER_MAY_IDS = [r.label for r in VIEWER_MAY_SWEEP]


class TestAViewerMayNotWrite:
    """Requirements 6.4 and 5.2, swept over every write route in the table.

    403 rather than 404: a Viewer can already see the Notebook, so hiding its
    existence achieves nothing and a 404 would read as a defect.
    """

    @pytest.mark.parametrize("route", VIEWER_SWEEP, ids=VIEWER_IDS)
    def test_a_viewers_write_is_forbidden(self, client, route: Route):
        world = World(owner=SOMEBODY_ELSE, shares={CALLER_ID})
        response = _drive(client, route, world)

        assert response.status_code == 403, (
            f"{route.label} answered {response.status_code} to a Viewer, not 403: "
            f"{_payload(response)}. A 2xx means the write was allowed; a 500 means "
            "the handler read content before deciding."
        )
        assert _carries_no_content(response)

    def test_the_sweep_covers_every_write_route_it_can_reach(self):
        writes = {r.key for r in MUST_SCOPE if r.is_write}
        covered = {r.key for r in VIEWER_SWEEP if r.is_write}
        missing = (
            writes
            - covered
            - set(UNDRIVABLE)
            - set(COLLECTION_ROUTES)
            - set(VIEWER_MAY)
        )
        assert not missing, (
            f"write routes not swept for a Viewer: {sorted(missing)}. Either the "
            "route refuses a Viewer and belongs in the sweep, or it is read-level "
            "on purpose and belongs in VIEWER_MAY with the reason."
        )

    @pytest.mark.parametrize("route", VIEWER_MAY_SWEEP, ids=VIEWER_MAY_IDS)
    def test_the_reads_a_viewer_is_allowed_are_not_refused(self, client, route: Route):
        """Requirement 6.3, the other direction.

        A Share that refused everything would satisfy every test above and grant
        nothing, and the classroom case is the whole reason Shares exist. These
        routes must get *past* access for a Viewer - they then fail on the blocked
        database, which is the evidence that the check passed rather than that the
        route worked.
        """
        world = World(owner=SOMEBODY_ELSE, shares={CALLER_ID})
        response = _drive(client, route, world)
        assert world.content_reads > 0, (
            f"{route.label} answered {response.status_code} to a Viewer without "
            f"reading any content, so the Share granted nothing: "
            f"{_payload(response)}. Asking questions of a shared Notebook is a "
            "Viewer's own right; if this route should be owner-only, move it out "
            "of VIEWER_MAY."
        )

    def test_the_share_grants_read_and_withholds_write(self):
        world = World(owner=SOMEBODY_ELSE, shares={CALLER_ID})
        with patch.object(access, "repo_query", new=world.query):
            resolved = _run(access.notebook_access(_caller(), THEIR_NOTEBOOK))
        assert resolved.role is access.Role.VIEWER
        assert resolved.can_read and not resolved.can_write


class TestTheFrontendGapTaskSixThreeLeftOpen:
    """Task 6.3: the source-detail modal and the standalone `/sources` page still
    offer their own write actions to a Viewer.

    6.3 asked for the gap to become visible per route here, so these are the
    routes those two surfaces call. Every one refuses the Viewer with 403, which
    is what makes this a presentation gap and not a disclosure: nothing is
    reachable that a Viewer could not already read, and the backend is not what
    needs changing.
    """

    SURFACED_BY_THE_FRONTEND: Dict[Tuple[str, str], str] = {
        ("PUT", "/api/sources/{source_id}"): "source editing in the detail modal",
        ("DELETE", "/api/sources/{source_id}"): "delete, on the /sources page",
        ("POST", "/api/sources/{source_id}/insights"): "insight generation",
        ("POST", "/api/sources/{source_id}/retry"): "retry processing",
        ("POST", "/api/embed"): "re-embed from the detail modal",
        ("DELETE", "/api/insights/{insight_id}"): "deleting a generated insight",
    }

    def test_each_of_them_is_a_registered_route_the_viewer_sweep_covers(self):
        for key, surface in self.SURFACED_BY_THE_FRONTEND.items():
            assert key in BY_KEY, f"{key} ({surface}) is no longer registered"
            assert key in {r.key for r in VIEWER_SWEEP}, (
                f"{key} ({surface}) is not in the Viewer sweep, so the 403 it "
                "relies on is no longer asserted anywhere"
            )

    def test_the_backend_refuses_every_one_of_them(self, client):
        for key, surface in self.SURFACED_BY_THE_FRONTEND.items():
            world = World(owner=SOMEBODY_ELSE, shares={CALLER_ID})
            response = _drive(client, BY_KEY[key], world)
            assert response.status_code == 403, (
                f"{key[0]} {key[1]} ({surface}) answered {response.status_code} to "
                f"a Viewer: {_payload(response)}"
            )


# ---------------------------------------------------------------------------
# 5. Existence is not disclosed
# ---------------------------------------------------------------------------


# The five ways a record can be addressed by id, one per kind of content. Each
# has its own inheritance path through the access module - `reference`, `artifact`,
# `refers_to`, and an insight's `source` link - so one of them getting the rule
# wrong is not visible from the others.
ID_ADDRESSED_KINDS: Tuple[Tuple[str, str], ...] = (
    ("GET", "/api/notebooks/{notebook_id}"),
    ("GET", "/api/sources/{source_id}"),
    ("GET", "/api/notes/{note_id}"),
    ("GET", "/api/insights/{insight_id}"),
    ("GET", "/api/chat/sessions/{session_id}"),
)
KIND_IDS = [path for _, path in ID_ADDRESSED_KINDS]


def _reasons_for_refusing(path: str) -> Tuple[Tuple[str, World], ...]:
    """Every distinct reason the application has for refusing this route.

    All of them must read identically. The Notebook route has two - it is the
    Notebook itself, so it cannot be orphaned - and every kind of content has a
    third, because content in no Notebook inherits from nothing.
    """
    reasons = [
        ("somebody else's", World(owner=SOMEBODY_ELSE)),
        ("does not exist", World(owner=CALLER_ID, notebook_exists=False)),
    ]
    if path != "/api/notebooks/{notebook_id}":
        reasons.append(
            ("in no notebook", World(owner=CALLER_ID, content_is_orphaned=True))
        )
    return tuple(reasons)


class TestExistenceIsNotDisclosed:
    """Requirement 7.4, message included.

    A status code that matched while the message differed would still be an oracle:
    "Notebook not found" against "You do not have access to that notebook" tells a
    stranger which Notebooks exist.

    **Where the property actually comes from differs by route, and that is worth
    knowing before changing either layer.** Measured by mutation: four of these
    five routers catch `NotFoundError` and re-raise one fixed message of their own,
    so the access module's wording never reaches the client and could drift
    unnoticed there. `GET /api/notebooks/{notebook_id}` passes it straight through,
    which is why a message naming its reason is visible at all. So these tests are
    parameterised over all five rather than sampling one: the Notebook route is the
    one that catches drift in the access module, and the other four catch a router
    that stops normalising.
    """

    @pytest.mark.parametrize("method,path", ID_ADDRESSED_KINDS, ids=KIND_IDS)
    def test_every_reason_for_refusing_reads_identically(self, client, method, path):
        route = BY_KEY[(method, path)]
        answers = {}
        for reason, world in _reasons_for_refusing(path):
            response = _drive(client, route, world)
            assert response.status_code == 404, (
                f"{route.label} answered {response.status_code} when the record was "
                f"{reason}: {_payload(response)}"
            )
            answers[reason] = _payload(response)

        distinct = {str(answer) for answer in answers.values()}
        assert len(distinct) == 1, (
            f"{route.label} tells a member which of these happened: {answers}. A "
            "record that does not exist, one belonging to somebody else, and one in "
            "no notebook at all must be indistinguishable."
        )

    @pytest.mark.parametrize("method,path", ID_ADDRESSED_KINDS, ids=KIND_IDS)
    def test_a_refusal_never_echoes_the_record_it_refused(self, client, method, path):
        """The id came from the caller, so echoing it discloses nothing on its own -
        but a message built from the record would, and this is the cheap way to
        notice one appearing."""
        response = _drive(client, BY_KEY[(method, path)], World(owner=SOMEBODY_ELSE))
        detail = str(_payload(response))
        assert "theirs" not in detail, (
            f"{method} {path} refused with {detail!r}, which is built from the "
            "record rather than being a fixed message"
        )

    def test_the_refusals_do_not_differ_between_kinds_in_a_way_that_maps_the_store(
        self, client
    ):
        """Naming the kind is fine - the caller chose the route - but the message
        must not go further and say whether the *notebook* behind it exists."""
        for method, path in ID_ADDRESSED_KINDS:
            response = _drive(client, BY_KEY[(method, path)], World(owner=SOMEBODY_ELSE))
            detail = str(_payload(response)).lower()
            for leak in ("access", "permission", "denied", "owner", "forbidden"):
                assert leak not in detail, (
                    f"{method} {path} refused with {detail!r}; a refusal that talks "
                    "about access confirms the record is there to be refused"
                )


# ---------------------------------------------------------------------------
# 6. A revoked Share ends access
# ---------------------------------------------------------------------------


class TestARevokedShareEndsAccess:
    """Requirement 6.5, driven through the real `share.revoke` from task 6.3.

    One stand-in database behind both modules, so the revoke that runs is the real
    statement and the access read that follows sees what it actually did. A test
    that patched the access answer directly would prove the fixture changed, not
    that revocation works.
    """

    def test_a_shared_notebook_becomes_unreachable_on_the_next_request(self, client):
        route = BY_KEY[("GET", "/api/notebooks/{notebook_id}")]
        world = World(owner=SOMEBODY_ELSE, shares={CALLER_ID})

        with patch.object(access, "repo_query", new=world.query):
            before = _run(access.notebook_access(_caller(), THEIR_NOTEBOOK))
            assert before.can_read, "the Share granted nothing to begin with"

        with patch.object(share_module, "repo_query", new=world.query):
            revoked = _run(share_module.revoke(THEIR_NOTEBOOK, CALLER_ID))
        assert revoked is True

        after = _drive(client, route, world)
        assert after.status_code == 404, (
            f"the Notebook still answered {after.status_code} after its Share was "
            f"revoked: {_payload(after)}"
        )
        assert _carries_no_content(after)

    def test_revocation_reaches_every_kind_of_content_at_once(self, client):
        """Requirement 5.3 under revocation. Sources, notes, insights and chat
        sessions inherit from the Notebook, so one revoke must close all of them -
        there is no per-content grant to miss."""
        world = World(owner=SOMEBODY_ELSE, shares={CALLER_ID})
        with patch.object(share_module, "repo_query", new=world.query):
            _run(share_module.revoke(THEIR_NOTEBOOK, CALLER_ID))

        for key in (
            ("GET", "/api/notebooks/{notebook_id}"),
            ("GET", "/api/sources/{source_id}"),
            ("GET", "/api/notes/{note_id}"),
            ("GET", "/api/insights/{insight_id}"),
            ("GET", "/api/chat/sessions/{session_id}"),
        ):
            response = _drive(client, BY_KEY[key], world)
            assert response.status_code == 404, (
                f"{key[0]} {key[1]} still answered {response.status_code} after the "
                "Share on its Notebook was revoked"
            )

    def test_the_revoked_notebook_leaves_the_members_list_scope(self, client):
        """Requirement 5.5: it must also stop being *listed*, not merely stop
        opening. A Notebook that still appears in a list but 404s on open reads as
        a broken application rather than as revoked access."""
        world = World(owner=SOMEBODY_ELSE, shares={CALLER_ID})
        with patch.object(access, "repo_query", new=world.query):
            assert _run(access.accessible_notebook_ids(_caller())) == [THEIR_NOTEBOOK]
        with patch.object(share_module, "repo_query", new=world.query):
            _run(share_module.revoke(THEIR_NOTEBOOK, CALLER_ID))
        with patch.object(access, "repo_query", new=world.query):
            assert _run(access.accessible_notebook_ids(_caller())) == []

    def test_nothing_caches_the_grant_across_requests(self, client):
        """Access is read per request and there is no session to expire, so the
        same client must see the change without being rebuilt."""
        route = BY_KEY[("GET", "/api/notebooks/{notebook_id}")]
        world = World(owner=SOMEBODY_ELSE, shares={CALLER_ID})

        first = _drive(client, route, world)
        assert first.status_code != 404, (
            "the Viewer could not read the Notebook even before revocation, so the "
            "flip below would prove nothing"
        )

        with patch.object(share_module, "repo_query", new=world.query):
            _run(share_module.revoke(THEIR_NOTEBOOK, CALLER_ID))

        second = _drive(client, route, world)
        assert second.status_code == 404


# ---------------------------------------------------------------------------
# 7. Lists and search exclude what the caller cannot reach
# ---------------------------------------------------------------------------


class TestScopedListsAndSearchExcludeWhatTheCallerCannotReach:
    """Requirements 7.3 and 5.5 at the route level.

    Task 6.4 covers the search functions and the SurrealQL. What is left for a
    route to get wrong is the scope it hands down, and the failure is invisible:
    an unscoped list returns more rows, which looks like success, and a scope that
    silently became empty looks exactly like a member who owns nothing.
    """

    LIST_ROUTES = (
        ("GET", "/api/notebooks", "api.routers.notebooks.repo_query"),
        ("GET", "/api/recently-viewed", "api.routers.notebooks.repo_query"),
        ("GET", "/api/sources", "api.routers.sources.repo_query"),
        ("GET", "/api/notes", "api.routers.notes.repo_query"),
    )
    LIST_IDS = [path for _, path, _ in LIST_ROUTES]

    # The one name all four list routes bind the member's scope under.
    SCOPE = "accessible_notebooks"

    @staticmethod
    def _queries_issued(client, method, path, query_symbol, world):
        issued: List[Tuple[str, Dict[str, Any]]] = []

        async def repo_query(sql, params=None):
            issued.append((" ".join(sql.split()), params or {}))
            return []

        with (
            patch.object(access, "repo_query", new=world.query),
            patch(query_symbol, new=repo_query),
            _no_real_database(world),
        ):
            response = client.request(method, path)
        return response, issued

    @pytest.mark.parametrize(
        "method,path,query_symbol", LIST_ROUTES, ids=LIST_IDS
    )
    def test_every_query_a_list_issues_filters_on_the_callers_notebooks(
        self, client, method, path, query_symbol
    ):
        """Every query, not the last one, and the filter must be *used*.

        Both halves were mutation-found. `GET /api/recently-viewed` issues two
        queries and an assertion that inspected only the last would have let the
        first go unscoped. And binding the parameter is not the same as filtering
        on it: deleting `WHERE id IN $accessible_notebooks` while leaving the bind
        in place passed an earlier version of this test, which is exactly the inert
        filter that looks correct - the same class of fault task 6.4 found in its
        own suite.
        """
        world = World(owner=CALLER_ID)
        response, issued = self._queries_issued(
            client, method, path, query_symbol, world
        )

        assert response.status_code == 200, _payload(response)
        assert issued, f"{method} {path} issued no query at all"

        for sql, params in issued:
            # Lowered because the four routers differ on keyword case, and the
            # `IN` is part of the pattern on purpose: a parameter that appears only
            # in a projection is bound and inert.
            assert f"in ${self.SCOPE}" in sql.lower(), (
                f"{method} {path} issued a query that does not filter on "
                f"${self.SCOPE}, so it spans every member's rows: {sql[:160]}"
            )
            bound = params.get(self.SCOPE)
            assert bound is not None, (
                f"{method} {path} names ${self.SCOPE} in its SurrealQL but binds "
                f"nothing to it: {sorted(params)}"
            )
            assert all(isinstance(item, RecordID) for item in bound), (
                f"{method} {path} bound the scope as "
                f"{[type(i).__name__ for i in bound]}; string elements match "
                "nothing in SurrealDB, silently (task 6.3)"
            )
            assert [str(item) for item in bound] == [THEIR_NOTEBOOK]

    @pytest.mark.parametrize(
        "method,path,query_symbol", LIST_ROUTES, ids=LIST_IDS
    )
    def test_a_member_with_no_access_scopes_to_an_empty_set(
        self, client, method, path, query_symbol
    ):
        """Empty, never absent. A missing filter matches everything, and `id IN []`
        matches nothing - measured on a real database in task 6.2."""
        world = World(owner=SOMEBODY_ELSE)
        response, issued = self._queries_issued(
            client, method, path, query_symbol, world
        )

        assert response.status_code == 200, _payload(response)
        assert issued
        for _sql, params in issued:
            assert params.get(self.SCOPE) == [], (
                f"{method} {path} ran for a member who can reach nothing with "
                f"{self.SCOPE}={params.get(self.SCOPE)!r}"
            )


class TestSearchRoutesAreScoped:
    """The search route hands the member down; the query does the filtering.

    Task 6.4 established why the filter has to be inside the function rather than
    around the call: both search functions LIMIT their ranked output, so filtering
    afterwards narrows a top-N already chosen across every member's content.
    """

    @pytest.mark.parametrize("search_type", ["text", "vector"])
    def test_the_route_passes_the_caller_to_the_search_function(
        self, client, search_type
    ):
        symbol = "text_search" if search_type == "text" else "vector_search"
        called = AsyncMock(return_value=[])
        world = World(owner=CALLER_ID)
        with (
            patch.object(access, "repo_query", new=world.query),
            patch(f"api.routers.search.{symbol}", new=called),
            patch(
                "api.routers.search.model_manager.get_embedding_model",
                new=AsyncMock(return_value=object()),
            ),
            _no_real_database(),
        ):
            response = client.post(
                "/api/search", json={"query": "photosynthesis", "type": search_type}
            )

        assert response.status_code == 200, _payload(response)
        assert called.await_args is not None, f"{symbol} was never called"
        passed = called.await_args.kwargs.get("member")
        assert isinstance(passed, Member) and passed.id == CALLER_ID

    @pytest.mark.parametrize("owner", [CALLER_ID, SOMEBODY_ELSE])
    def test_the_scope_reaching_the_query_is_the_callers_own(self, client, owner):
        """Through the real `text_search`, so the binding under test is the one the
        SurrealQL receives rather than a router argument."""
        captured: Dict[str, Any] = {}

        async def repo_query(sql, params=None):
            captured["sql"] = sql
            captured["params"] = params or {}
            return []

        world = World(owner=owner)
        with (
            patch.object(access, "repo_query", new=world.query),
            patch.object(notebook_module, "repo_query", new=repo_query),
            _no_real_database(),
        ):
            response = client.post("/api/search", json={"query": "x", "type": "text"})

        assert response.status_code == 200, _payload(response)
        assert "$notebooks" in captured["sql"], (
            "the scope is not in the SurrealQL, so the ranking and total_count are "
            "computed over every member's content"
        )
        expected = [THEIR_NOTEBOOK] if owner == CALLER_ID else []
        assert [str(n) for n in captured["params"]["notebooks"]] == expected

    def test_the_route_does_not_filter_results_after_the_fact(self):
        """The one shape that looks equivalent and is not (ADR-009)."""
        source = inspect.getsource(
            BY_KEY[("POST", "/api/search")].endpoint
        )
        for leak in ("for r in results", "if r[", "filter("):
            assert leak not in source, (
                "the route filters what came back; the count and the ordering would "
                "still be derived from every member's material"
            )


# ---------------------------------------------------------------------------
# 8. An outage is 503, never an empty answer
# ---------------------------------------------------------------------------


class TestAnAccessOutageIsNeverAnEmptyAnswer:
    """The rule tasks 5.3, 6.2 and 6.4 each established, swept here.

    An empty list is what "you own nothing" looks like and an empty result set is
    what "no matches" looks like, so an access read answered as either has no
    symptom at all. 503 is the only honest answer.
    """

    OUTAGE_ROUTES = (
        ("GET", "/api/notebooks"),
        ("GET", "/api/recently-viewed"),
        ("GET", "/api/sources"),
        ("GET", "/api/notes"),
        ("POST", "/api/search"),
        ("GET", "/api/notebooks/{notebook_id}"),
        ("GET", "/api/sources/{source_id}"),
        ("GET", "/api/notes/{note_id}"),
    )

    @pytest.mark.parametrize(
        "method,path", OUTAGE_ROUTES, ids=[f"{m} {p}" for m, p in OUTAGE_ROUTES]
    )
    def test_an_unreachable_database_answers_503(self, client, method, path):
        route = BY_KEY[(method, path)]

        async def unreachable(*_args, **_kwargs):
            raise RuntimeError("connection refused")

        with (
            patch.object(access, "repo_query", new=unreachable),
            patch(
                "open_notebook.ai.models.model_manager.get_embedding_model",
                new=AsyncMock(return_value=object()),
            ),
            _no_real_database(),
        ):
            body_model = _body_model(route)
            request: Dict[str, Any] = {"params": _query_params(route)}
            if body_model is not None:
                request["json"] = _body_for(body_model)
            response = client.request(route.method, _url_for(route), **request)

        assert response.status_code == 503, (
            f"{route.label} answered {response.status_code} while access could not "
            f"be determined: {_payload(response)}"
        )

    @pytest.mark.parametrize(
        "method,path",
        tuple(COLLECTION_ROUTES),
        ids=[f"{m} {p}" for m, p in COLLECTION_ROUTES],
    )
    def test_no_collection_route_answers_an_outage_with_an_empty_collection(
        self, client, method, path
    ):
        route = BY_KEY[(method, path)]

        async def unreachable(*_args, **_kwargs):
            raise RuntimeError("connection refused")

        with (
            patch.object(access, "repo_query", new=unreachable),
            patch(
                "open_notebook.ai.models.model_manager.get_embedding_model",
                new=AsyncMock(return_value=object()),
            ),
            _no_real_database(),
        ):
            body_model = _body_model(route)
            request: Dict[str, Any] = {"params": _query_params(route)}
            if body_model is not None:
                request["json"] = _body_for(body_model)
            response = client.request(route.method, _url_for(route), **request)

        assert response.status_code != 200, (
            f"{route.label} answered 200 with {_payload(response)} while access was "
            "unknown, which is indistinguishable from reaching nothing"
        )


# ---------------------------------------------------------------------------
# 9. Authentication, and the routes closed to everybody
# ---------------------------------------------------------------------------


@pytest.mark.no_auth_bypass
class TestAuthenticationIsRequired:
    """Requirements 4.1 and 4.5, and the first half of "401 or 404, never content".

    Swept over the derived table with the authentication bypass off, so the real
    middleware runs.
    """

    @pytest.fixture
    def unauthenticated(self):
        """A client whose middleware stack was built without conftest's bypass.

        Worth spelling out, because getting this wrong makes the sweep below pass
        while measuring nothing. Starlette binds `dispatch` when it *constructs*
        the middleware instance, and it constructs it once - on the first request
        of the process. By the time this file runs, some earlier test has already
        built the stack with conftest's bypass bound into it, so a 401 sweep
        against the shared client resolves every caller to the test member and
        reads 503 from the blocked database rather than 401.

        Rebuilding the stack here, with the `no_auth_bypass` marker keeping the
        patch off, is what reaches the real gate on the real route table. The
        previous stack is restored so nothing after this class is affected.
        """
        from api.main import app

        previous = app.middleware_stack
        app.middleware_stack = app.build_middleware_stack()
        try:
            yield TestClient(app, raise_server_exceptions=False)
        finally:
            app.middleware_stack = previous

    @pytest.mark.parametrize("route", MUST_SCOPE, ids=MUST_SCOPE_IDS)
    def test_a_request_with_no_token_is_401(self, unauthenticated, route: Route):
        with _no_real_database():
            response = unauthenticated.request(
                route.method, _url_for(route), json={}
            )
        assert response.status_code == 401, (
            f"{route.label} answered {response.status_code} without a token: "
            f"{_payload(response)}"
        )
        assert _carries_no_content(response)

    def test_the_401_comes_from_the_middleware_and_not_from_a_route(
        self, unauthenticated
    ):
        """Which of the two gates answered, distinguished by a header.

        `MemberAuthMiddleware._unauthorised` is the only thing in the application
        that sets `WWW-Authenticate: Bearer`; the `AuthenticationError` handler
        behind `current_member` answers 401 without it. Both are correct refusals,
        but they are not equally good: the middleware refuses before routing, so a
        route that forgot to depend on `current_member` is still refused. Asserting
        the header is how the sweep above knows it is measuring the outer gate.
        """
        with _no_real_database():
            response = unauthenticated.get("/api/notebooks")
        assert response.status_code == 401
        assert response.headers.get("WWW-Authenticate") == "Bearer", (
            "the refusal did not come from MemberAuthMiddleware, so the sweep "
            "above is measuring each route's own dependency rather than the gate "
            "in front of all of them"
        )

    def test_the_paths_that_skip_authentication_are_the_named_ones(self):
        """No swept route may be excluded from the gate.

        The excluded list is upstream's, preserved because the frontend reads
        those four before anybody has signed in. What must hold is that nothing
        carrying member content is on it.
        """
        from open_notebook.identity.middleware import DEFAULT_EXCLUDED_PATHS

        registered = {r.path for r in ROUTES}
        excluded = {path for path in DEFAULT_EXCLUDED_PATHS if path in registered}
        assert excluded == set(EXCLUDED_FROM_AUTHENTICATION), (
            f"the middleware excludes {sorted(excluded)}; this file records "
            f"{sorted(EXCLUDED_FROM_AUTHENTICATION)}"
        )
        swept = {r.path for r in MUST_SCOPE}
        assert not (excluded & swept), (
            f"{sorted(excluded & swept)} carries member content and is exempt from "
            "authentication"
        )


class TestTheRoutesClosedToEverybody:
    """Not scoped, closed. A command record carries no Notebook, so there is
    nothing to scope against, and every capability these reached has a scoped
    route of its own."""

    @pytest.mark.parametrize(
        "method,path",
        tuple(CLOSED_TO_EVERYBODY),
        ids=[f"{m} {p}" for m, p in CLOSED_TO_EVERYBODY],
    )
    def test_they_answer_403_to_an_owner_as_well(self, client, method, path):
        route = BY_KEY[(method, path)]
        response = _drive(client, route, World(owner=CALLER_ID))
        assert response.status_code == 403, (
            f"{route.label} answered {response.status_code} to a member who owns "
            f"everything: {_payload(response)}"
        )
        assert _carries_no_content(response)


class TestOnlyOneRouteAssignsOwnership:
    """What the `CREATES_THE_SCOPE` carve-out actually depends on.

    `POST /api/notebooks` is allowlisted out of the scoping sweep because there is
    no existing record to scope against - it makes the record whose owner it sets.
    That behaviour is already covered:
    `tests/test_access_control.py::TestTheRoutesActuallyCallIt::
    test_a_created_notebook_is_owned_by_its_creator` drives the route and asserts
    the owner comes from the resolved caller rather than from the request body, so
    it is not repeated here.

    What is *not* covered there, and is this file's business, is the premise of the
    carve-out: that exactly one route assigns ownership. A second one would be a
    second place a Notebook can acquire an owner, and it would need the same
    scrutiny - so it has to fail here rather than inherit an allowlist entry
    written for a different route.
    """

    @staticmethod
    def _assigns_an_owner(route: Route) -> bool:
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(route.endpoint)))
        except (OSError, TypeError, SyntaxError):
            return False
        return any(
            keyword.arg == "owner"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
        )

    def test_exactly_one_route_assigns_an_owner(self):
        assigning = {r.key for r in ROUTES if self._assigns_an_owner(r)}
        assert assigning == set(CREATES_THE_SCOPE), (
            f"{sorted(assigning)} assign ownership; CREATES_THE_SCOPE allows only "
            f"{sorted(CREATES_THE_SCOPE)}. A new route that sets an owner needs its "
            "own decision about where that owner comes from, not this entry."
        )

    def test_the_owner_it_assigns_comes_from_the_resolved_caller(self):
        """Statically, as a complement to the behavioural test named above.

        The two fail differently and that is the point: the behavioural test
        catches the route assigning the wrong member, and this catches the *shape*
        that makes it possible - an owner read out of the request body, which a
        stand-in Notebook would happily accept.
        """
        route = BY_KEY[next(iter(CREATES_THE_SCOPE))]
        tree = ast.parse(textwrap.dedent(inspect.getsource(route.endpoint)))
        assigned = [
            ast.unparse(keyword.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "owner"
        ]
        assert assigned == ["member.id"], (
            f"{route.label} sets owner={assigned}; anything other than the resolved "
            "caller lets a client name the owner of the Notebook it creates"
        )


# ---------------------------------------------------------------------------
# Helpers used by the synchronous tests above
# ---------------------------------------------------------------------------


def _caller() -> Member:
    return Member(id=CALLER_ID, provider="test-suite", subject="test-subject")


def _run(coro):
    """Run one coroutine from a synchronous test.

    These tests are synchronous because they drive a TestClient in the same body,
    and mixing an async test with TestClient's own event loop is the kind of
    incidental complexity that makes a suite get deleted.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
