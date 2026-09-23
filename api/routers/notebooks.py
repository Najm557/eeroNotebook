from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger

from api.models import (
    NotebookCreate,
    NotebookDeletePreview,
    NotebookDeleteResponse,
    NotebookResponse,
    NotebookUpdate,
    RecentlyViewedResponse,
)
from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.domain.access import (
    accessible_notebook_records,
    require_notebook_read,
    require_notebook_write,
    require_source_write,
    role_from_owner,
)
from open_notebook.domain.member import Member
from open_notebook.domain.notebook import Notebook, Source
from open_notebook.exceptions import (
    InvalidInputError,
    NotFoundError,
    OpenNotebookError,
)
from open_notebook.identity import current_member

router = APIRouter()


def _last_viewed_sort_key(item: RecentlyViewedResponse) -> str:
    return item.last_viewed_at


async def _stamp_notebook_view(notebook_id: str) -> None:
    # Best-effort write-on-read: recording the view timestamp must never turn a
    # successful read into a 500. Log and move on if the stamp update fails.
    try:
        await repo_query(
            "UPDATE $notebook_id SET last_viewed_at = time::now();",
            {"notebook_id": ensure_record_id(notebook_id)},
        )
    except Exception as e:
        logger.warning(
            f"Failed to stamp last_viewed_at for notebook {notebook_id}: {e}"
        )


def _recently_viewed_notebook(row: dict) -> RecentlyViewedResponse:
    return RecentlyViewedResponse(
        type="notebook",
        id=str(row.get("id", "")),
        title=row.get("title") or row.get("name") or "Untitled notebook",
        last_viewed_at=str(row.get("last_viewed_at", "")),
    )


def _recently_viewed_source(row: dict) -> RecentlyViewedResponse:
    return RecentlyViewedResponse(
        type="source",
        id=str(row.get("id", "")),
        title=row.get("title") or "Untitled source",
        last_viewed_at=str(row.get("last_viewed_at", "")),
    )


@router.get("/notebooks", response_model=List[NotebookResponse])
async def get_notebooks(
    archived: Optional[bool] = Query(None, description="Filter by archived status"),
    order_by: str = Query("updated desc", description="Order by field and direction"),
    member: Member = Depends(current_member),
):
    """Get the notebooks this member owns or holds a Share on.

    Requirement 5.5. The scope is applied inside the query rather than to its
    result: filtering afterwards would still have read every member's rows, and
    the next person to add a projection or a count would be computing it over
    material the caller cannot see.
    """
    try:
        # Validate order_by against allowlist to prevent SurrealQL injection
        allowed_fields = {"name", "created", "updated"}
        allowed_directions = {"asc", "desc"}

        parts = order_by.strip().lower().split()
        if len(parts) == 1:
            if parts[0] not in allowed_fields:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid order_by field: '{order_by}'. Allowed fields: {', '.join(sorted(allowed_fields))}",
                )
            validated_order_by = parts[0]
        elif len(parts) == 2:
            if parts[0] not in allowed_fields or parts[1] not in allowed_directions:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid order_by: '{order_by}'. Allowed fields: {', '.join(sorted(allowed_fields))}. Allowed directions: asc, desc",
                )
            validated_order_by = f"{parts[0]} {parts[1]}"
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid order_by format: '{order_by}'. Expected 'field' or 'field direction'",
            )

        accessible = await accessible_notebook_records(member)

        # Build the query with counts
        query = f"""
            SELECT *,
            count(<-reference.in) as source_count,
            count(<-artifact.in) as note_count
            FROM notebook
            WHERE id IN $accessible_notebooks
            ORDER BY {validated_order_by}
        """

        result = await repo_query(query, {"accessible_notebooks": accessible})

        # Filter by archived status if specified
        if archived is not None:
            result = [nb for nb in result if nb.get("archived") == archived]

        # Each row already carries `owner`, so the caller's role is derived from it
        # rather than by re-asking `notebook_access` twice per row (spec task 6.3).
        #
        # A row whose role resolves to None is dropped, and that is a narrowing
        # rather than a new rule: an unowned notebook is reachable by nobody, so
        # `notebook_access` already answers 404 for it. Only a Share pointing at an
        # unowned notebook can produce one here, which task 6.2 logs and ignores,
        # and listing it would mean inventing a role for a notebook that 404s on
        # open - a claim the UI would act on.
        scoped = [(nb, role_from_owner(member, nb.get("owner"))) for nb in result]
        return [
            NotebookResponse(
                id=str(nb.get("id", "")),
                name=nb.get("name", ""),
                description=nb.get("description", ""),
                archived=nb.get("archived", False),
                created=str(nb.get("created", "")),
                updated=str(nb.get("updated", "")),
                source_count=nb.get("source_count", 0),
                note_count=nb.get("note_count", 0),
                role=role.value,
            )
            for nb, role in scoped
            if role is not None
        ]
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error fetching notebooks: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error fetching notebooks: {str(e)}"
        )


@router.post("/notebooks", response_model=NotebookResponse)
async def create_notebook(
    notebook: NotebookCreate,
    member: Member = Depends(current_member),
):
    """Create a new notebook owned by the member creating it (Requirement 5.1).

    The owner comes from the resolved caller and never from the request body:
    a member id accepted from a client would let anyone create a Notebook owned
    by somebody else, which is ownership assignment by the unauthenticated half
    of the request.
    """
    try:
        if not member.id:
            # Unreachable through the API - MemberAuthMiddleware refuses an
            # unresolved caller and Member.resolve persists before returning -
            # but a Notebook saved without an owner is reachable by nobody, so
            # this refuses rather than creating one.
            raise InvalidInputError(
                "Cannot create a notebook without a resolved member"
            )

        new_notebook = Notebook(
            name=notebook.name,
            description=notebook.description,
            owner=member.id,
        )
        await new_notebook.save()

        return NotebookResponse(
            id=new_notebook.id or "",
            name=new_notebook.name,
            description=new_notebook.description,
            archived=new_notebook.archived or False,
            created=str(new_notebook.created),
            updated=str(new_notebook.updated),
            source_count=0,  # New notebook has no sources
            note_count=0,  # New notebook has no notes
            # The creator is the owner by construction (Requirement 5.1).
            role="owner",
        )
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error creating notebook: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error creating notebook: {str(e)}"
        )


@router.get("/recently-viewed", response_model=List[RecentlyViewedResponse])
async def get_recently_viewed(
    limit: int = Query(12, ge=1, le=50, description="Number of items to return"),
    member: Member = Depends(current_member),
):
    """Get recently viewed notebooks and sources, newest first.

    Scoped to the member's own access on both halves. A source is included by
    the Notebooks that reference it, not by its own timestamp, since a source
    carries no owner of its own (Requirement 5.3).
    """
    try:
        accessible = await accessible_notebook_records(member)

        notebooks = await repo_query(
            """
            SELECT id, name AS title, last_viewed_at
            FROM notebook
            WHERE id IN $accessible_notebooks
              AND last_viewed_at != NONE AND last_viewed_at != NULL
            ORDER BY last_viewed_at DESC
            LIMIT $limit
            """,
            {"limit": limit, "accessible_notebooks": accessible},
        )
        sources = await repo_query(
            """
            SELECT id, title, last_viewed_at
            FROM (SELECT VALUE in FROM reference WHERE out IN $accessible_notebooks)
            WHERE last_viewed_at != NONE AND last_viewed_at != NULL
            ORDER BY last_viewed_at DESC
            LIMIT $limit
            """,
            {"limit": limit, "accessible_notebooks": accessible},
        )

        items = [
            *[_recently_viewed_notebook(nb) for nb in notebooks],
            *[_recently_viewed_source(src) for src in sources],
        ]
        items.sort(key=_last_viewed_sort_key, reverse=True)
        return items[:limit]
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        # Log full context server-side; return a generic message so internal
        # details are not leaked to clients.
        logger.exception(f"Error fetching recently viewed items: {e}")
        raise HTTPException(
            status_code=500, detail="Error fetching recently viewed items"
        )


@router.get(
    "/notebooks/{notebook_id}/delete-preview", response_model=NotebookDeletePreview
)
async def get_notebook_delete_preview(
    notebook_id: str,
    member: Member = Depends(current_member),
):
    """Get a preview of what will be deleted when this notebook is deleted."""
    try:
        # Owner only: the preview counts a notebook's notes and sources, so a
        # Viewer reading it would learn the size of a collection they cannot
        # delete anyway.
        await require_notebook_write(member, notebook_id)
        notebook = await Notebook.get(notebook_id)

        preview = await notebook.get_delete_preview()

        return NotebookDeletePreview(
            notebook_id=str(notebook.id),
            notebook_name=notebook.name,
            note_count=preview["note_count"],
            exclusive_source_count=preview["exclusive_source_count"],
            shared_source_count=preview["shared_source_count"],
        )
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error getting delete preview for notebook {notebook_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching notebook deletion preview: {str(e)}",
        )


@router.get("/notebooks/{notebook_id}", response_model=NotebookResponse)
async def get_notebook(
    notebook_id: str,
    member: Member = Depends(current_member),
):
    """Get a specific notebook by ID.

    The response carries the caller's role, which is what lets the UI stop
    offering a Viewer actions the API will refuse (spec task 6.3). It comes from
    the access check that already ran, not from a second lookup.
    """
    try:
        access = await require_notebook_read(member, notebook_id)

        # Query with counts for single notebook
        query = """
            SELECT *,
            count(<-reference.in) as source_count,
            count(<-artifact.in) as note_count
            FROM $notebook_id
        """
        result = await repo_query(query, {"notebook_id": ensure_record_id(notebook_id)})

        if not result:
            raise HTTPException(status_code=404, detail="Notebook not found")

        await _stamp_notebook_view(notebook_id)

        nb = result[0]
        return NotebookResponse(
            id=str(nb.get("id", "")),
            name=nb.get("name", ""),
            description=nb.get("description", ""),
            archived=nb.get("archived", False),
            created=str(nb.get("created", "")),
            updated=str(nb.get("updated", "")),
            source_count=nb.get("source_count", 0),
            note_count=nb.get("note_count", 0),
            role=access.granted_role.value,
        )
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error fetching notebook {notebook_id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error fetching notebook: {str(e)}"
        )


@router.put("/notebooks/{notebook_id}", response_model=NotebookResponse)
async def update_notebook(
    notebook_id: str,
    notebook_update: NotebookUpdate,
    member: Member = Depends(current_member),
):
    """Update a notebook. Owner only (Requirement 5.2)."""
    try:
        await require_notebook_write(member, notebook_id)
        notebook = await Notebook.get(notebook_id)

        # Update only provided fields. `owner` is deliberately not among them:
        # NotebookUpdate carries no owner field, and transferring ownership is
        # not a v1 capability. Saving without one cannot unown the notebook -
        # repo_update MERGEs, so the absent key leaves the stored owner alone.
        if notebook_update.name is not None:
            notebook.name = notebook_update.name
        if notebook_update.description is not None:
            notebook.description = notebook_update.description
        if notebook_update.archived is not None:
            notebook.archived = notebook_update.archived

        await notebook.save()

        # Query with counts after update
        query = """
            SELECT *,
            count(<-reference.in) as source_count,
            count(<-artifact.in) as note_count
            FROM $notebook_id
        """
        result = await repo_query(query, {"notebook_id": ensure_record_id(notebook_id)})

        if result:
            nb = result[0]
            return NotebookResponse(
                id=str(nb.get("id", "")),
                name=nb.get("name", ""),
                description=nb.get("description", ""),
                archived=nb.get("archived", False),
                created=str(nb.get("created", "")),
                updated=str(nb.get("updated", "")),
                source_count=nb.get("source_count", 0),
                note_count=nb.get("note_count", 0),
                # Only the owner reaches this route at all.
                role="owner",
            )

        # Fallback if query fails
        return NotebookResponse(
            id=notebook.id or "",
            name=notebook.name,
            description=notebook.description,
            archived=notebook.archived or False,
            created=str(notebook.created),
            updated=str(notebook.updated),
            source_count=0,
            note_count=0,
            role="owner",
        )
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error updating notebook {notebook_id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error updating notebook: {str(e)}"
        )


@router.post("/notebooks/{notebook_id}/sources/{source_id}")
async def add_source_to_notebook(
    notebook_id: str,
    source_id: str,
    member: Member = Depends(current_member),
):
    """Add an existing source to a notebook (create the reference).

    Both ends are owner-checked, and that is what stops the obvious escalation:
    with a read-only check on the source, a Viewer could link a source out of
    somebody else's notebook into one of their own, become an owner of a
    notebook containing it, and then delete it.
    """
    try:
        await require_notebook_write(member, notebook_id)
        await require_source_write(member, source_id)

        # Verify the notebook and source exist (raises NotFoundError -> 404)
        await Notebook.get(notebook_id)
        await Source.get(source_id)

        # Check if reference already exists (idempotency)
        existing_ref = await repo_query(
            "SELECT * FROM reference WHERE out = $source_id AND in = $notebook_id",
            {
                "notebook_id": ensure_record_id(notebook_id),
                "source_id": ensure_record_id(source_id),
            },
        )

        # If reference doesn't exist, create it
        if not existing_ref:
            await repo_query(
                "RELATE $source_id->reference->$notebook_id",
                {
                    "notebook_id": ensure_record_id(notebook_id),
                    "source_id": ensure_record_id(source_id),
                },
            )

        return {"message": "Source linked to notebook successfully"}
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Notebook or source not found")
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(
            f"Error linking source {source_id} to notebook {notebook_id}: {str(e)}"
        )
        raise HTTPException(
            status_code=500, detail=f"Error linking source to notebook: {str(e)}"
        )


@router.delete("/notebooks/{notebook_id}/sources/{source_id}")
async def remove_source_from_notebook(
    notebook_id: str,
    source_id: str,
    member: Member = Depends(current_member),
):
    """Remove a source from a notebook (delete the reference).

    Refuses when this is the source's last notebook. A source belonging to no
    notebook inherits access from nothing, so it becomes unreadable and
    undeletable through the API - the ongoing orphaning task 6.1 measured.
    """
    try:
        await require_notebook_write(member, notebook_id)

        # Verify the notebook exists (raises NotFoundError -> 404)
        await Notebook.get(notebook_id)

        remaining = await repo_query(
            "SELECT VALUE out FROM reference WHERE in = $source_id AND out != $notebook_id",
            {
                "notebook_id": ensure_record_id(notebook_id),
                "source_id": ensure_record_id(source_id),
            },
        )
        if not remaining:
            raise InvalidInputError(
                "This is the only notebook holding that source, so unlinking it "
                "would leave it in no notebook and reachable by nobody. Add it to "
                "another notebook first, or delete the source."
            )

        # Delete the reference record linking source to notebook
        await repo_query(
            "DELETE FROM reference WHERE out = $notebook_id AND in = $source_id",
            {
                "notebook_id": ensure_record_id(notebook_id),
                "source_id": ensure_record_id(source_id),
            },
        )

        return {"message": "Source removed from notebook successfully"}
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(
            f"Error removing source {source_id} from notebook {notebook_id}: {str(e)}"
        )
        raise HTTPException(
            status_code=500, detail=f"Error removing source from notebook: {str(e)}"
        )


@router.delete("/notebooks/{notebook_id}", response_model=NotebookDeleteResponse)
async def delete_notebook(
    notebook_id: str,
    delete_exclusive_sources: bool = Query(
        False,
        description=(
            "Whether to delete sources that belong only to this notebook. "
            "False is refused when the notebook holds such sources: they would "
            "belong to no notebook and become unreachable."
        ),
    ),
    member: Member = Depends(current_member),
):
    """
    Delete a notebook with cascade deletion. Owner only (Requirement 5.2).

    Always deletes all notes associated with the notebook.

    **The exclusive-source decision task 6.1 handed to 6.2.** Deleting a notebook
    makes SurrealDB delete the `reference` edges and keep the sources, so a source
    referenced only by this notebook survives in no notebook at all. Under
    inherited access that source is reachable by nobody: it cannot be read,
    edited or deleted through the API by any member, including the operator.
    Migration 25 swept the orphans that already existed and explicitly could not
    prevent the next one.

    Three options were on the table. Silently deleting exclusive sources
    regardless of the flag destroys content a member asked to keep, which is the
    failure migration 25 refused to accept when it adopted orphans rather than
    deleting them. Adopting them into a per-member recovery notebook works but
    invents product surface - an auto-created notebook nobody asked for - on a
    decision this task should not be making alone. So: **the request is refused
    when it would strand a source**, with 400 naming the remedy. Nothing is
    destroyed, nothing is invented, and the member who can fix it is told how.
    `delete_exclusive_sources=true` still deletes them, and a notebook whose
    sources are all shared with other notebooks still unlinks cleanly.

    `Notebook.delete()`'s own default stays at upstream's `False`. The rule
    belongs at the API boundary where a member is making the choice, not in a
    domain method also called by background jobs (Requirement 14.3).
    """
    try:
        await require_notebook_write(member, notebook_id)
        notebook = await Notebook.get(notebook_id)

        if not delete_exclusive_sources:
            preview = await notebook.get_delete_preview()
            if preview["exclusive_source_count"] > 0:
                raise InvalidInputError(
                    f"{preview['exclusive_source_count']} source(s) exist only in "
                    "this notebook. Keeping them would leave them in no notebook, "
                    "where no member can read or delete them. Delete them with the "
                    "notebook (delete_exclusive_sources=true), or add them to "
                    "another notebook first."
                )

        result = await notebook.delete(
            delete_exclusive_sources=delete_exclusive_sources
        )

        return NotebookDeleteResponse(
            message="Notebook deleted successfully",
            deleted_notes=result["deleted_notes"],
            deleted_sources=result["deleted_sources"],
            unlinked_sources=result["unlinked_sources"],
            deleted_chat_sessions=result["deleted_chat_sessions"],
        )
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error deleting notebook {notebook_id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error deleting notebook: {str(e)}"
        )
