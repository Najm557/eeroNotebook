from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from surreal_commands import get_command_status, submit_command

from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.exceptions import AccessUnavailableError

# Which argument names on a command record name a piece of member content, and
# which kind of content that is. A job carries no owner - `command` has only app,
# args, context, error_message, id, name, result and status - so this mapping is
# how a job is resolved back to something access.py can decide on (spec task 6.4).
#
# Keys rather than positions because every command in this codebase names its
# subject the same way: embed_source, create_insight and run_transformation all
# pass `source_id`, and embed_note passes `note_id`.
_CONTENT_ARG_KEYS: Dict[str, str] = {
    "source_id": "source",
    "note_id": "note",
    "notebook_id": "notebook",
}


class CommandService:
    """Generic service layer for command operations"""

    @staticmethod
    async def content_for_job(job_id: str) -> Optional[Tuple[str, str]]:
        """What member content a job acts on, as (kind, record id), or None.

        None means the job is not about any one member's content - a
        `rebuild_embeddings` run is instance-wide - and a caller enforcing access
        should refuse rather than guess. There is no "unscopable so allow it"
        branch here on purpose: a job result can be insight text.

        Two resolution paths, because commands are submitted in two shapes:

        1. The arguments name the content directly (`source_id`, `note_id`,
           `notebook_id`).
        2. `process_source` is submitted *before* its Source exists, so its
           arguments name no source. The Source is linked to the job afterwards
           (`source.command`), so that link is read in reverse.

        Raises rather than answering None when the read fails, for the reason
        access.py raises: "the database is unreachable" must not be
        indistinguishable from "this job is about nothing you can see", because
        the second silently passes a check that was meant to run.
        """
        try:
            job = ensure_record_id(job_id if ":" in job_id else f"command:{job_id}")
        except Exception:
            logger.debug("unparseable command job id: {!r}", job_id)
            return None

        try:
            rows = await repo_query("SELECT args FROM $job", {"job": job})
        except Exception as exc:
            logger.error("command job lookup failed: {}", exc)
            raise AccessUnavailableError(
                "Job access could not be determined because the database is "
                "unavailable"
            ) from exc

        args = (rows[0].get("args") if rows else None) or {}
        if isinstance(args, dict):
            for key, kind in _CONTENT_ARG_KEYS.items():
                value = args.get(key)
                if value:
                    return kind, str(value)

        try:
            linked = await repo_query(
                "SELECT VALUE id FROM source WHERE command = $job", {"job": job}
            )
        except Exception as exc:
            logger.error("command job source lookup failed: {}", exc)
            raise AccessUnavailableError(
                "Job access could not be determined because the database is "
                "unavailable"
            ) from exc

        if linked and linked[0] is not None:
            return "source", str(linked[0])

        return None

    @staticmethod
    async def submit_command_job(
        module_name: str,  # Actually app_name for surreal-commands
        command_name: str,
        command_args: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Submit a generic command job for background processing"""
        try:
            # Ensure command modules are imported before submitting
            # This is needed because submit_command validates against local registry
            try:
                import commands.podcast_commands  # noqa: F401
            except ImportError as import_err:
                logger.error(f"Failed to import command modules: {import_err}")
                raise ValueError("Command modules not available")

            # surreal-commands expects: submit_command(app_name, command_name, args)
            cmd_id = submit_command(
                module_name,  # This is actually the app name (e.g., "open_notebook")
                command_name,  # Command name (e.g., "generate_podcast")
                command_args,  # Input data
            )
            # Convert RecordID to string if needed
            if not cmd_id:
                raise ValueError("Failed to get cmd_id from submit_command")
            cmd_id_str = str(cmd_id)
            logger.info(
                f"Submitted command job: {cmd_id_str} for {module_name}.{command_name}"
            )
            return cmd_id_str

        except Exception as e:
            logger.error(f"Failed to submit command job: {e}")
            raise

    @staticmethod
    async def get_command_status(job_id: str) -> Dict[str, Any]:
        """Get status of any command job"""
        try:
            status = await get_command_status(job_id)
            return {
                "job_id": job_id,
                "status": status.status if status else "unknown",
                "result": status.result if status else None,
                "error_message": getattr(status, "error_message", None)
                if status
                else None,
                "created": str(status.created)
                if status and hasattr(status, "created") and status.created
                else None,
                "updated": str(status.updated)
                if status and hasattr(status, "updated") and status.updated
                else None,
                "progress": getattr(status, "progress", None) if status else None,
            }
        except Exception as e:
            logger.error(f"Failed to get command status: {e}")
            raise

    @staticmethod
    async def list_command_jobs(
        module_filter: Optional[str] = None,
        command_filter: Optional[str] = None,
        status_filter: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List command jobs with optional filtering"""
        # This will be implemented with proper SurrealDB queries
        # For now, return empty list as this is foundation phase
        return []

    @staticmethod
    async def cancel_command_job(job_id: str) -> bool:
        """Cancel a running command job"""
        try:
            # Implementation depends on surreal-commands cancellation support
            # For now, just log the attempt
            logger.info(f"Attempting to cancel job: {job_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel command job: {e}")
            raise
