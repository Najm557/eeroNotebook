"""Share management: grant, list and revoke one member's access to one Notebook.

Requirements 6.1, 6.2, 6.4 and 6.6. Spec task 6.3.

All three routes are owner-only through `require_notebook_write`, the same
function every other write on a Notebook already uses. Sharing is therefore not a
second access path: a Viewer gets 403 (they can see the Notebook, so hiding it
achieves nothing) and anybody else gets 404, so the routes do not confirm that a
Notebook exists to somebody with neither ownership nor a Share (Requirement 7.4).

Listing is owner-only rather than owner-and-Viewer on purpose. A Viewer reading
the list would learn the addresses of the Notebook's other Viewers, and the
requirements only ever run visibility upward to the owner - never sideways between
Viewers (Requirement 10.6, and the same reasoning).

There is no route that changes a Share. A grant has one role and revoking is the
only edit, so an update endpoint would exist only to carry a `role` the schema
refuses.
"""

from typing import List

from fastapi import APIRouter, Depends
from loguru import logger

from api.models import ShareCreate, ShareResponse
from open_notebook.domain.access import require_notebook_write
from open_notebook.domain.member import Member
from open_notebook.domain.share import (
    Share,
    grant_viewer,
    list_shares,
    member_for_email,
    normalise_email,
    revoke,
)
from open_notebook.exceptions import InvalidInputError, NotFoundError
from open_notebook.identity import current_member

router = APIRouter()


def _response(share: Share) -> ShareResponse:
    return ShareResponse(
        notebook_id=share.notebook_id,
        member_id=share.member_id,
        email=share.email,
        role="viewer",
        created=share.created,
    )


@router.get("/notebooks/{notebook_id}/shares", response_model=List[ShareResponse])
async def get_shares(
    notebook_id: str,
    member: Member = Depends(current_member),
):
    """Who this notebook is shared with. Owner only."""
    access = await require_notebook_write(member, notebook_id)
    return [_response(share) for share in await list_shares(access.notebook_id)]


@router.post("/notebooks/{notebook_id}/shares", response_model=ShareResponse)
async def create_share(
    notebook_id: str,
    payload: ShareCreate,
    member: Member = Depends(current_member),
):
    """Grant one named member Viewer access to this notebook. Owner only.

    Requirements 6.1 and 6.2. The role is not in the request and not an argument
    anywhere below - `grant_viewer` writes `viewer` as a literal, and migration
    25's `ASSERT $value = 'viewer'` refuses anything else.

    **Resolving the address.** It is matched against members of this instance, not
    against the identity provider's accounts, and the two are not the same set:
    `Member.resolve` creates the local row on first sign-in, so somebody the
    operator has created but who has never signed in here has no local record yet
    (task 5.6 found the table empty while the provider held three accounts). The
    404 below says exactly that rather than "no such person", because it cannot
    tell the two apart and guessing would be wrong either way.

    Task 5.4 refused to distinguish "no such user" from "wrong password" on the
    login path to avoid account enumeration, and this endpoint does weaken that,
    which is worth stating plainly rather than leaving implied. A successful share
    confirms that the address has signed in to this instance at least once. Three
    things bound it: the caller is already an authenticated member the operator
    created by hand, not an anonymous visitor; the refusal is *not* an oracle for
    absence, since "no local row" covers both "no account" and "has an account and
    has not signed in"; and there is no endpoint that lists members, so an owner
    can only test an address they already hold. The alternative - a share that
    names nobody and binds later - would mean a typo silently granting access to
    whoever registers that address next, which is worse than what it avoids.
    """
    access = await require_notebook_write(member, notebook_id)
    email = normalise_email(payload.email)

    target = await member_for_email(email)
    if target is None or not target.id:
        raise NotFoundError(
            "No member of this notebook server signs in with that address yet. "
            "If the account exists, ask them to sign in once and then share again."
        )

    if member.id and str(target.id) == str(member.id):
        raise InvalidInputError(
            "You already own this notebook, so it cannot be shared with you"
        )

    share = await grant_viewer(access.notebook_id, target.id)
    logger.info(
        "notebook {} shared with member {} as viewer", access.notebook_id, target.id
    )
    return _response(share)


@router.delete("/notebooks/{notebook_id}/shares/{member_id}")
async def delete_share(
    notebook_id: str,
    member_id: str,
    member: Member = Depends(current_member),
):
    """Revoke one member's access. Owner only.

    Requirement 6.5: the grant is deleted and the notebook's contents are not
    touched. Access ends on the revoked member's next request, because
    `notebook_access` reads the relation per request and caches nothing - there is
    no session to expire and nothing to invalidate.
    """
    access = await require_notebook_write(member, notebook_id)

    if not await revoke(access.notebook_id, member_id):
        raise NotFoundError("That member does not have access to this notebook")

    logger.info("access to notebook {} revoked for member {}", access.notebook_id, member_id)
    return {"message": "Access revoked"}
