"""Creating, listing and revoking Shares. The write side of the access relation.

Requirements 6.1, 6.2, 6.4 and 6.6. Spec task 6.3.

The division of labour with `open_notebook.domain.access` is the same shape as the
one between `identity` and `access`: this module *manages* grants and access.py
*consults* them. `notebook_access` remains the only place that decides what a
member may reach, and nothing here answers an access question - the router
establishes ownership through `require_notebook_write` before calling in, exactly
as every other write on a Notebook does. So there is one enforcement path, not
two.

**Requirement 6.6 is met by the shape of these functions, not by a rule.** No
call takes a collection: `grant_viewer` grants one member access to one Notebook
and there is no plural form of it anywhere in the module. `role` is written as a
literal inside the SurrealQL and is never a parameter, so no value a caller
supplied can reach it, and the schema's `ASSERT $value = 'viewer'` (migration 25)
is the backstop rather than the mechanism.

**Revocation deletes the grant and nothing else** (Requirement 6.5). The delete
targets the `share` relation by both endpoints and touches no content table, so a
revoked member loses access on their next request while the Notebook's Sources,
notes and - once task 7.1 lands - the revoked member's own Study_Progress are
left exactly as they were.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from loguru import logger

from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.domain.access import Role
from open_notebook.domain.member import Member
from open_notebook.exceptions import AccessUnavailableError, InvalidInputError

# The longest address the RFCs allow, used only to refuse absurd input early.
MAX_EMAIL_LENGTH = 320

# The only role a Share can carry. This constant is documentation: `grant_viewer`
# writes the literal into its statement rather than interpolating this value, so
# the SurrealQL is the guarantee and a test asserts the two still agree.
VIEWER_ROLE = Role.VIEWER.value


@dataclass(frozen=True)
class Share:
    """One member's Viewer access to one Notebook.

    `role` is carried rather than assumed so that a row which somehow held
    another value would be visible to a caller instead of being relabelled on the
    way out.
    """

    notebook_id: str
    member_id: str
    email: Optional[str]
    role: str
    created: str


async def _query(sql: str, params: Optional[dict], action: str) -> List:
    """Run a share query, or refuse the request.

    Never answers a failed read with an empty result. "Nobody has access to this
    Notebook" and "the database is unreachable" must not be the same response:
    the first is something an owner would act on, and the second silently makes
    every Share look revoked. Same rule task 5.3 set for the identity store and
    task 6.2 set for access decisions, and the same 503.
    """
    try:
        return await repo_query(sql, params)
    except Exception as exc:
        logger.error("share {} failed: {}", action, exc)
        raise AccessUnavailableError(
            f"Could not {action} because the database is unavailable"
        ) from exc


def _member_record(member_id: str):
    """A member id as a record id, or None when it cannot be one.

    Accepts both `member:abc` and a bare `abc`, matching what the access module
    does for content ids, and answers None rather than raising for anything
    unparseable. A malformed id in the path of a revoke should read as "no such
    share" - the same answer as a well-formed id that holds none - not as a 500.
    """
    if not member_id:
        return None
    qualified = member_id if ":" in member_id else f"member:{member_id}"
    try:
        return ensure_record_id(qualified)
    except Exception:
        logger.debug("unparseable member id in a share request: {!r}", member_id)
        return None


def normalise_email(raw: str) -> str:
    """Trim and lower-case an address, or refuse it.

    Deliberately shallow. This is not address validation - the identity provider
    owns that, and an address only reaches a Share by matching a member who has
    already signed in with it. It exists to turn a typo into a 400 the owner can
    read rather than a lookup that quietly matches nothing, and to make the
    comparison case-insensitive in one place.
    """
    email = (raw or "").strip().lower()
    if not email:
        raise InvalidInputError("An email address is required to share a notebook")
    if len(email) > MAX_EMAIL_LENGTH:
        raise InvalidInputError("That email address is too long to be valid")
    if any(char.isspace() for char in email):
        raise InvalidInputError("An email address cannot contain spaces")
    local, _, domain = email.partition("@")
    if not local or not domain or "@" in domain:
        raise InvalidInputError(
            "That does not look like an email address. Use the address the person "
            "signs in with."
        )
    return email


async def member_for_email(email: str) -> Optional[Member]:
    """The member who signs in with this address, or None.

    **This resolves against eeroNotebook's own `member` table and does not ask the
    identity provider.** That has a consequence worth stating: `Member.resolve`
    creates the local row on first sign-in (task 5.6 found the table empty while
    the provider held three accounts), so an address belonging to someone who has
    never signed in here resolves to None even though the account exists. The
    caller turns that into "ask them to sign in once, then share" rather than
    "no such person", because those are genuinely different and the second would
    be a guess.

    The alternative was asking the provider's admin API for the address, which
    would let an owner share ahead of a first sign-in and would return the
    provider's `subject` so the local row matched later. It was rejected for this
    task: it puts a second provider-specific integration on the sharing path, and
    it could not be verified against a live provider from here - an unverifiable
    call in the path that grants access to private material is a worse trade than
    a documented "sign in once first".

    Creating a placeholder member from the address alone was also rejected, and it
    is the tempting wrong answer: `member` is keyed on (provider, subject) where
    subject is the provider's own user id, never the address, so a row invented
    here would never be the row `Member.resolve` finds at first sign-in. The Share
    would point at a member nobody can ever be - an owner told "shared" and a
    Viewer who never gets in.

    Matching is case-insensitive against `member.email`, which is the provider's
    claim as stored at sign-in. Verified against a real database rather than
    assumed, including a mixed-case stored address.

    The `email != NONE` guard is deliberate but was measured not to be strictly
    required: SurrealDB 2.6.5 returns NONE from `string::lowercase(NONE)` rather
    than failing the query, so the admin member - which has no address at all - did
    not break the lookup without it. Kept because it makes an address-less member
    unmatchable by construction rather than by a coincidence of NONE propagation.
    """
    rows = await _query(
        "SELECT * FROM member WHERE email != NONE AND string::lowercase(email) = $email LIMIT 1",
        {"email": email},
        "look up that member",
    )
    if not rows:
        return None
    return Member(**rows[0])


async def list_shares(notebook_id: str) -> List[Share]:
    """Every Share on one Notebook, oldest first.

    Two queries rather than one traversal: the relation rows, then the members
    they point at. `SELECT * FROM share` keeps `created` in the projection, which
    `ORDER BY created` needs - an explicit projection that omitted it is refused
    by SurrealDB ("Missing order idiom"), which task 6.2 hit the hard way.
    """
    notebook = ensure_record_id(notebook_id)
    rows = await _query(
        "SELECT * FROM share WHERE out = $notebook ORDER BY created",
        {"notebook": notebook},
        "list the people this notebook is shared with",
    )
    if not rows:
        return []

    emails = await _emails_for([row.get("in") for row in rows])
    return [_share(row, emails) for row in rows]


async def _emails_for(member_ids: List) -> Dict[str, Optional[str]]:
    """Address per member id, for labelling Share rows.

    The ids are converted back to record ids before being bound. `repo_query`
    renders every RecordID in a result as a string on the way out, and `id IN
    $members` given a list of strings matches nothing - silently, as an empty
    result rather than an error, so every Share would come back unlabelled. Found
    by running this against a real database; the string form reads correctly
    everywhere else in this module, which is what makes it easy to miss.
    """
    wanted = [
        ensure_record_id(member_id) for member_id in member_ids if member_id is not None
    ]
    if not wanted:
        return {}
    members = await _query(
        "SELECT id, email FROM member WHERE id IN $members",
        {"members": wanted},
        "read the members this notebook is shared with",
    )
    return {str(m.get("id")): m.get("email") for m in members}


def _share(row: dict, emails: Dict[str, Optional[str]]) -> Share:
    member_id = str(row.get("in"))
    return Share(
        notebook_id=str(row.get("out")),
        member_id=member_id,
        # None when the member row is gone. Migration 25's cascade event deletes a
        # Share with its member, so this should not happen - carrying the id
        # without a label beats dropping the row, because an owner can still
        # revoke a grant we cannot name.
        email=emails.get(member_id),
        role=str(row.get("role")),
        created=str(row.get("created", "")),
    )


async def find_share(notebook_id: str, member_id: str) -> Optional[Share]:
    """One member's Share on one Notebook, or None."""
    member = _member_record(member_id)
    if member is None:
        return None
    notebook = ensure_record_id(notebook_id)
    rows = await _query(
        "SELECT * FROM share WHERE in = $member AND out = $notebook LIMIT 1",
        {"member": member, "notebook": notebook},
        "check that share",
    )
    if not rows:
        return None
    return _share(rows[0], await _emails_for([rows[0].get("in")]))


async def grant_viewer(notebook_id: str, member_id: str) -> Share:
    """Grant one member Viewer access to one Notebook (Requirements 6.1, 6.2).

    Idempotent: granting access somebody already holds returns the existing Share
    rather than failing, because the UNIQUE index on (in, out) would reject the
    second RELATE and an owner clicking twice has not done anything wrong. The
    index is also what makes this safe under concurrency - the loser of a race
    re-reads instead of both writes succeeding, the same shape as
    `Member.resolve`.

    `role` is the literal `'viewer'` in the statement. It is not a parameter and
    there is no argument for it, so no client-supplied value can widen a grant.
    """
    member = _member_record(member_id)
    if member is None:
        raise InvalidInputError("That is not a member of this notebook server")

    existing = await find_share(notebook_id, member_id)
    if existing is not None:
        return existing

    notebook = ensure_record_id(notebook_id)
    try:
        await repo_query(
            "RELATE $member->share->$notebook SET role = 'viewer'",
            {"member": member, "notebook": notebook},
        )
    except Exception as exc:
        # Most likely the UNIQUE index rejecting a concurrent grant. Re-read
        # before calling it a failure: if the Share now exists, the other request
        # won and its row is the correct one.
        racing = await find_share(notebook_id, member_id)
        if racing is not None:
            logger.debug(
                "share insert lost a race for {} on {}; using the existing grant",
                member_id,
                notebook_id,
            )
            return racing
        logger.error("could not grant access to {} on {}: {}", member_id, notebook_id, exc)
        raise AccessUnavailableError(
            "Could not share the notebook because the database is unavailable"
        ) from exc

    granted = await find_share(notebook_id, member_id)
    if granted is None:
        # A RELATE that reported success and stored nothing. Refused rather than
        # returning a Share object describing a grant that does not exist.
        raise AccessUnavailableError("The share could not be recorded")
    return granted


async def revoke(notebook_id: str, member_id: str) -> bool:
    """End one member's access, leaving the Notebook's contents untouched.

    Requirement 6.5. The statement deletes from `share` and names both endpoints,
    so it can remove at most the one grant and cannot reach any content table.
    Access ends on the member's next request because `notebook_access` reads this
    relation per request and caches nothing.

    Returns False when there was no Share to revoke, so the caller can answer that
    honestly rather than reporting a revocation that did not happen.
    """
    member = _member_record(member_id)
    if member is None:
        return False
    notebook = ensure_record_id(notebook_id)
    deleted = await _query(
        "DELETE share WHERE in = $member AND out = $notebook RETURN BEFORE",
        {"member": member, "notebook": notebook},
        "revoke that share",
    )
    return bool(deleted)
