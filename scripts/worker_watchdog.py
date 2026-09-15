"""Restart the command worker when it stops consuming its queue.

Why this exists, and why it is a restart rather than a repair: `surreal_commands`
holds one LIVE subscription with no reconnect, but it *does* scan for
`status = 'new'` at startup. So the library already contains the recovery path —
it simply never runs it again. Restarting the worker executes that scan and drains
the backlog, which is why this watchdog does the crude thing on purpose instead of
reaching inside the library's listener.

Runs as its own supervisord program so that it survives the failure it watches for.
"""

import asyncio
import os
import subprocess
import sys

from loguru import logger

from open_notebook.worker_health import (
    DEFAULT_STALL_THRESHOLD_SECONDS,
    read_queue_state,
)

CHECK_INTERVAL_SECONDS = float(os.environ.get("OPEN_NOTEBOOK_WATCHDOG_INTERVAL", "30"))
STALL_THRESHOLD_SECONDS = float(
    os.environ.get("OPEN_NOTEBOOK_WATCHDOG_THRESHOLD", DEFAULT_STALL_THRESHOLD_SECONDS)
)
WORKER_PROGRAM = os.environ.get("OPEN_NOTEBOOK_WATCHDOG_PROGRAM", "worker")
SUPERVISOR_CONF = "/etc/supervisor/conf.d/supervisord.conf"

# After a restart, ignore the queue for long enough that the worker can start and
# run its scan. Without this the watchdog would restart it again while it is still
# draining, and never let it finish.
RESTART_GRACE_SECONDS = 90.0


def restart_worker() -> bool:
    """Ask supervisord to restart the worker. True when it reports success."""
    try:
        result = subprocess.run(
            ["supervisorctl", "-c", SUPERVISOR_CONF, "restart", WORKER_PROGRAM],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("watchdog: could not invoke supervisorctl: {}", exc)
        return False

    output = (result.stdout or "").strip()
    if result.returncode != 0:
        logger.error(
            "watchdog: supervisorctl restart failed (rc={}): {} {}",
            result.returncode,
            output,
            (result.stderr or "").strip(),
        )
        return False

    logger.warning("watchdog: restarted {} — {}", WORKER_PROGRAM, output)
    return True


async def main() -> int:
    logger.info(
        "watchdog: watching the command queue every {}s, stall threshold {}s",
        CHECK_INTERVAL_SECONDS,
        STALL_THRESHOLD_SECONDS,
    )
    # Let the stack finish starting before the first read, so a cold boot with a
    # genuine backlog is not mistaken for a stall.
    await asyncio.sleep(RESTART_GRACE_SECONDS)

    while True:
        try:
            state = await read_queue_state(STALL_THRESHOLD_SECONDS)
            if state.is_stalled:
                logger.error(
                    "watchdog: queue stalled — {} command(s) unclaimed, oldest {:.0f}s "
                    "(threshold {:.0f}s). The worker's LIVE subscription has most "
                    "likely died; restarting it to trigger its startup scan.",
                    state.new_count,
                    state.oldest_age_seconds or 0.0,
                    state.threshold_seconds,
                )
                if restart_worker():
                    await asyncio.sleep(RESTART_GRACE_SECONDS)
                    continue
            elif state.new_count:
                logger.debug(
                    "watchdog: {} queued, oldest {:.0f}s — consuming normally",
                    state.new_count,
                    state.oldest_age_seconds or 0.0,
                )
        except Exception as exc:
            # Never exit on a transient read failure: a watchdog that dies when the
            # database blinks is worse than no watchdog, because its absence looks
            # like health.
            logger.warning("watchdog: queue read failed, will retry: {}", exc)

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
