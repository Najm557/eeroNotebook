from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from api.models import NoteResponse, SaveAsNoteRequest, SourceInsightResponse
from open_notebook.domain.access import (
    require_insight_read,
    require_insight_write,
    require_notebook_write,
)
from open_notebook.domain.member import Member
from open_notebook.domain.notebook import SourceInsight
from open_notebook.exceptions import (
    InvalidInputError,
    NotFoundError,
    OpenNotebookError,
)
from open_notebook.identity import current_member

router = APIRouter()


@router.get("/insights/{insight_id}", response_model=SourceInsightResponse)
async def get_insight(
    insight_id: str,
    member: Member = Depends(current_member),
):
    """Get a specific insight by ID.

    An insight carries no owner of its own. It inherits from its source, which
    inherits from the notebooks referencing it (Requirement 5.3).
    """
    try:
        await require_insight_read(member, insight_id)
        insight = await SourceInsight.get(insight_id)
        if not insight:
            raise HTTPException(status_code=404, detail="Insight not found")

        # Get source ID from the insight relationship
        source = await insight.get_source()

        return SourceInsightResponse(
            id=insight.id or "",
            source_id=source.id or "",
            insight_type=insight.insight_type,
            content=insight.content,
            created=insight.created.isoformat() if insight.created else None,
            updated=insight.updated.isoformat() if insight.updated else None,
        )
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error fetching insight {insight_id}: {str(e)}")
        raise HTTPException(status_code=500, detail="Error fetching insight")


@router.delete("/insights/{insight_id}")
async def delete_insight(
    insight_id: str,
    member: Member = Depends(current_member),
):
    """Delete a specific insight. Owner only."""
    try:
        await require_insight_write(member, insight_id)
        insight = await SourceInsight.get(insight_id)
        if not insight:
            raise HTTPException(status_code=404, detail="Insight not found")

        await insight.delete()

        return {"message": "Insight deleted successfully"}
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error deleting insight {insight_id}: {str(e)}")
        raise HTTPException(status_code=500, detail="Error deleting insight")


@router.post("/insights/{insight_id}/save-as-note", response_model=NoteResponse)
async def save_insight_as_note(
    insight_id: str,
    request: SaveAsNoteRequest,
    member: Member = Depends(current_member),
):
    """Convert an insight to a note.

    `notebook_id` was optional upstream - `SourceInsight.save_as_note()` accepts
    None and writes a note attached to nothing. That is one of the two paths that
    kept producing the orphaned content migration 25 had to sweep up, and under
    inherited access the note would be unreachable the moment it existed. It is
    now required, and must name a notebook this member owns.
    """
    try:
        if not request.notebook_id:
            raise InvalidInputError(
                "notebook_id is required: a note must belong to a notebook to be "
                "reachable"
            )
        await require_insight_read(member, insight_id)
        await require_notebook_write(member, request.notebook_id)

        insight = await SourceInsight.get(insight_id)
        if not insight:
            raise HTTPException(status_code=404, detail="Insight not found")

        # Use the existing save_as_note method from the domain model
        note = await insight.save_as_note(request.notebook_id)

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
        raise HTTPException(status_code=404, detail="Notebook not found")
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error saving insight {insight_id} as note: {str(e)}")
        raise HTTPException(
            status_code=500, detail="Error saving insight as note"
        )
