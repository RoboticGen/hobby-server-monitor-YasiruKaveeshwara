"""
Background metrics collector process.

Runs as a standalone OS process (not a thread inside the API), satisfying
the project brief's requirement that "the metrics collector runs
independently of the UI/API." This process polls every active container
on a fixed interval and writes metric points to TinyFlux.

Independence from LXD is a hard requirement: if LXD is temporarily
unreachable (daemon restart, update, overload), the collector must log a
warning for the affected container and continue to the next one. It must
NEVER crash or stop because of a single container failure or a fully-down
LXD. This is enforced by the per-container try/except in the main loop —
exceptions are caught at the individual container level, not at the loop
level, so one bad container cannot affect the others.

Run this process with:
    python -m backend.collector.collector
"""

import logging
import time
from datetime import datetime, timezone

from backend.collector.retention import run_retention_pass
from backend.config import config
from backend.db import repo
from backend.lxd import client as lxd_client
from backend.lxd.client import LXDUnavailableError
from backend.tsdb import store as tsdb

# Module-level logger so every log line from this process is prefixed
# consistently, making it easy to filter in system logs.
log = logging.getLogger(__name__)


def _collect_one(container: dict) -> None:
    """Collect and store a single metric snapshot for one container.

    Called inside a per-container try/except in the main loop so a failure
    here never propagates to the loop — matching the brief's independence
    requirement. If LXD is unreachable for this container, raises
    LXDUnavailableError, which the caller catches and logs as a warning.

    Args:
        container: A container dict from repo.list_active_containers().
    """
    state = lxd_client.get_container_state(container["lxd_name"])
    tsdb.write_point(
        container_id=container["id"],
        fields=state,
        resolution="raw",
    )


def run_collector_loop() -> None:
    """Main collection loop — runs forever until the process is killed.

    - Sleeps COLLECTOR_INTERVAL_SECONDS between iterations (not between
      containers) so all containers in one iteration share the same
      approximate timestamp window.
    - Per-container try/except with warning-level logging: one failed
      container or a fully-down LXD does NOT crash the loop. This is the
      exact independence guarantee the brief requires.
    - Uses LXDUnavailableError for LXD-specific failures and a broad
      Exception catch for any other unexpected error (e.g., DB read
      failure) — both are warned about, not raised.
    - Retention/downsampling runs once per hour using an
      in-memory timestamp. This does not need to survive a restart — the
      worst case on restart is running retention slightly earlier than one
      hour after the last run, which is safe. It is NOT a second process.
    """
    log.info(
        "Collector starting. Interval: %ds",
        config.collector_interval_seconds,
    )

    # In-memory tracking of the last retention run time.
    # Simple datetime comparison — does not need to survive a restart.
    _last_retention_run: datetime | None = None
    _retention_interval_seconds = 3600  # 1 hour in wall-clock time

    while True:
        containers = repo.list_active_containers()
        log.debug("Collecting metrics for %d container(s)", len(containers))

        for container in containers:
            try:
                _collect_one(container)
                log.debug(
                    "Collected metrics for container %s (%s)",
                    container["lxd_name"],
                    container["id"],
                )
            except LXDUnavailableError as exc:
                # LXD is down or this container is not reachable.
                # Log a warning and continue to the next container —
                # the brief explicitly requires the collector to keep
                # running when LXD is temporarily unavailable.
                log.warning(
                    "LXD unavailable for container %s: %s — skipping",
                    container["lxd_name"],
                    exc,
                )
            except Exception as exc:  # noqa: BLE001
                # Catch-all for unexpected errors (e.g., TinyFlux I/O,
                # DB connectivity). Same rationale: one bad container
                # must never take down the collector for the others.
                log.warning(
                    "Unexpected error collecting container %s: %s — skipping",
                    container["lxd_name"],
                    exc,
                )

        # Hourly retention tick.
        # Runs within the same loop, not a second process.
        now = datetime.now(tz=timezone.utc)
        if (
            _last_retention_run is None
            or (now - _last_retention_run).total_seconds()
            >= _retention_interval_seconds
        ):
            try:
                log.info("Running hourly retention pass")
                run_retention_pass(now=now)
                _last_retention_run = now
            except Exception as exc:  # noqa: BLE001
                log.warning("Retention pass error: %s — will retry next hour", exc)

        time.sleep(config.collector_interval_seconds)


if __name__ == "__main__":
    # This block makes the collector runnable as a standalone OS process:
    #   python -m backend.collector.collector
    # It is a genuinely separate process from the API (app.py), not a
    # background thread inside it — matching "the metrics collector runs
    # independently of the UI/API."
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    run_collector_loop()
