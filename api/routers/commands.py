from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field
from surreal_commands import registry

from api.command_service import CommandService
from open_notebook.domain.access import (
    require_note_read,
    require_notebook_read,
    require_source_read,
)
from open_notebook.domain.member import Member
from open_notebook.exceptions import (
    AccessDeniedError,
    NotFoundError,
    OpenNotebookError,
)
from open_notebook.identity import current_member

# A command job record carries no notebook reference, so there is nothing for an
# access check to resolve: `command` has only app, args, context, error_message,
# id, name, result and status. Three of this router's routes therefore cannot be
# scoped, and each of them reaches content across every member:
#
# - POST /commands/jobs submits any registered command with any arguments, which
#   is a write path into anybody's sources and notes that bypasses every scoped
#   route in the API.
# - GET /commands/jobs lists jobs with their args and results. A `run_transformation`
#   result is insight text and its args name a source id, so the list discloses
#   other members' material.
# - DELETE /commands/jobs/{id} cancels somebody else's job.
#
# All three are closed rather than left open (spec task 6.2). Every capability
# POST reached has a scoped route of its own - /sources, /sources/{id}/retry,
# /sources/{id}/insights, /embed, /embeddings/rebuild, /podcasts/generate - so
# nothing is lost, and the frontend uses none of the three.
#
# GET /commands/jobs/{job_id} is now scoped rather than open, and without the
# migration task 6.2 expected it to need. An owner column on `command` would have
# to be written by this application after submit_command() returns, racing the
# worker that is already processing the row; resolving the job back to the content
# it names needs no schema change and no second write. See
# CommandService.content_for_job. A job that names no member content is refused,
# so there is no "unscopable, therefore allowed" branch.
_UNSCOPABLE = (
    "This endpoint is not available: a command job carries no notebook, so "
    "ownership cannot be enforced on it. Use the endpoint for the specific "
    "action instead - sources, insights, embeddings or podcasts."
)

router = APIRouter()


class CommandExecutionRequest(BaseModel):
    command: str = Field(
        ..., description="Command function name (e.g., 'generate_podcast')"
    )
    app: str = Field(..., description="Application name (e.g., 'open_notebook')")
    input: Dict[str, Any] = Field(..., description="Arguments to pass to the command")


class CommandJobResponse(BaseModel):
    job_id: str
    status: str
    message: str


class CommandJobStatusResponse(BaseModel):
    job_id: str
    status: str
    result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    created: Optional[str] = None
    updated: Optional[str] = None
    progress: Optional[Dict[str, Any]] = None


@router.post("/commands/jobs", response_model=CommandJobResponse)
async def execute_command(request: CommandExecutionRequest):
    """
    Submit a command for background processing.
    Returns immediately with job ID for status tracking.

    Example request:
    {
        "command": "generate_podcast",
        "app": "open_notebook",
        "input": {
            "episode_profile": "tech_experts",
            "speaker_profile": "tech_experts",
            "episode_name": "My Episode",
            "content": "Content to discuss"
        }
    }
    """
    raise AccessDeniedError(_UNSCOPABLE)


@router.get("/commands/jobs/{job_id}", response_model=CommandJobStatusResponse)
async def get_command_job_status(
    job_id: str, member: Member = Depends(current_member)
):
    """Get the status of a command job about content this member can read.

    A job's `result` can be insight text and its `error_message` can quote source
    content, so this answers only for a caller who could read that content
    directly.

    Every refusal is 404 with one message, whatever the reason - no such job, a
    job about somebody else's source, a job about nothing scopable. A 403, or a
    404 whose wording differed per case, would confirm the job exists and hint at
    what it touched (Requirement 7.4).
    """
    content = await CommandService.content_for_job(job_id)
    if content is None:
        raise NotFoundError("Job not found")

    kind, record_id = content
    checks = {
        "source": require_source_read,
        "note": require_note_read,
        "notebook": require_notebook_read,
    }
    try:
        await checks[kind](member, record_id)
    except NotFoundError:
        raise NotFoundError("Job not found") from None

    try:
        status_data = await CommandService.get_command_status(job_id)
        return CommandJobStatusResponse(**status_data)

    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error fetching job status: {str(e)}")
        raise HTTPException(
            status_code=500, detail="Failed to fetch job status"
        )


@router.get("/commands/jobs", response_model=List[Dict[str, Any]])
async def list_command_jobs(
    command_filter: Optional[str] = Query(None, description="Filter by command name"),
    status_filter: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(50, description="Maximum number of jobs to return"),
):
    """List command jobs with optional filtering"""
    raise AccessDeniedError(_UNSCOPABLE)


@router.delete("/commands/jobs/{job_id}")
async def cancel_command_job(job_id: str):
    """Cancel a running command job"""
    raise AccessDeniedError(_UNSCOPABLE)


@router.get("/commands/registry/debug")
async def debug_registry():
    """Debug endpoint to see what commands are registered"""
    try:
        # Get all registered commands
        all_items = registry.get_all_commands()

        # Create JSON-serializable data
        command_items = []
        for item in all_items:
            try:
                command_items.append(
                    {
                        "app_id": item.app_id,
                        "name": item.name,
                        "full_id": f"{item.app_id}.{item.name}",
                    }
                )
            except Exception as item_error:
                logger.error(f"Error processing item: {item_error}")

        # Get the basic command structure
        try:
            commands_dict: dict[str, list[str]] = {}
            for item in all_items:
                if item.app_id not in commands_dict:
                    commands_dict[item.app_id] = []
                commands_dict[item.app_id].append(item.name)
        except Exception:
            commands_dict = {}

        return {
            "total_commands": len(all_items),
            "commands_by_app": commands_dict,
            "command_items": command_items,
        }

    except Exception as e:
        logger.error(f"Error debugging registry: {str(e)}")
        return {
            "error": str(e),
            "total_commands": 0,
            "commands_by_app": {},
            "command_items": [],
        }
