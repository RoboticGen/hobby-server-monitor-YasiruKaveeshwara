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
_sync_lock = threading.Lock()
_last_file_signature: tuple[float, int] = (0.0, 0)


def _sync_store_with_disk(store: TinyFlux) -> None:
    """Reindex if the underlying CSV file was modified by another process.

    The background collector process writes new data points directly to the CSV
    file on disk. Without reindexing, a long-running API process holds a stale
    in-memory index and continues serving points from when the API booted.
    """
    global _last_file_signature
    try:
        stat = config.tinyflux_path.stat()
        sig = (stat.st_mtime, stat.st_size)
        if sig != _last_file_signature:
            with _sync_lock:
                if sig != _last_file_signature:
                    store.index.invalidate()
                    store.reindex()
                    _last_file_signature = (stat.st_mtime, stat.st_size)
    except Exception:
        pass


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
                try:
                    stat = config.tinyflux_path.stat()
                    global _last_file_signature
                    _last_file_signature = (stat.st_mtime, stat.st_size)
                except Exception:
                    pass
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


def remove_points(query) -> int:
    """Delete points matching a query and return how many were removed.

    Wraps TinyFlux's remove() to work around a defect in 1.2.0: inserting a
    point older than the newest one already stored invalidates the in-memory
    time index (correctly), but a subsequent remove() rebuilds that index
    from a partial view and marks it valid again. From then on, time-range
    queries in this process are answered from an index that does not match
    the file — silently returning too few points, or occasionally too many,
    with the data itself intact on disk.

    That is exactly the sequence retention performs: write a back-dated
    rollup bucket, then remove the raw points it replaced.

    invalidate() forces the next query to scan the file and rebuild
    honestly. reindex() is not an alternative — it checks index.valid,
    finds True, and returns without doing anything.

    Every deletion goes through here rather than calling store.remove()
    directly, so no caller has to remember the workaround.
    """
    store = get_store()
    removed = store.remove(query)
    store.index.invalidate()
    return removed


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
    _sync_store_with_disk(store)
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
    _sync_store_with_disk(store)
    Tag = TagQuery()

    results = store.search(
        (Tag.container_id == container_id) & (Tag.resolution == "raw")
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


def get_recent_points(container_id: str, limit: int = 60) -> list[dict]:
    """Return the most recent raw points for a container, newest-last.

    Used by the /api/metrics/recent endpoint to pre-seed the frontend's
    live graph on first load so the chart is immediately populated with
    the last `limit` samples rather than starting empty and accumulating
    point-by-point from subsequent 5-second polls.

    Args:
        container_id: UUID of the container to query.
        limit: Maximum number of most-recent raw points to return.
               Defaults to 60 (~5 minutes at a 5-second collector interval).

    Returns:
        A list of plain dicts ordered ascending by time (oldest first),
        so the frontend can append new live points to the tail of this list.
    """
    store = get_store()
    _sync_store_with_disk(store)
    Tag = TagQuery()

    # Fetch all raw points for this container (no time filter needed here;
    # the retention job already prunes anything older than 24 h). Sorting
    # after the fact is cheaper than a TinyFlux time-range scan when the
    # raw dataset for a single container is small (24h / collector_interval
    # = at most ~17,280 rows at the shipped 5-second interval).
    results = store.search(
        (Tag.container_id == container_id) & (Tag.resolution == "raw")
    )

    if not results:
        return []

    # Sort ascending so the client sees oldest-first — matching what live
    # polling appends to — and take only the last `limit` points.
    sorted_results = sorted(results, key=lambda p: p.time)
    recent = sorted_results[-limit:]

    return [
        {
            "time": point.time.isoformat(),
            "container_id": container_id,
            "resolution": "raw",
            **point.fields,
        }
        for point in recent
    ]
