"""Detecting a worker that has stopped consuming its queue.

`surreal_commands` scans `command` for `status = 'new'` once at worker startup and
then subscribes to a single LIVE query with no reconnect, no polling fallback, and
no error path around the subscription. When SurrealDB restarts, that subscription
dies and the worker process stays alive, healthy, and idle — commands queue
forever with nothing logged.

That failure is invisible in exactly the way that matters: source processing,
embeddings, and every study artifact are background commands, so a silenced worker
looks identical to a model that never answers.

**Why persistence rather than a timestamp.** The `command` table carries no
creation time — its fields are `app`, `args`, `context`, `error_message`, `id`,
`name`, `result`, `status`, and nothing else. (The library's own startup scan
orders by a `created` column that does not exist, which SurrealDB tolerates
silently.) So age cannot be read from the database. Instead an observer remembers
which command ids it has seen unclaimed and for how long, which measures
non-consumption directly and needs nothing from the library or the schema.
"""

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set

from open_notebook.database.repository import repo_query

# How long a single command may stay unclaimed before the queue is considered
# stalled. Well above the sub-second claim time of a healthy worker, and well
# below the point where somebody notices their upload never finished.
DEFAULT_STALL_THRESHOLD_SECONDS = 90.0


@dataclass(frozen=True)
class QueueState:
    """What the command queue looks like across successive observations."""

    new_count: int
    # Seconds the longest-waiting command has been observed unclaimed. None when
    # nothing is queued — distinct from 0.0, which means "queued, just arrived".
    longest_unclaimed_seconds: Optional[float]
    threshold_seconds: float

    @property
    def is_stalled(self) -> bool:
        """True when one command has stayed unclaimed beyond the threshold.

        Depth alone is not a fault: a healthy worker under load legitimately has
        items in `new`. Persistence of the *same* item is the signal, because a
        consuming worker never leaves one sitting.
        """
        if self.new_count == 0 or self.longest_unclaimed_seconds is None:
            return False
        return self.longest_unclaimed_seconds > self.threshold_seconds

    def describe(self) -> str:
        """A log line that does not disguise 'unknown' as 'zero'."""
        if self.new_count == 0:
            return "queue empty"
        if self.longest_unclaimed_seconds is None:
            return f"{self.new_count} queued, first observation"
        return (
            f"{self.new_count} queued, longest unclaimed "
            f"{self.longest_unclaimed_seconds:.0f}s of {self.threshold_seconds:.0f}s"
        )


async def read_new_command_ids() -> List[str]:
    """Ids of commands nobody has claimed."""
    rows = await repo_query("SELECT id FROM command WHERE status = 'new'")
    ids: List[str] = []
    for row in rows or []:
        if isinstance(row, dict) and row.get("id") is not None:
            ids.append(str(row["id"]))
    return ids


@dataclass
class QueueObserver:
    """Tracks how long each queued command has gone unclaimed.

    Stateful by necessity, and the state is deliberately tiny: one timestamp per
    unclaimed command id, discarded the moment that command is picked up.
    """

    threshold_seconds: float = DEFAULT_STALL_THRESHOLD_SECONDS
    _first_seen: Dict[str, float] = field(default_factory=dict)

    def observe(self, new_ids: Iterable[str], now: float) -> QueueState:
        """Record the current queue and report what it implies."""
        current: Set[str] = set(new_ids)

        # Forget anything that has been claimed, so a command that queues again
        # later starts a fresh clock rather than inheriting an old one.
        for known in list(self._first_seen):
            if known not in current:
                del self._first_seen[known]

        for command_id in current:
            self._first_seen.setdefault(command_id, now)

        if not current:
            return QueueState(0, None, self.threshold_seconds)

        longest = max(now - self._first_seen[cid] for cid in current)
        return QueueState(len(current), longest, self.threshold_seconds)

    def reset(self) -> None:
        """Drop all observations. Used after a restart, so the worker is judged on
        what happens next rather than on the backlog it inherited."""
        self._first_seen.clear()
