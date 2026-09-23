from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from api.command_service import CommandService
from api.models import EmbedRequest, EmbedResponse
from open_notebook.ai.models import model_manager
from open_notebook.domain.access import require_note_write, require_source_write
from open_notebook.domain.member import Member
from open_notebook.domain.notebook import Note, Source
from open_notebook.exceptions import (
    NotFoundError,
    OpenNotebookError,
)
from open_notebook.identity import current_member

router = APIRouter()


@router.post("/embed", response_model=EmbedResponse)
async def embed_content(
    embed_request: EmbedRequest,
    member: Member = Depends(current_member),
):
    """Embed content for vector search. Owner only.

    Addresses a source or a note directly by id, so it is scoped through that
    record's notebooks like any other content route. Owner rather than Viewer: it
    writes embeddings and spends inference on the shared gateway.
    """
    try:
        # Check if embedding model is available
        if not await model_manager.get_embedding_model():
            raise HTTPException(
                status_code=400,
                detail="No embedding model configured. Please configure one in the Models section.",
            )

        item_id = embed_request.item_id
        item_type = embed_request.item_type.lower()

        # Validate item type
        if item_type not in ["source", "note"]:
            raise HTTPException(
                status_code=400, detail="Item type must be either 'source' or 'note'"
            )

        # Before either branch, so the async path cannot queue a job for content
        # the member cannot reach.
        if item_type == "source":
            await require_source_write(member, item_id)
        else:
            await require_note_write(member, item_id)

        # Branch based on processing mode
        if embed_request.async_processing:
            # ASYNC PATH: Submit command for background processing
            logger.info(f"Using async processing for {item_type} {item_id}")

            try:
                # Import commands to ensure they're registered
                import commands.embedding_commands  # noqa: F401

                # Submit type-specific command
                if item_type == "source":
                    command_name = "embed_source"
                    command_input = {"source_id": item_id}
                else:  # note
                    command_name = "embed_note"
                    command_input = {"note_id": item_id}

                command_id = await CommandService.submit_command_job(
                    "open_notebook",
                    command_name,
                    command_input,
                )

                logger.info(f"Submitted async {command_name} command: {command_id}")

                return EmbedResponse(
                    success=True,
                    message="Embedding queued for background processing",
                    item_id=item_id,
                    item_type=item_type,
                    command_id=command_id,
                )

            except Exception as e:
                logger.error(f"Failed to submit async embedding command: {e}")
                raise HTTPException(
                    status_code=500, detail=f"Failed to queue embedding: {str(e)}"
                )

        else:
            # DOMAIN MODEL PATH: Submit job via domain model convenience methods
            # These methods internally call submit_command() - still fire-and-forget
            logger.info(f"Using domain model path for {item_type} {item_id}")

            command_id = None

            # Get the item and submit embedding job
            if item_type == "source":
                source_item = await Source.get(item_id)

                # Submit embed_source job (returns command_id for tracking)
                command_id = await source_item.vectorize()
                message = "Source embedding job submitted"

            elif item_type == "note":
                note_item = await Note.get(item_id)

                # Note.save() internally submits embed_note command and
                # returns command_id. Unlike Source.vectorize(), save()'s
                # embed submission is best-effort (a hiccup there shouldn't
                # fail an otherwise-successful note save) - but this
                # endpoint's whole point is submitting the embedding job,
                # so a submission failure here (content present, no
                # command_id) must still surface as a failure.
                command_id = await note_item.save()
                if not command_id and note_item.content and note_item.content.strip():
                    raise HTTPException(
                        status_code=500, detail="Failed to submit note embedding job"
                    )
                message = "Note embedding job submitted"

            return EmbedResponse(
                success=True,
                message=message,
                item_id=item_id,
                item_type=item_type,
                command_id=command_id,
            )

    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(
            status_code=404, detail=f"{embed_request.item_type} not found"
        )
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(
            f"Error embedding {embed_request.item_type} {embed_request.item_id}: {str(e)}"
        )
        raise HTTPException(
            status_code=500, detail=f"Error embedding content: {str(e)}"
        )
