from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger

from api.models import NoteCreate, NoteResponse, NoteUpdate
from open_notebook.database.repository import repo_query
from open_notebook.domain.access import (
    accessible_notebook_records,
    require_note_read,
    require_note_write,
    require_notebook_read,
    require_notebook_write,
)
from open_notebook.domain.member import Member
from open_notebook.domain.notebook import Note
from open_notebook.exceptions import (
    InvalidInputError,
    NotFoundError,
    OpenNotebookError,
)
from open_notebook.identity import current_member

router = APIRouter()


@router.get("/notes", response_model=List[NoteResponse])
async def get_notes(
    notebook_id: Optional[str] = Query(None, description="Filter by notebook ID"),
    member: Member = Depends(current_member),
):
    """Get notes from notebooks this member can read.

    Without a notebook filter this used to return every note in the instance.
    It now spans the member's own notebooks instead - the filter narrows the
    scope, it does not create it.
    """
    try:
        if notebook_id:
            from open_notebook.domain.notebook import Notebook

            await require_notebook_read(member, notebook_id)
            notebook = await Notebook.get(notebook_id)
            notes = await notebook.get_notes()
        else:
            accessible = await accessible_notebook_records(member)
            rows = await repo_query(
                """
                SELECT * OMIT content, embedding
                FROM (SELECT VALUE in FROM artifact WHERE out IN $accessible_notebooks)
                ORDER BY updated DESC
                """,
                {"accessible_notebooks": accessible},
            )
            notes = [Note(**row) for row in rows]

        return [
            NoteResponse(
                id=note.id or "",
                title=note.title,
                content=note.content,
                note_type=note.note_type,
                created=str(note.created),
                updated=str(note.updated),
            )
            for note in notes
        ]
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error fetching notes: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching notes: {str(e)}")


@router.post("/notes", response_model=NoteResponse)
async def create_note(
    note_data: NoteCreate,
    member: Member = Depends(current_member),
):
    """Create a new note in a notebook the member owns.

    `notebook_id` was optional upstream and is now required. A note belonging to
    no notebook inherits access from nothing, so it would be unreadable and
    undeletable the moment it was created - the same ongoing orphaning migration
    25 had to sweep up. Access is checked before the note is written, not after,
    so a refused request leaves nothing behind.
    """
    try:
        if not note_data.notebook_id:
            raise InvalidInputError(
                "notebook_id is required: a note must belong to a notebook to be "
                "reachable"
            )
        await require_notebook_write(member, note_data.notebook_id)

        # Auto-generate title if not provided and it's an AI note
        title = note_data.title
        if not title and note_data.note_type == "ai" and note_data.content:
            from open_notebook.graphs.prompt import graph as prompt_graph

            prompt = "Based on the Note below, please provide a Title for this content, with max 15 words"
            # LangGraph accepts a partial state dict at runtime, but its typed
            # overloads require the full state type (langgraph typing limitation).
            result = await prompt_graph.ainvoke(  # type: ignore[call-overload]
                {
                    "input_text": note_data.content,
                    "prompt": prompt,
                }
            )
            title = result.get("output", "Untitled Note")

        # Validate note_type
        note_type: Optional[Literal["human", "ai"]] = None
        if note_data.note_type in ("human", "ai"):
            note_type = note_data.note_type  # type: ignore[assignment]
        elif note_data.note_type is not None:
            raise HTTPException(
                status_code=400, detail="note_type must be 'human' or 'ai'"
            )

        new_note = Note(
            title=title,
            content=note_data.content,
            note_type=note_type,
        )
        command_id = await new_note.save()

        await new_note.add_to_notebook(note_data.notebook_id)

        return NoteResponse(
            id=new_note.id or "",
            title=new_note.title,
            content=new_note.content,
            note_type=new_note.note_type,
            created=str(new_note.created),
            updated=str(new_note.updated),
            command_id=str(command_id) if command_id else None,
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
        logger.error(f"Error creating note: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error creating note: {str(e)}")


@router.get("/notes/{note_id}", response_model=NoteResponse)
async def get_note(
    note_id: str,
    member: Member = Depends(current_member),
):
    """Get a specific note by ID."""
    try:
        await require_note_read(member, note_id)
        note = await Note.get(note_id)

        return NoteResponse(
            id=note.id or "",
            title=note.title,
            content=note.content,
            note_type=note.note_type,
            created=str(note.created),
            updated=str(note.updated),
        )
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Note not found")
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error fetching note {note_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching note: {str(e)}")


@router.put("/notes/{note_id}", response_model=NoteResponse)
async def update_note(
    note_id: str,
    note_update: NoteUpdate,
    member: Member = Depends(current_member),
):
    """Update a note. Owner only (Requirements 5.2, 6.4)."""
    try:
        await require_note_write(member, note_id)
        note = await Note.get(note_id)

        # Update only provided fields
        if note_update.title is not None:
            note.title = note_update.title
        if note_update.content is not None:
            note.content = note_update.content
        if note_update.note_type is not None:
            if note_update.note_type in ("human", "ai"):
                note.note_type = note_update.note_type  # type: ignore[assignment]
            else:
                raise HTTPException(
                    status_code=400, detail="note_type must be 'human' or 'ai'"
                )

        command_id = await note.save()

        return NoteResponse(
            id=note.id or "",
            title=note.title,
            content=note.content,
            note_type=note.note_type,
            created=str(note.created),
            updated=str(note.updated),
            command_id=str(command_id) if command_id else None,
        )
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Note not found")
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error updating note {note_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error updating note: {str(e)}")


@router.delete("/notes/{note_id}")
async def delete_note(
    note_id: str,
    member: Member = Depends(current_member),
):
    """Delete a note.

    Owner of every notebook holding it: deleting a note removes it from all of
    them, so owning one is not permission to remove it from the others.
    """
    try:
        await require_note_write(member, note_id, every_notebook=True)
        note = await Note.get(note_id)

        await note.delete()

        return {"message": "Note deleted successfully"}
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Note not found")
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error deleting note {note_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error deleting note: {str(e)}")
