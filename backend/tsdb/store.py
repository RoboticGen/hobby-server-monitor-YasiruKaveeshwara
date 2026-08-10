"""
TinyFlux store wrapper.

Provides a memoized TinyFlux instance and three read/write functions
that are the only interface to the time-series database throughout the
application. Callers do not import tinyflux directly.

The 'resolution' tag is the key to how retention/downsampling
job and history endpoint share a single TinyFlux measurement
instead of needing separate tables per granularity — every point is tagged
with resolution="raw"|"5m"|"1h", so the retention job can query and delete
by resolution while history queries can filter to a specific granularity.
"""

import threading
from datetime import datetime, timezone

from tinyflux import TinyFlux, Point, TagQuery, TimeQuery

from backend.config import config


# Module-level memoized instance. Initialised on first call to get_store()
# and reused for all subsequent calls. A threading lock protects the
# initialisation check so concurrent requests don't create two instances.
_store: TinyFlux | None = None
_store_lock = threading.Lock()


def get_store() -> TinyFlux:
    """Return the memoized TinyFlux instance, creating it on first call.

    Points at config.TINYFLUX_PATH. Thread-safe initialisation via a lock
    — only one TinyFlux instance is ever created per process lifetime.
    """
    global _store
    if _store is None:
        with _store_lock:
            # Double-checked locking: re-check after acquiring the lock
            # in case another thread initialised it while we were waiting.
            if _store is None:
                _store = TinyFlux(config.tinyflux_path)
    return _store


def write_point(
    container_id: str,
    fields: dict,
    resolution: str = "raw",
    timestamp: datetime | None = None,
) -> None:
    """Write a single data point for a container.

    Tags every point with container_id and resolution so the retention
    job and history endpoint can filter by both
    without needing separate TinyFlux measurements per granularity.

    Args:
        container_id: The UUID of the container these metrics belong to.
        fields: Flat dict of metric values (e.g. cpu_pct, ram_mb, …).
        resolution: Granularity tag — "raw" for live collector points,
                    "5m" for 5-minute averages, "1h" for hourly averages.
        timestamp: Optional explicit UTC datetime for the point. Defaults
                   to the current UTC time. Provided so the retention test
                   can insert synthetically old points without sleeping.
    """
    store = get_store()
    point = Point(
        time=timestamp if timestamp is not None else datetime.now(tz=timezone.utc),
        tags={"container_id": container_id, "resolution": resolution},
        fields=fields,
    )
    store.insert(point)


def query_range(
    container_id: str,
    start: datetime,
    end: datetime,
    resolution: str = "raw",
) -> list[dict]:
    """Return all points for a container within a UTC time range.

    Filters by both container_id tag and resolution tag so callers can
    query raw, 5m-averaged, or 1h-averaged history independently.

    Returns plain dicts (not TinyFlux Point objects) so callers elsewhere
    in the application do not need to know about TinyFlux's types. Each
    dict contains 'time', 'resolution', 'container_id', and all field keys.

    Args:
        container_id: UUID of the container to query.
        start: Inclusive start of the time range (UTC, timezone-aware).
        end: Inclusive end of the time range (UTC, timezone-aware).
        resolution: Granularity to query — "raw", "5m", or "1h".
    """
    store = get_store()
    Tag = TagQuery()
    Time = TimeQuery()

    results = store.search(
        (Tag.container_id == container_id)
        & (Tag.resolution == resolution)
        & (Time >= start)
        & (Time <= end)
    )

    # Convert each TinyFlux Point to a plain dict so callers don't
    # depend on tinyflux types.
    return [
        {
            "time": point.time.isoformat(),
            "container_id": container_id,
            "resolution": resolution,
            **point.fields,
        }
        for point in results
    ]


def get_latest_point(container_id: str) -> dict | None:
    """Return the most recent raw point for a container, or None.

    Used by the /api/metrics/latest polling endpoint (Phase 11) to serve
    the dashboard's live metric cards without querying a full time range.
    Returns a plain dict, or None if no points exist for this container.
    """
    store = get_store()
    Tag = TagQuery()

    results = store.search(
        (Tag.container_id == container_id)
        & (Tag.resolution == "raw")
    )

    if not results:
        return None

    # TinyFlux returns points in insertion order; the last one is newest.
    latest = max(results, key=lambda p: p.time)
    return {
        "time": latest.time.isoformat(),
        "container_id": container_id,
        "resolution": "raw",
        **latest.fields,
    }
