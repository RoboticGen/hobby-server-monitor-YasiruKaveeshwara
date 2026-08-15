"""
Retention and downsampling job for TinyFlux.

 "Raw points are kept for 24 hours. Points older than 24 hours are downsampled into 5-minute averages.
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

"Downsampled into averages" is the brief's wording and describes the gauges
correctly, but not every field is a gauge. Three of the seven the collector
writes are monotonic counters and one is a high-water mark; averaging those
produces numbers that were never read and, for the counters, breaks the
subtraction the charts do. See the field classification below for which
reduction each field gets and why.

Composing the two tiers is safe for every reduction used here: the last of a
series of last-values is still the last value, and the max of a series of
maxima is still the max. The mean of means is only exactly the overall mean
when the buckets hold equal sample counts, which is the normal case at a fixed
collector interval and off by a fraction of a percent when a collection was
missed.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from tinyflux import TagQuery, TimeQuery

from backend.db import repo
from backend.tsdb.store import get_store, remove_points, write_point

log = logging.getLogger(__name__)

# thresholds
_RAW_MAX_AGE = timedelta(hours=24)  # raw points older than this → 5m
_FIVE_MIN_MAX_AGE = timedelta(days=7)  # 5m points older than this → 1h
_ABSOLUTE_MAX_AGE = timedelta(days=90)  # anything older → delete outright

# Bucket widths for downsampling
_FIVE_MIN_BUCKET = timedelta(minutes=5)
_ONE_HOUR_BUCKET = timedelta(hours=1)

# --------------------------------------------------------------------------
# How each metric field is reduced when a bucket collapses to one point.
#
# Which reduction is correct depends on what the field *means*, not on its
# type — every field here is a float. Averaging is right for a gauge and
# wrong for a counter, so the two cannot share a code path.
#
# Every field lxd.client.get_container_state() produces is classified below.
# test_retention.py::test_every_collected_field_is_classified checks that
# against the real function rather than a copied list, so adding a metric
# without classifying it fails the suite instead of silently inheriting the
# averaging default — which is the wrong reduction if the new field counts.
# --------------------------------------------------------------------------

# Monotonically increasing totals, reported by LXD as "since this container
# started". No consumer reads them directly as a level: computeCpuPercent and
# computeRate in the frontend both subtract the previous point to get a rate.
#
# Keeping the *last* reading in the bucket is what makes that subtraction come
# out right. Consecutive rolled-up points then differ by exactly the amount the
# counter advanced between them, whatever shape the traffic had inside the
# bucket, and the stored number is still a total the counter genuinely reached.
#
# The mean fails on both counts. It is a value the counter held only in
# passing, so it is wrong wherever the field is read as a level ("N bytes
# received") — low by up to a bucket's worth of traffic. And it only
# reconstructs the rate correctly when the counter advanced uniformly across
# the bucket: a burst early in one bucket pulls that bucket's mean below its
# final reading, so part of the burst is attributed to the *following* bucket.
# The chart then understates the spike and draws activity in a window where
# there was none. test_retention.py::TestCounterRateFidelity pins the numbers.
_COUNTER_FIELDS = frozenset({"cpu_usage_ns", "net_rx_bytes", "net_tx_bytes"})

# High-water marks: the largest value seen so far, not a running total.
# The maximum over a bucket is itself a peak that really occurred, whereas a
# mean of peaks is smaller than every peak it summarises — it understates the
# one thing the field exists to report.
_PEAK_FIELDS = frozenset({"ram_peak_mb"})

# Instantaneous readings. Here the mean is the honest summary: it answers
# "what was this value during the window", which is what a downsampled chart
# is asking.
#
# _reduce_fields does not branch on this set — averaging is its fallback, so
# listing a gauge changes no behaviour. The set exists to make the
# classification total: the completeness test unions all three and requires
# every collected field to appear in one of them, which is what turns "I forgot
# to classify a new field" from a silent wrong average into a failing test.
_GAUGE_FIELDS = frozenset({"ram_used_mb", "disk_used_mb", "pid_count"})


def _bucket_key(ts: datetime, bucket_width: timedelta) -> datetime:
    """Snap a UTC timestamp to the start of its bucket.

    E.g. with a 5-minute bucket, 12:07:33 → 12:05:00.
    Used to group points into the windows that get rolled up together.
    """
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    bucket_index = int((ts - epoch).total_seconds() // bucket_width.total_seconds())
    return epoch + timedelta(seconds=bucket_index * bucket_width.total_seconds())


def _reduce_fields(points: list) -> dict:
    """Collapse a bucket of TinyFlux Points into one dict of field values.

    Each field is reduced according to its classification above: counters keep
    their last reading, peaks keep their maximum, everything else is averaged.

    Points are sorted by time here rather than trusted in the order the store
    returned them. "Last reading" is only meaningful against a known order, and
    insertion order is not it — retention writes back-dated rollup points, so
    the file is not strictly chronological.

    Unclassified numeric fields are averaged. That is the safe default for a
    gauge-like value and the wrong one for a counter, which is why the fields
    the collector actually writes are pinned by a test instead of relying on
    this fallback.

    Non-numeric readings are skipped. TinyFlux refuses to store a string field
    at all, but it does accept None, and one None in a bucket would make sum()
    raise — inside run_retention_pass's per-container try/except that surfaces
    as a skipped container and a missing bucket rather than a crash.
    """
    if not points:
        return {}

    ordered = sorted(points, key=lambda p: p.time)

    # Union across all points, not just the first: a field can appear midway
    # through a bucket (a container gaining a network interface, or a schema
    # that grew between two collector runs), and dropping it here would erase
    # data the rollup was supposed to preserve.
    keys: set[str] = set()
    for p in ordered:
        keys.update(k for k, v in p.fields.items() if isinstance(v, (int, float)))

    reduced: dict[str, float] = {}
    for key in keys:
        # Only points that actually carry the field take part. Treating a
        # missing field as 0.0 would drag a mean toward zero, and for a
        # counter it would look like a reset.
        values = [
            p.fields[key]
            for p in ordered
            if isinstance(p.fields.get(key), (int, float))
        ]
        if not values:
            continue

        if key in _COUNTER_FIELDS:
            reduced[key] = float(values[-1])
        elif key in _PEAK_FIELDS:
            reduced[key] = float(max(values))
        else:
            reduced[key] = sum(values) / len(values)

    return reduced


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
    3. Writes one rolled-up point per bucket at target_resolution.
    4. Deletes the original source points that were rolled up.

    Each bucket's point is timestamped at the bucket's *start*, so a counter
    field (which keeps the bucket's final reading) is labelled up to one bucket
    earlier than the instant it was read. The interval between consecutive
    rolled-up points is still exactly bucket_width, so rates computed from them
    are correct; only the absolute placement on the x-axis shifts, by at most
    5 minutes on the 24h chart and an hour on the 7d one.

    Args:
        container_id: Container UUID to process.
        source_resolution: Input resolution tag ("raw" or "5m").
        target_resolution: Output resolution tag ("5m" or "1h").
        older_than: Only process points older than (now - older_than).
        bucket_width: Time window to reduce within.
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

    # Write one rolled-up point per bucket at the target resolution
    for bucket_start, bucket_points in buckets.items():
        rolled_fields = _reduce_fields(bucket_points)
        if rolled_fields:
            write_point(
                container_id=container_id,
                fields=rolled_fields,
                resolution=target_resolution,
                timestamp=bucket_start,
            )

    # Delete the original source points now that they've been rolled up.
    # We remove them by matching the same query used to fetch them.
    # remove_points, not store.remove: the back-dated writes above put the
    # time index in the state where TinyFlux's remove() leaves it stale but
    # flagged valid. See tsdb.store.remove_points.
    remove_points(
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
    Tag = TagQuery()
    Time = TimeQuery()

    cutoff = now - _ABSOLUTE_MAX_AGE

    removed = remove_points((Tag.container_id == container_id) & (Time < cutoff))
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
