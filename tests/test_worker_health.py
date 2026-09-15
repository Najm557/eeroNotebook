"""Tests for stalled-worker detection.

The distinction under test is depth versus age. A busy worker legitimately has
commands in `new`; a silenced one has *old* commands in `new`. Getting that wrong
in either direction is costly: treating depth as a fault restarts a healthy worker
under load, and ignoring age leaves the real failure invisible.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from open_notebook.worker_health import (
    DEFAULT_STALL_THRESHOLD_SECONDS,
    QueueState,
    _parse_created,
    read_queue_state,
)

NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)


def _responses(count: int, created=None):
    """Queue the two reads read_queue_state makes, in order."""
    calls = [[{"count": count}]]
    if count:
        calls.append([{"created": created}])
    return calls


class TestStallDecision:
    def test_empty_queue_is_not_stalled(self) -> None:
        assert not QueueState(0, None, 90.0).is_stalled

    def test_depth_alone_is_not_a_stall(self) -> None:
        """A worker under load has queued work and is consuming it."""
        assert not QueueState(25, 3.0, 90.0).is_stalled

    def test_old_unclaimed_work_is_a_stall(self) -> None:
        assert QueueState(1, 120.0, 90.0).is_stalled

    def test_exactly_at_threshold_is_not_yet_a_stall(self) -> None:
        assert not QueueState(1, 90.0, 90.0).is_stalled

    def test_unknown_age_is_never_a_stall(self) -> None:
        """An unparseable timestamp is a reporting gap. Restarting on it would turn
        that gap into a restart loop."""
        assert not QueueState(5, None, 90.0).is_stalled


class TestReadQueueState:
    @pytest.mark.asyncio
    async def test_no_queued_work_skips_the_second_read(self) -> None:
        with patch(
            "open_notebook.worker_health.repo_query", new_callable=AsyncMock
        ) as query:
            query.side_effect = _responses(0)
            state = await read_queue_state(now=NOW)
        assert state.new_count == 0
        assert state.oldest_age_seconds is None
        assert query.await_count == 1, "should not ask for the oldest row when empty"

    @pytest.mark.asyncio
    async def test_computes_age_of_the_oldest_command(self) -> None:
        created = NOW - timedelta(seconds=300)
        with patch(
            "open_notebook.worker_health.repo_query", new_callable=AsyncMock
        ) as query:
            query.side_effect = _responses(4, created)
            state = await read_queue_state(now=NOW)
        assert state.new_count == 4
        assert state.oldest_age_seconds == pytest.approx(300.0)
        assert state.is_stalled

    @pytest.mark.asyncio
    async def test_recent_command_is_healthy(self) -> None:
        created = NOW - timedelta(seconds=5)
        with patch(
            "open_notebook.worker_health.repo_query", new_callable=AsyncMock
        ) as query:
            query.side_effect = _responses(2, created)
            state = await read_queue_state(now=NOW)
        assert not state.is_stalled

    @pytest.mark.asyncio
    async def test_iso_string_timestamps_are_accepted(self) -> None:
        """SurrealDB's client has returned both datetimes and ISO strings."""
        with patch(
            "open_notebook.worker_health.repo_query", new_callable=AsyncMock
        ) as query:
            query.side_effect = _responses(1, "2026-09-15T11:55:00Z")
            state = await read_queue_state(now=NOW)
        assert state.oldest_age_seconds == pytest.approx(300.0)

    @pytest.mark.asyncio
    async def test_unparseable_timestamp_reports_count_without_age(self) -> None:
        with patch(
            "open_notebook.worker_health.repo_query", new_callable=AsyncMock
        ) as query:
            query.side_effect = _responses(3, "not-a-timestamp")
            state = await read_queue_state(now=NOW)
        assert state.new_count == 3
        assert state.oldest_age_seconds is None
        assert not state.is_stalled

    @pytest.mark.asyncio
    async def test_clock_skew_does_not_produce_a_negative_age(self) -> None:
        created = NOW + timedelta(seconds=30)
        with patch(
            "open_notebook.worker_health.repo_query", new_callable=AsyncMock
        ) as query:
            query.side_effect = _responses(1, created)
            state = await read_queue_state(now=NOW)
        assert state.oldest_age_seconds == 0.0
        assert not state.is_stalled

    @pytest.mark.asyncio
    async def test_default_threshold_is_used_when_unspecified(self) -> None:
        with patch(
            "open_notebook.worker_health.repo_query", new_callable=AsyncMock
        ) as query:
            query.side_effect = _responses(0)
            state = await read_queue_state()
        assert state.threshold_seconds == DEFAULT_STALL_THRESHOLD_SECONDS


class TestParseCreated:
    def test_naive_datetime_is_treated_as_utc(self) -> None:
        parsed = _parse_created(datetime(2026, 9, 15, 12, 0, 0))
        assert parsed is not None and parsed.tzinfo is timezone.utc

    def test_aware_datetime_is_preserved(self) -> None:
        assert _parse_created(NOW) == NOW

    def test_unsupported_type_returns_none(self) -> None:
        assert _parse_created(12345) is None
