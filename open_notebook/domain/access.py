"""What a member may reach. One module, consulted by every route.

Division of labour with the identity boundary is deliberate and sharp:
`open_notebook.identity` answers *who is calling*, and this module answers *what
that caller may reach*. A route depends on `current_member` for the first and
calls one function from here for the second.

Why one module rather than a filter per router: ownership lives on `notebook` and
nowhere else (migration 25), so every access question reduces to "what is this
member's role on that Notebook". Twenty-two routers each writing their own
version of that question would be twenty-two things to keep in agreement across
an upstream merge, and the one that drifts is a data leak rather than a bug.

Three rules the whole module is built to keep:

1. **Fail closed.** `notebook.owner = NONE` means reachable by *nobody*, never by
   everybody — migration 25 chose an optional field with that cost attached, and
   the dangerous reading of "inherit" is the opposite one. A member with no id
   reaches nothing. An unresolvable Notebook reaches nothing.

2. **A failed lookup is never an empty answer.** Every database read here raises
   `AccessUnavailableError` (503) on failure rather than returning "no access".
   Task 5.3 established the same rule for the identity store: "the database is
   down" must not be indistinguishable from "you own nothing", because the second
   silently passes a check that was meant to run.

3. **Absence of access reads as absence of the record.** No access answers
   `NotFoundError` (404), not 403, so a response cannot confirm that a Notebook
   exists to somebody who has neither ownership nor a Share (Requirement 7.4).
   `AccessDeniedError` (403) is reserved for a caller who can already see the
   record and is refused a *write* — a Viewer, for whom hiding existence is moot
   and a 404 would just read as a bug.

Sources, notes, insights and chat sessions carry no owner of their own. They
resolve to their Notebooks through the existing `reference`, `artifact` and
`refers_to` relations and inherit from there (Requirement 5.3).
"""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Sequence

from loguru import logger

from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.domain.member import Member
from open_notebook.exceptions import (
    AccessDeniedError,
    AccessUnavailableError,
    NotFoundError,
)


class Role(str, Enum):
    """A member's role on one Notebook.

    Two values and no more. Requirement 6.6 is met structurally rather than by
    policy: `share.role` is `ASSERT $value = 'viewer'` in the schema, so there is
    no third role to widen to without a migration.
    """

    OWNER = "owner"
    VIEWER = "viewer"


@dataclass(frozen=True)
class NotebookAccess:
    """The answer to "what is this member's role on this Notebook"."""

    notebook_id: str
    role: Optional[Role]

    @property
    def can_read(self) -> bool:
        """Owner or Viewer. Requirement 6.3."""
        return self.role is not None

    @property
    def can_write(self) -> bool:
        """Owner only. Requirements 5.2 and 6.4."""
        return self.role is Role.OWNER

    @property
    def granted_role(self) -> Role:
        """The resolved role, for a caller that has already established there is one.

        `role` is optional because "no access" is an answer this type must be able
        to give, so a route wanting to report the caller's role (spec task 6.3)
        would otherwise have to narrow it with a fallback - and the only plausible
        fallback, `viewer`, would label a stranger a reader if the narrowing were
        ever reached. This refuses instead, which is the fail-closed answer and
        matches what `require_notebook_read` would already have raised.
        """
        if self.role is None:
            raise NotFoundError("Notebook not found")
        return self.role


# Which relation carries a kind of content to its Notebooks, and which end of it
# points at the content. Kept as data rather than as four near-identical
# functions so that a new kind of Notebook-owned content (task 8's
# Study_Artifacts) is one entry rather than one more copy of the same query.
_CONTENT_RELATIONS: Dict[str, str] = {
    "source": "reference",
    "note": "artifact",
    "chat_session": "refers_to",
}


async def _read(query: str, params: Optional[dict] = None) -> List:
    """Run an access query, or refuse the request.

    Never returns an empty result for a failed read. A caller that cannot tell
    "the database is unreachable" from "this member has no access" will treat the
    outage as a passed check.
    """
    try:
        return await repo_query(query, params)
    except Exception as exc:
        logger.error("access lookup failed: {}", exc)
        raise AccessUnavailableError(
            "Access could not be determined because the database is unavailable"
        ) from exc


def _qualify(table: str, record_id: str) -> str:
    """Accept both `chat_session:abc` and a bare `abc` for a known table.

    Several routes take an id in either form - `_chat_shared.normalize_record_id`
    exists for exactly that - and an access check that only understood one form
    would raise on the other. Raising would be safe here but useless: the route
    would then 500 where it used to work, which is how a check gets removed.
    """
    return record_id if ":" in record_id else f"{table}:{record_id}"


def _member_record(member: Member):
    """The member's record id, or None when it has none.

    A `Member` with no id is not an error — `Member.resolve` always persists
    before returning one, but a hand-constructed member (a test, a future code
    path) would have none. It reaches nothing, which is the fail-closed answer,
    and never compares equal to an unowned Notebook's `NONE`.
    """
    if not member.id:
        return None
    return ensure_record_id(member.id)


async def notebook_access(member: Member, notebook_id: str) -> NotebookAccess:
    """Resolve one member and one Notebook id to a role, or to no role.

    The single database-backed decision in this module. Everything else composes
    it. Two short queries rather than one clever one: the owner check answers the
    common case on its own, and the Share check only runs when it does not.
    """
    if not notebook_id:
        return NotebookAccess(notebook_id="", role=None)

    caller = _member_record(member)
    notebook_id = _qualify("notebook", notebook_id)

    try:
        notebook = ensure_record_id(notebook_id)
    except Exception:
        # An unparseable id is not a Notebook anyone can reach. Answering "no
        # access" here rather than raising keeps a malformed id from being
        # distinguishable from a well-formed one that does not exist.
        logger.debug("unparseable notebook id in access check: {!r}", notebook_id)
        return NotebookAccess(notebook_id=str(notebook_id), role=None)

    # `id` is selected alongside `owner` so that a Notebook which exists but is
    # unowned is distinguishable from one that does not exist. `SELECT VALUE
    # owner` would collapse both to an empty-ish answer.
    rows = await _read("SELECT id, owner FROM $notebook", {"notebook": notebook})
    if not rows:
        return NotebookAccess(notebook_id=str(notebook_id), role=None)

    owner = rows[0].get("owner")
    # Both halves of this comparison can be None - an unowned Notebook and a
    # member with no id - and they must not match. That equality is the whole
    # fail-closed rule, so it is written explicitly rather than left to `==`.
    if caller is not None and owner is not None and str(owner) == str(caller):
        return NotebookAccess(notebook_id=str(notebook_id), role=Role.OWNER)

    if caller is None:
        return NotebookAccess(notebook_id=str(notebook_id), role=None)

    shares = await _read(
        "SELECT VALUE role FROM share WHERE in = $member AND out = $notebook LIMIT 1",
        {"member": caller, "notebook": notebook},
    )
    if shares and shares[0] == Role.VIEWER.value:
        # An unowned Notebook stays unreachable even if a Share points at it.
        # `owner = NONE` means nobody, and a Share is an addition to an owner's
        # access rather than a replacement for it.
        if owner is None:
            logger.warning(
                "share on an unowned notebook {} ignored; an unowned Notebook is "
                "reachable by nobody",
                notebook_id,
            )
            return NotebookAccess(notebook_id=str(notebook_id), role=None)
        return NotebookAccess(notebook_id=str(notebook_id), role=Role.VIEWER)

    return NotebookAccess(notebook_id=str(notebook_id), role=None)


def role_from_owner(member: Member, owner) -> Optional[Role]:
    """The member's role on a Notebook whose `owner` column is already in hand.

    The same decision as `notebook_access` without the two reads, for a list route
    that has already selected `owner` on every row it is about to return. Running
    `notebook_access` per row would be two more queries per Notebook to re-derive
    something the row carries, and task 6.2 deliberately kept list scoping to two
    indexed reads.

    Fail-closed the same way, and that matters more here than it looks: an unowned
    Notebook answers None rather than VIEWER, so a route cannot label a Notebook
    nobody can reach as readable. VIEWER is only correct for a row that reached a
    list because a Share put it there, which is why this is a helper for that
    caller and not a general answer - it cannot tell "shared with me" from "not
    shared with me" on its own.
    """
    if not member.id or owner is None:
        return None
    if str(owner) == str(ensure_record_id(member.id)):
        return Role.OWNER
    return Role.VIEWER


async def accessible_notebook_ids(member: Member) -> List[str]:
    """Every Notebook id this member owns or holds a Share on.

    Requirement 5.5, and what every list route binds into its own query instead
    of filtering rows after the fact. Two indexed reads unioned in Python rather
    than one query with a graph traversal in its WHERE clause: each half is
    separately readable, and the fail-closed branch for a member with no id is
    visible here rather than buried in SurrealQL where `owner = NONE` would match
    every unowned Notebook.
    """
    caller = _member_record(member)
    if caller is None:
        return []

    owned = await _read(
        "SELECT VALUE id FROM notebook WHERE owner = $member", {"member": caller}
    )
    shared = await _read(
        "SELECT VALUE out FROM share WHERE in = $member", {"member": caller}
    )

    seen: Dict[str, None] = {}
    for notebook_id in [*owned, *shared]:
        if notebook_id is None:
            continue
        seen.setdefault(str(notebook_id), None)
    return list(seen)


async def accessible_notebook_records(member: Member) -> List:
    """`accessible_notebook_ids` as record ids, ready to bind into a query."""
    return [ensure_record_id(nb_id) for nb_id in await accessible_notebook_ids(member)]


async def require_notebook_read(member: Member, notebook_id: str) -> NotebookAccess:
    """Owner or Viewer, or 404."""
    access = await notebook_access(member, notebook_id)
    if not access.can_read:
        raise NotFoundError("Notebook not found")
    return access


async def require_notebook_write(member: Member, notebook_id: str) -> NotebookAccess:
    """Owner only.

    A Viewer gets 403 rather than 404: they can already see the Notebook, so
    hiding its existence achieves nothing and a 404 would read as a defect.
    Anybody else gets 404 (Requirement 7.4).
    """
    access = await notebook_access(member, notebook_id)
    if access.can_write:
        return access
    if access.can_read:
        raise AccessDeniedError(
            "Only the notebook owner can change this notebook or its contents"
        )
    raise NotFoundError("Notebook not found")


async def require_notebooks_write(
    member: Member, notebook_ids: Sequence[str]
) -> List[NotebookAccess]:
    """Owner of every Notebook named. Used where a request lists several.

    Every one, not any one: a request that creates a Source in Notebooks A and B
    writes into both, so holding A is not permission to write into B.
    """
    return [await require_notebook_write(member, nb_id) for nb_id in notebook_ids]


async def _notebook_ids_for(kind: str, record_id: str) -> List[str]:
    """The Notebooks a piece of content belongs to.

    An empty list means the content is orphaned - it belongs to no Notebook, so
    no access check can grant it. That is the fail-closed answer and not an
    error: migration 25 swept the orphans that existed, and the routes that could
    create new ones now refuse to.
    """
    relation = _CONTENT_RELATIONS[kind]
    try:
        record = ensure_record_id(_qualify(kind, record_id))
    except Exception:
        logger.debug("unparseable {} id in access check: {!r}", kind, record_id)
        return []

    rows = await _read(
        f"SELECT VALUE out FROM {relation} WHERE in = $record", {"record": record}
    )
    return [str(row) for row in rows if row is not None]


async def _require_content(
    member: Member,
    kind: str,
    record_id: str,
    *,
    write: bool,
    every_notebook: bool = False,
    missing: str = "Not found",
) -> List[str]:
    """Resolve content to its Notebooks and apply the Notebook's access.

    `every_notebook` is for an operation whose effect is not confined to one
    Notebook. Deleting a Source removes it from every Notebook that references
    it, so owning one of them is not enough; editing a Source in place is
    confined to content the caller put there, so owning one is.
    """
    notebook_ids = await _notebook_ids_for(kind, record_id)
    if not notebook_ids:
        raise NotFoundError(missing)

    accesses = [await notebook_access(member, nb_id) for nb_id in notebook_ids]
    readable = [a for a in accesses if a.can_read]
    if not readable:
        raise NotFoundError(missing)

    if not write:
        return notebook_ids

    writable = [a for a in accesses if a.can_write]
    if every_notebook:
        if len(writable) != len(accesses):
            raise AccessDeniedError(
                f"This {kind.replace('_', ' ')} also belongs to a notebook you do "
                "not own, so it cannot be deleted from here"
            )
        return notebook_ids

    if not writable:
        raise AccessDeniedError(
            f"Only the notebook owner can change this {kind.replace('_', ' ')}"
        )
    return notebook_ids


async def require_source_read(member: Member, source_id: str) -> List[str]:
    return await _require_content(
        member, "source", source_id, write=False, missing="Source not found"
    )


async def require_source_write(
    member: Member, source_id: str, *, every_notebook: bool = False
) -> List[str]:
    return await _require_content(
        member,
        "source",
        source_id,
        write=True,
        every_notebook=every_notebook,
        missing="Source not found",
    )


async def require_note_read(member: Member, note_id: str) -> List[str]:
    return await _require_content(
        member, "note", note_id, write=False, missing="Note not found"
    )


async def require_note_write(
    member: Member, note_id: str, *, every_notebook: bool = False
) -> List[str]:
    return await _require_content(
        member,
        "note",
        note_id,
        write=True,
        every_notebook=every_notebook,
        missing="Note not found",
    )


async def require_chat_session_read(member: Member, session_id: str) -> List[str]:
    """A chat session hangs off either a Notebook or a Source via `refers_to`.

    Both are resolved: `refers_to` carries notebook-scoped sessions (chat.py) and
    source-scoped ones (source_chat.py), and which one a given session is cannot
    be told from the relation alone.
    """
    return await _require_session(member, session_id, write=False)


async def require_chat_session_write(member: Member, session_id: str) -> List[str]:
    return await _require_session(member, session_id, write=True)


async def _require_session(
    member: Member, session_id: str, *, write: bool
) -> List[str]:
    targets = await _notebook_ids_for("chat_session", session_id)
    if not targets:
        raise NotFoundError("Session not found")

    notebook_ids: List[str] = []
    for target in targets:
        if target.startswith("source:"):
            notebook_ids.extend(await _notebook_ids_for("source", target))
        else:
            notebook_ids.append(target)

    if not notebook_ids:
        raise NotFoundError("Session not found")

    accesses = [await notebook_access(member, nb_id) for nb_id in notebook_ids]
    if not any(a.can_read for a in accesses):
        raise NotFoundError("Session not found")
    if write and not any(a.can_write for a in accesses):
        raise AccessDeniedError("Only the notebook owner can change this session")
    return notebook_ids


async def _source_for_insight(insight_id: str) -> Optional[str]:
    try:
        insight = ensure_record_id(_qualify("source_insight", insight_id))
    except Exception:
        logger.debug("unparseable insight id in access check: {!r}", insight_id)
        return None
    rows = await _read("SELECT VALUE source FROM $insight", {"insight": insight})
    if not rows or rows[0] is None:
        return None
    return str(rows[0])


async def require_insight_read(member: Member, insight_id: str) -> List[str]:
    """An insight inherits from its Source, which inherits from its Notebooks."""
    source_id = await _source_for_insight(insight_id)
    if source_id is None:
        raise NotFoundError("Insight not found")
    return await _require_content(
        member, "source", source_id, write=False, missing="Insight not found"
    )


async def require_insight_write(member: Member, insight_id: str) -> List[str]:
    source_id = await _source_for_insight(insight_id)
    if source_id is None:
        raise NotFoundError("Insight not found")
    return await _require_content(
        member, "source", source_id, write=True, missing="Insight not found"
    )
