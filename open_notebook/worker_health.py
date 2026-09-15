"""Detecting a worker that has stopped consuming its queue.

`surreal_commands` scans `command` for `status = 'new'` once at worker startup and
then subscribes to a single LIVE query with no reconnect, no polling fallback, and
no error path around the subscription. When SurrealDB restarts, that subscription
dies and the worker process stays alive, healthy, and idle — commands queue
forever with nothing logged.

That failure is invisible in exactly the way that matters: source processing,
embeddings, and every study artifact are background commands, so a silenced worker
looks identical to a model that never answers.

This module answers one question — is work sitting unclaimed? — and leaves the
decision of what to do about it to the caller.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from open_notebook.database.repository import repo_query

# How long a command may sit in `new` before the queue is considered stalled.
# Well above the sub-second claim time of a healthy worker, and well below the
# point where somebody notices their upload never finished.
DEFAULT_STALL_THRESHOLD_SECONDS = 90.0


@dataclass(frozen=True)
class QueueState:
    """What the command queue looks like right now."""

    new_count: int
    oldest_age_seconds: Optional[float]
    threshold_seconds: float

    @property
    def is_stalled(self) -> bool:
        """True when work is queued and the oldest item has waited too long.

        Depth alone is not a fault: a healthy worker under load legitimately has
        items in `new`. Age is the signal, because a consuming worker keeps the
        oldest item young.
        """
        if self.new_count == 0 or self.oldest_age_seconds is None:
            return False
        return self.oldest_age_seconds > self.threshold_seconds


def _parse_created(value: Any) -> Optional[datetime]:
    """Coerce SurrealDB's `created` into an aware datetime, or None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


async def read_queue_state(
    threshold_seconds: float = DEFAULT_STALL_THRESHOLD_SECONDS,
    now: Optional[datetime] = None,
) -> QueueState:
    """Measure unclaimed work without touching it."""
    counted = await repo_query(
        "SELECT count() FROM command WHERE status = 'new' GROUP ALL"
    )
    new_count = 0
    if counted:
        raw = counted[0].get("count") if isinstance(counted[0], dict) else None
        new_count = int(raw or 0)

    if new_count == 0:
        return QueueState(0, None, threshold_seconds)

    oldest = await repo_query(
        "SELECT created FROM command WHERE status = 'new' ORDER BY created ASC LIMIT 1"
    )
    created = _parse_created(oldest[0].get("created")) if oldest else None
    if created is None:
        # Queued work whose age cannot be established. Reported as un-aged rather
        # than as stalled: restarting the worker on an unparseable timestamp would
        # turn a reporting gap into a restart loop.
        return QueueState(new_count, None, threshold_seconds)

    reference = now or datetime.now(timezone.utc)
    age = (reference - created).total_seconds()
    return QueueState(new_count, max(age, 0.0), threshold_seconds)
