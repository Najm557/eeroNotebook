"""Tests for stalled-worker detection.

The distinction under test is depth versus persistence. A busy worker legitimately
has commands in `new`; a silenced one leaves the *same* command sitting. Getting
that wrong in either direction is costly: treating depth as a fault restarts a
healthy worker under load, and missing persistence leaves the real failure
invisible.

Persistence is measured across the watchdog's own polls because the `command`
table carries no creation timestamp — an earlier version of this module tried to
age rows from a `created` column that does not exist, and reported every queue as
healthy.
"""

from unittest.mock import AsyncMock, patch

import pytest

from open_notebook.worker_health import (
    DEFAULT_STALL_THRESHOLD_SECONDS,
    QueueObserver,
    QueueState,
    read_new_command_ids,
)


class TestStallDecision:
    def test_empty_queue_is_not_stalled(self) -> None:
        assert not QueueState(0, None, 90.0).is_stalled

    def test_depth_alone_is_not_a_stall(self) -> None:
        """A worker under load has queued work and is consuming it."""
        assert not QueueState(25, 3.0, 90.0).is_stalled

    def test_persistent_unclaimed_work_is_a_stall(self) -> None:
        assert QueueState(1, 120.0, 90.0).is_stalled

    def test_exactly_at_threshold_is_not_yet_a_stall(self) -> None:
        assert not QueueState(1, 90.0, 90.0).is_stalled

    def test_first_observation_is_never_a_stall(self) -> None:
        assert not QueueState(5, None, 90.0).is_stalled

    def test_describe_distinguishes_unknown_from_zero(self) -> None:
        """The bug this guards against: an earlier log line rendered a missing
        measurement as '0s', which made a broken detector look like a healthy
        queue."""
        assert "first observation" in QueueState(3, None, 90.0).describe()
        assert "0s of 90s" in QueueState(3, 0.0, 90.0).describe()
        assert QueueState(0, None, 90.0).describe() == "queue empty"


class TestQueueObserver:
    def test_empty_queue_reports_nothing_queued(self) -> None:
        observer = QueueObserver(threshold_seconds=90.0)
        state = observer.observe([], now=1000.0)
        assert state.new_count == 0
        assert state.longest_unclaimed_seconds is None

    def test_first_sighting_has_no_elapsed_time(self) -> None:
        observer = QueueObserver(threshold_seconds=90.0)
        state = observer.observe(["command:a"], now=1000.0)
        assert state.new_count == 1
        assert state.longest_unclaimed_seconds == 0.0
        assert not state.is_stalled

    def test_same_command_still_queued_accumulates_time(self) -> None:
        observer = QueueObserver(threshold_seconds=90.0)
        observer.observe(["command:a"], now=1000.0)
        state = observer.observe(["command:a"], now=1100.0)
        assert state.longest_unclaimed_seconds == pytest.approx(100.0)
        assert state.is_stalled

    def test_a_consumed_command_is_forgotten(self) -> None:
        """A worker that claims work keeps the queue young."""
        observer = QueueObserver(threshold_seconds=90.0)
        observer.observe(["command:a"], now=1000.0)
        observer.observe([], now=1050.0)
        state = observer.observe(["command:a"], now=1100.0)
        assert state.longest_unclaimed_seconds == 0.0, (
            "a requeued id must start a fresh clock, not inherit the old one"
        )
        assert not state.is_stalled

    def test_steady_throughput_never_looks_stalled(self) -> None:
        """Different commands arriving and being claimed, indefinitely."""
        observer = QueueObserver(threshold_seconds=90.0)
        now = 0.0
        for i in range(50):
            now += 30.0
            state = observer.observe([f"command:{i}"], now=now)
            assert not state.is_stalled

    def test_longest_waiter_drives_the_decision(self) -> None:
        observer = QueueObserver(threshold_seconds=90.0)
        observer.observe(["command:old"], now=1000.0)
        state = observer.observe(["command:old", "command:new"], now=1200.0)
        assert state.new_count == 2
        assert state.longest_unclaimed_seconds == pytest.approx(200.0)
        assert state.is_stalled

    def test_reset_clears_observations(self) -> None:
        observer = QueueObserver(threshold_seconds=90.0)
        observer.observe(["command:a"], now=1000.0)
        observer.reset()
        state = observer.observe(["command:a"], now=1500.0)
        assert state.longest_unclaimed_seconds == 0.0

    def test_default_threshold(self) -> None:
        assert QueueObserver().threshold_seconds == DEFAULT_STALL_THRESHOLD_SECONDS


class TestReadNewCommandIds:
    @pytest.mark.asyncio
    async def test_extracts_ids(self) -> None:
        with patch(
            "open_notebook.worker_health.repo_query", new_callable=AsyncMock
        ) as query:
            query.return_value = [{"id": "command:a"}, {"id": "command:b"}]
            assert await read_new_command_ids() == ["command:a", "command:b"]

    @pytest.mark.asyncio
    async def test_empty_result_is_empty_list(self) -> None:
        with patch(
            "open_notebook.worker_health.repo_query", new_callable=AsyncMock
        ) as query:
            query.return_value = []
            assert await read_new_command_ids() == []

    @pytest.mark.asyncio
    async def test_rows_without_an_id_are_skipped(self) -> None:
        with patch(
            "open_notebook.worker_health.repo_query", new_callable=AsyncMock
        ) as query:
            query.return_value = [{"id": None}, {"status": "new"}, {"id": "command:c"}]
            assert await read_new_command_ids() == ["command:c"]

    @pytest.mark.asyncio
    async def test_only_new_commands_are_requested(self) -> None:
        with patch(
            "open_notebook.worker_health.repo_query", new_callable=AsyncMock
        ) as query:
            query.return_value = []
            await read_new_command_ids()
        assert query.await_args is not None
        sql = query.await_args.args[0]
        assert "status = 'new'" in sql
        assert "created" not in sql, "the command table has no created column"
