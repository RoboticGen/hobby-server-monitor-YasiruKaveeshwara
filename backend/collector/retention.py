"""
Retention and downsampling job for TinyFlux.

 "Raw 10-second points are kept for 24 hours. Points older than 24 hours are downsampled into 5-minute averages. 
 Points older than 7 days are downsampled into 1-hour averages. 
 Anything older than 90 days is deleted outright."

TinyFlux has no built-in retention or TTL mechanism — unlike InfluxDB or
Prometheus, it is a simple CSV-backed store. This module is the manual
equivalent: it runs once per hour inside the collector process, walks the
data for each active container, and performs the three-tier rollup+prune
described above.

The resolution tag written to every point by tsdb.store.write_point is the
mechanism that makes this possible without needing separate measurements per
granularity: raw/5m/1h points all live in the same TinyFlux database and
are distinguished solely by that tag.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from tinyflux import TagQuery, TimeQuery

from backend.db import repo
from backend.tsdb.store import get_store, write_point

log = logging.getLogger(__name__)

# thresholds
_RAW_MAX_AGE = timedelta(hours=24)       # raw points older than this → 5m
_FIVE_MIN_MAX_AGE = timedelta(days=7)    # 5m points older than this → 1h
_ABSOLUTE_MAX_AGE = timedelta(days=90)   # anything older → delete outright

# Bucket widths for downsampling
_FIVE_MIN_BUCKET = timedelta(minutes=5)
_ONE_HOUR_BUCKET = timedelta(hours=1)


def _bucket_key(ts: datetime, bucket_width: timedelta) -> datetime:
    """Snap a UTC timestamp to the start of its bucket.

    E.g. with a 5-minute bucket, 12:07:33 → 12:05:00.
    Used to group raw points into average windows.
    """
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    bucket_index = int((ts - epoch).total_seconds() // bucket_width.total_seconds())
    return epoch + timedelta(seconds=bucket_index * bucket_width.total_seconds())


def _average_fields(points: list) -> dict:
    """Return a dict of averaged field values across a list of TinyFlux Points.

    Non-numeric fields are skipped. All numeric fields in the first point
    are included in the average.
    """
    if not points:
        return {}
    # Collect all numeric field keys from all points
    keys: set[str] = set()
    for p in points:
        keys.update(k for k, v in p.fields.items() if isinstance(v, (int, float)))

    return {
        key: sum(p.fields.get(key, 0.0) for p in points) / len(points)
        for key in keys
    }


def _downsample_and_prune(
    container_id: str,
    source_resolution: str,
    target_resolution: str,
    older_than: timedelta,
    bucket_width: timedelta,
    now: datetime,
) -> None:
    """Core downsampling logic for one container and one resolution tier.

    1. Finds all points at source_resolution older than older_than.
    2. Groups them into bucket_width-sized time buckets.
    3. Writes one averaged point per bucket at target_resolution.
    4. Deletes the original source points that were rolled up.

    Args:
        container_id: Container UUID to process.
        source_resolution: Input resolution tag ("raw" or "5m").
        target_resolution: Output resolution tag ("5m" or "1h").
        older_than: Only process points older than (now - older_than).
        bucket_width: Time window to average within.
        now: Reference "current" time (passed in so tests can control it).
    """
    store = get_store()
    Tag = TagQuery()
    Time = TimeQuery()

    cutoff = now - older_than

    # Fetch all source points for this container that are old enough
    points = store.search(
        (Tag.container_id == container_id)
        & (Tag.resolution == source_resolution)
        & (Time < cutoff)
    )

    if not points:
        return

    # Group into time buckets
    buckets: dict[datetime, list] = defaultdict(list)
    for p in points:
        key = _bucket_key(p.time, bucket_width)
        buckets[key].append(p)

    # Write one averaged point per bucket at the target resolution
    for bucket_start, bucket_points in buckets.items():
        avg_fields = _average_fields(bucket_points)
        if avg_fields:
            write_point(
                container_id=container_id,
                fields=avg_fields,
                resolution=target_resolution,
                timestamp=bucket_start,
            )

    # Delete the original source points now that they've been rolled up.
    # We remove them by matching the same query used to fetch them.
    store.remove(
        (Tag.container_id == container_id)
        & (Tag.resolution == source_resolution)
        & (Time < cutoff)
    )

    log.info(
        "Rolled up %d %s points into %d %s buckets for container %s",
        len(points),
        source_resolution,
        len(buckets),
        target_resolution,
        container_id,
    )


def _prune_old_points(container_id: str, now: datetime) -> None:
    """Delete all points for a container older than the absolute maximum age.

    anything older than 90 days is deleted outright,
    regardless of resolution. This prevents unbounded growth of the
    TinyFlux CSV file over the months.
    """
    store = get_store()
    Tag = TagQuery()
    Time = TimeQuery()

    cutoff = now - _ABSOLUTE_MAX_AGE

    removed = store.remove(
        (Tag.container_id == container_id)
        & (Time < cutoff)
    )
    if removed:
        log.info(
            "Pruned %d points older than %d days for container %s",
            removed,
            _ABSOLUTE_MAX_AGE.days,
            container_id,
        )


def run_retention_pass(now: datetime | None = None) -> None:
    """Run one full retention and downsampling pass over all active containers.

    Implement three steps per container:
    1. Downsample raw → 5m for points older than 24 hours.
    2. Downsample 5m → 1h for points older than 7 days.
    3. Delete outright anything older than 90 days (any resolution).

    The optional `now` parameter lets the retention test inject a synthetic
    "current time" so it can insert old-timestamped points and assert the
    correct state without actually waiting 24+ hours.

    Called once per hour from the collector main loop. It is NOT a separate
    process — it runs on an in-memory hourly tick within run_collector_loop().
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    containers = repo.list_active_containers()
    log.info("Running retention pass for %d container(s)", len(containers))

    for container in containers:
        cid = container["id"]

        try:
            # Step 1: raw points older than 24h → 5m averages
            _downsample_and_prune(
                container_id=cid,
                source_resolution="raw",
                target_resolution="5m",
                older_than=_RAW_MAX_AGE,
                bucket_width=_FIVE_MIN_BUCKET,
                now=now,
            )

            # Step 2: 5m points older than 7 days → 1h averages
            _downsample_and_prune(
                container_id=cid,
                source_resolution="5m",
                target_resolution="1h",
                older_than=_FIVE_MIN_MAX_AGE,
                bucket_width=_ONE_HOUR_BUCKET,
                now=now,
            )

            # Step 3: delete anything older than 90 days
            _prune_old_points(cid, now=now)

        except Exception as exc:  # noqa: BLE001
            # Never let one container's retention failure stop the others
            log.warning(
                "Retention pass failed for container %s: %s — skipping",
                cid,
                exc,
            )
