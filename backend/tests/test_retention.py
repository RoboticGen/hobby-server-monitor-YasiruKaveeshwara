"""
Tests for the retention and downsampling job (Step 10.2).

Calls the internal retention functions (_downsample_and_prune,
_prune_old_points) directly with explicit container IDs and synthetic
timestamps so tests are fully isolated from the shared DB session and
from the repo.list_active_containers() path.

Implement three steps per container:
- Raw points older than 24h -> rolled into 5m buckets, originals deleted.
- 5m points older than 7 days -> rolled into 1h buckets, originals deleted.
- Any points older than 90 days -> deleted outright.

How a bucket is reduced depends on the field: gauges are averaged, monotonic
counters keep their last reading, high-water marks keep their maximum.
TestFieldReductions covers why, and is the part of this file most worth
reading — the rest is threshold arithmetic.
"""

import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from tinyflux import Point, TinyFlux

import backend.tsdb.store as store_module
from backend.tsdb.store import write_point, query_range
from backend.collector.retention import (
    _downsample_and_prune,
    _prune_old_points,
    _reduce_fields,
    _COUNTER_FIELDS,
    _GAUGE_FIELDS,
    _PEAK_FIELDS,
    _RAW_MAX_AGE,
    _FIVE_MIN_MAX_AGE,
    _FIVE_MIN_BUCKET,
    _ONE_HOUR_BUCKET,
)


def _cid() -> str:
    """Return a unique container UUID for each call."""
    return f"ret-{uuid.uuid4().hex[:12]}"


def _point(ts: datetime, fields: dict) -> Point:
    """A bare TinyFlux Point for reduction tests.

    _reduce_fields only reads .time and .fields, so these never go near the
    store — building them directly keeps the arithmetic tests independent of
    write/query behaviour and of TinyFlux's index quirks.
    """
    return Point(time=ts, tags={"container_id": "unused"}, fields=fields)


def _bucket_aligned(ts: datetime) -> datetime:
    """Snap a timestamp down to a 5-minute boundary.

    Tests that count buckets need their first sample to sit at the start of
    one, or the samples straddle an extra bucket and the count is off by one
    for reasons unrelated to what is being tested.
    """
    return ts.replace(minute=ts.minute - (ts.minute % 5), second=0, microsecond=0)


def _collected_field_names() -> list[str]:
    """The field names the collector actually writes, from the real code path.

    get_container_state builds its dict by reading a pylxd state object, so it
    is called here against the integration suite's fake LXD rather than having
    the field list restated in this file — a copied list would agree with
    itself forever while the real one drifted.
    """
    from backend.lxd import client as lxd_client_module
    from backend.tests.integration.fake_lxd import FakeLXDClient

    fake = FakeLXDClient()
    fake.add("classification-probe")

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(lxd_client_module, "_get_client", lambda: fake)
        return list(lxd_client_module.get_container_state("classification-probe"))
    finally:
        mp.undo()


@pytest.fixture(autouse=True, scope="module")
def isolated_retention_store():
    """Inject a fresh temp TinyFlux instance for the entire retention test module."""
    tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
    tmp.close()

    fresh = TinyFlux(tmp.name)
    store_module._store = fresh

    yield

    fresh.close()
    if os.path.exists(tmp.name):
        os.remove(tmp.name)
    store_module._store = None


@pytest.fixture(autouse=True)
def clear_store():
    """Reset TinyFlux to a clean state before each test.

    TinyFlux's remove_all() clears rows but leaves the in-memory index
    in an inconsistent state, causing subsequent inserts to be invisible
    to queries. The reliable fix is to close the store, truncate the CSV
    file, and reopen it — giving a truly fresh instance each time.
    """
    # Close existing store and wipe the CSV
    current = store_module._store
    path = current.storage._path  # internal path to the CSV file
    current.close()

    # Truncate the file (wipes data without deleting it)
    open(path, "w").close()

    # Reopen a fresh TinyFlux instance on the same file
    store_module._store = TinyFlux(path)


# Fixed anchor for all time calculations
_NOW = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
_30H_AGO = _NOW - timedelta(hours=30)
_10D_AGO = _NOW - timedelta(days=10)
_100D_AGO = _NOW - timedelta(days=100)
_1H_AGO = _NOW - timedelta(hours=1)


class TestRawToFiveMinDownsample:
    """Raw points > 24h old are rolled up into 5m buckets."""

    def test_old_raw_points_are_removed(self):
        """After downsample, raw points older than 24h are gone."""
        cid = _cid()
        write_point(cid, {"cpu_usage_ns": 100.0}, resolution="raw", timestamp=_30H_AGO)
        write_point(
            cid,
            {"cpu_usage_ns": 200.0},
            resolution="raw",
            timestamp=_30H_AGO + timedelta(minutes=2),
        )

        _downsample_and_prune(cid, "raw", "5m", _RAW_MAX_AGE, _FIVE_MIN_BUCKET, _NOW)

        raw_left = query_range(
            cid,
            start=_30H_AGO - timedelta(hours=1),
            end=_30H_AGO + timedelta(hours=1),
            resolution="raw",
        )
        assert raw_left == []

    def test_five_min_rollup_point_is_created(self):
        """One 5m point per bucket, each field reduced by its own rule.

        Both field kinds are written into the same bucket so this also proves
        the two reductions coexist rather than one winning for the whole point.
        """
        cid = _cid()
        write_point(
            cid,
            {"cpu_usage_ns": 100.0, "ram_used_mb": 100.0},
            resolution="raw",
            timestamp=_30H_AGO,
        )
        write_point(
            cid,
            {"cpu_usage_ns": 200.0, "ram_used_mb": 200.0},
            resolution="raw",
            timestamp=_30H_AGO + timedelta(minutes=2),
        )

        _downsample_and_prune(cid, "raw", "5m", _RAW_MAX_AGE, _FIVE_MIN_BUCKET, _NOW)

        five_min = query_range(
            cid,
            start=_30H_AGO - timedelta(hours=1),
            end=_30H_AGO + timedelta(hours=1),
            resolution="5m",
        )
        assert len(five_min) == 1
        # ram_used_mb is a gauge: the mean is what the window looked like.
        assert five_min[0]["ram_used_mb"] == pytest.approx(150.0)
        # cpu_usage_ns is a counter: the mean (150) is a total the counter
        # never reported, and it would understate the work done by the end of
        # the bucket.
        assert five_min[0]["cpu_usage_ns"] == pytest.approx(200.0)

    def test_recent_raw_points_are_kept(self):
        """Raw points less than 24h old are NOT touched by the raw to 5m step."""
        cid = _cid()
        write_point(cid, {"cpu_usage_ns": 50.0}, resolution="raw", timestamp=_30H_AGO)
        write_point(cid, {"cpu_usage_ns": 75.0}, resolution="raw", timestamp=_1H_AGO)

        _downsample_and_prune(cid, "raw", "5m", _RAW_MAX_AGE, _FIVE_MIN_BUCKET, _NOW)

        # Query all raw points within the last 48h — should contain only the
        # 1h-ago point (the 30h-ago one was rolled up). Use a wide end bound to
        # avoid any off-by-one issues with TinyFlux's <= operator.
        recent = query_range(
            cid,
            start=_NOW - timedelta(hours=48),
            end=_NOW + timedelta(hours=1),
            resolution="raw",
        )
        assert len(recent) == 1
        assert recent[0]["cpu_usage_ns"] == pytest.approx(75.0)


class TestFiveMinToOneHourDownsample:
    """5m points > 7 days old are rolled up into 1h buckets."""

    def test_old_5m_points_are_removed(self):
        """After 5m to 1h downsample, old 5m points are gone."""
        cid = _cid()
        write_point(cid, {"cpu_usage_ns": 300.0}, resolution="5m", timestamp=_10D_AGO)
        write_point(
            cid,
            {"cpu_usage_ns": 400.0},
            resolution="5m",
            timestamp=_10D_AGO + timedelta(minutes=30),
        )

        _downsample_and_prune(
            cid, "5m", "1h", _FIVE_MIN_MAX_AGE, _ONE_HOUR_BUCKET, _NOW
        )

        five_min_left = query_range(
            cid,
            start=_10D_AGO - timedelta(hours=1),
            end=_10D_AGO + timedelta(hours=2),
            resolution="5m",
        )
        assert five_min_left == []

    def test_one_hour_rollup_point_is_created(self):
        """The second tier applies the same per-field rules as the first.

        Both tiers share _reduce_fields, so this is a regression guard against
        someone giving the coarser tier its own reduction.
        """
        cid = _cid()
        write_point(
            cid,
            {"cpu_usage_ns": 300.0, "ram_used_mb": 300.0},
            resolution="5m",
            timestamp=_10D_AGO,
        )
        write_point(
            cid,
            {"cpu_usage_ns": 400.0, "ram_used_mb": 400.0},
            resolution="5m",
            timestamp=_10D_AGO + timedelta(minutes=30),
        )

        _downsample_and_prune(
            cid, "5m", "1h", _FIVE_MIN_MAX_AGE, _ONE_HOUR_BUCKET, _NOW
        )

        one_hour = query_range(
            cid,
            start=_10D_AGO - timedelta(hours=1),
            end=_10D_AGO + timedelta(hours=2),
            resolution="1h",
        )
        assert len(one_hour) == 1
        assert one_hour[0]["ram_used_mb"] == pytest.approx(350.0)
        assert one_hour[0]["cpu_usage_ns"] == pytest.approx(400.0)


class TestFieldReductions:
    """Which reduction each field gets, and why the choice is not arbitrary.

    _reduce_fields is called directly here. The rollup path around it is
    covered above; these tests are about the arithmetic inside one bucket.
    """

    def test_gauges_are_averaged(self):
        """A gauge answers "what was this during the window" — that is a mean."""
        points = [
            _point(
                _30H_AGO, {"ram_used_mb": 100.0, "disk_used_mb": 10.0, "pid_count": 4.0}
            ),
            _point(
                _30H_AGO + timedelta(seconds=30),
                {"ram_used_mb": 300.0, "disk_used_mb": 20.0, "pid_count": 8.0},
            ),
        ]
        reduced = _reduce_fields(points)

        assert reduced["ram_used_mb"] == pytest.approx(200.0)
        assert reduced["disk_used_mb"] == pytest.approx(15.0)
        assert reduced["pid_count"] == pytest.approx(6.0)

    def test_counters_keep_the_last_reading(self):
        """A counter's value at the end of the bucket is the running total."""
        points = [
            _point(
                _30H_AGO,
                {"cpu_usage_ns": 1_000.0, "net_rx_bytes": 500.0, "net_tx_bytes": 100.0},
            ),
            _point(
                _30H_AGO + timedelta(seconds=30),
                {"cpu_usage_ns": 4_000.0, "net_rx_bytes": 900.0, "net_tx_bytes": 300.0},
            ),
        ]
        reduced = _reduce_fields(points)

        assert reduced["cpu_usage_ns"] == pytest.approx(4_000.0)
        assert reduced["net_rx_bytes"] == pytest.approx(900.0)
        assert reduced["net_tx_bytes"] == pytest.approx(300.0)

    def test_counters_are_not_averaged(self):
        """Stated as its own assertion because it is the bug being fixed.

        The mean of these two readings is 2500, which the counter reported at
        no point and which understates the work done by the end of the bucket.
        """
        points = [
            _point(_30H_AGO, {"cpu_usage_ns": 1_000.0}),
            _point(_30H_AGO + timedelta(seconds=30), {"cpu_usage_ns": 4_000.0}),
        ]
        assert _reduce_fields(points)["cpu_usage_ns"] != pytest.approx(2_500.0)

    def test_counter_reduction_ignores_input_order(self):
        """The store does not return points chronologically.

        Retention writes back-dated rollup points, so the CSV is not sorted by
        time. "Last reading" therefore has to mean latest timestamp, not last
        element — reversing the input must not change the answer.
        """
        early = _point(_30H_AGO, {"cpu_usage_ns": 1_000.0})
        late = _point(_30H_AGO + timedelta(minutes=4), {"cpu_usage_ns": 9_000.0})

        assert _reduce_fields([late, early])["cpu_usage_ns"] == pytest.approx(9_000.0)
        assert _reduce_fields([early, late])["cpu_usage_ns"] == pytest.approx(9_000.0)

    def test_peak_keeps_the_maximum(self):
        """ram_peak_mb is LXD's memory.usage_peak — a high-water mark.

        Averaging peaks produces a number below every peak it summarises,
        which defeats the only purpose the field has.
        """
        points = [
            _point(_30H_AGO, {"ram_peak_mb": 700.0}),
            _point(_30H_AGO + timedelta(seconds=30), {"ram_peak_mb": 900.0}),
            _point(_30H_AGO + timedelta(seconds=60), {"ram_peak_mb": 900.0}),
        ]
        reduced = _reduce_fields(points)

        assert reduced["ram_peak_mb"] == pytest.approx(900.0)
        # The mean would be ~833: lower than the peak actually recorded.
        assert reduced["ram_peak_mb"] > 833.4

    def test_a_counter_reset_is_preserved_not_masked(self):
        """Why last-value rather than max, which is identical on clean data.

        A container restart sets LXD's counters back to zero, so a bucket can
        straddle a reset. Both reductions agree on every monotonic bucket —
        max == last when nothing decreases — and this is the case that
        separates them.

        Keeping the last reading (500) leaves the drop visible: the delta from
        the previous bucket goes negative, the frontend recognises that as a
        reset and drops the single interval it cannot know, and every interval
        after it is measured from the correct new baseline.

        max would report 95000 — the pre-reset high — which invents traffic
        that never happened in this bucket and pushes the unavoidable gap into
        the *next* interval, where the reset did not occur.
        """
        points = [
            _point(_30H_AGO, {"net_rx_bytes": 90_000.0}),
            _point(_30H_AGO + timedelta(seconds=30), {"net_rx_bytes": 95_000.0}),
            _point(_30H_AGO + timedelta(seconds=60), {"net_rx_bytes": 0.0}),
            _point(_30H_AGO + timedelta(seconds=90), {"net_rx_bytes": 500.0}),
        ]
        reduced = _reduce_fields(points)

        assert reduced["net_rx_bytes"] == pytest.approx(500.0), (
            "the rollup reported a value from before the counter reset, "
            "which fabricates traffic and hides the restart"
        )

    def test_missing_field_does_not_count_as_zero(self):
        """A point that lacks a field must not be treated as reporting 0.

        The pre-fix code used `p.fields.get(key, 0.0)`, which divides by the
        full bucket size regardless. One absent reading then halves a gauge —
        indistinguishable from real idleness — and for a counter it looks like
        the container restarted.
        """
        points = [
            _point(_30H_AGO, {"ram_used_mb": 400.0, "cpu_usage_ns": 900.0}),
            _point(_30H_AGO + timedelta(seconds=30), {"ram_used_mb": 400.0}),
        ]
        reduced = _reduce_fields(points)

        assert reduced["ram_used_mb"] == pytest.approx(400.0)
        assert reduced["cpu_usage_ns"] == pytest.approx(900.0)

    def test_null_readings_are_skipped_not_summed(self):
        """TinyFlux permits None as a field value, and None breaks arithmetic.

        A string cannot reach _reduce_fields — TinyFlux refuses to construct a
        Point with one — but None it accepts, and `sum()` over a list holding
        one raises TypeError. Since the rollup runs inside a per-container
        try/except, that would show up as a skipped container in the log and a
        silently missing bucket rather than as a crash.
        """
        points = [
            _point(_30H_AGO, {"ram_used_mb": 100.0, "disk_used_mb": None}),
            _point(
                _30H_AGO + timedelta(seconds=30),
                {"ram_used_mb": 200.0, "disk_used_mb": None},
            ),
        ]
        reduced = _reduce_fields(points)

        assert reduced["ram_used_mb"] == pytest.approx(150.0)
        assert (
            "disk_used_mb" not in reduced
        ), "a field with no numeric readings became a value in the rollup"

    def test_empty_bucket_reduces_to_nothing(self):
        """_downsample_and_prune skips writing when this is empty, so an empty
        bucket must not become a point with no fields."""
        assert _reduce_fields([]) == {}

    def test_every_collected_field_is_classified(self):
        """The classification must cover everything the collector writes.

        Checked against lxd.client.get_container_state's real return value, not
        a hardcoded list, so a field added there and forgotten here fails this
        test instead of silently falling through to the averaging default —
        which is the wrong reduction if the new field is a counter.
        """
        classified = _COUNTER_FIELDS | _GAUGE_FIELDS | _PEAK_FIELDS
        collected = set(_collected_field_names())

        assert collected, "could not determine the collector's field names"
        assert collected <= classified, (
            f"unclassified metric field(s): {sorted(collected - classified)} — "
            f"add each to _COUNTER_FIELDS, _GAUGE_FIELDS, or _PEAK_FIELDS in "
            f"retention.py after deciding which reduction is correct for it"
        )

    def test_no_field_is_classified_twice(self):
        """Overlapping sets would make the reduction depend on branch order."""
        assert not (_COUNTER_FIELDS & _GAUGE_FIELDS)
        assert not (_COUNTER_FIELDS & _PEAK_FIELDS)
        assert not (_GAUGE_FIELDS & _PEAK_FIELDS)


class TestCounterRateFidelity:
    """The reason last-value is correct: the frontend charts rates, not levels.

    ContainerResourceGraphs.tsx computes every counter series by subtracting
    consecutive points — `current.cpu_usage_ns - previous.cpu_usage_ns` over
    the elapsed time. These tests do that same subtraction over rolled-up
    points and check the rate that comes out, which is what a viewer sees.
    """

    @staticmethod
    def _rollup(cid: str, samples: list[tuple[datetime, float]]) -> list[dict]:
        """Write raw counter samples, roll them up, return the 5m points."""
        for ts, value in samples:
            write_point(cid, {"net_rx_bytes": value}, resolution="raw", timestamp=ts)

        _downsample_and_prune(cid, "raw", "5m", _RAW_MAX_AGE, _FIVE_MIN_BUCKET, _NOW)

        points = query_range(
            cid,
            start=_30H_AGO - timedelta(hours=1),
            end=_30H_AGO + timedelta(hours=2),
            resolution="5m",
        )
        return sorted(points, key=lambda p: p["time"])

    @staticmethod
    def _rates(points: list[dict]) -> list[float]:
        """Bytes per second between consecutive points — the frontend's math."""
        rates = []
        for previous, current in zip(points, points[1:]):
            elapsed = (
                datetime.fromisoformat(current["time"])
                - datetime.fromisoformat(previous["time"])
            ).total_seconds()
            delta = current["net_rx_bytes"] - previous["net_rx_bytes"]
            rates.append(delta / elapsed)
        return rates

    def test_steady_traffic_survives_the_rollup(self):
        """A constant 100 B/s must still read as 100 B/s after downsampling.

        This one passes under averaging too, and that is worth stating: the
        mean of a uniform ramp is its midpoint, and consecutive midpoints are
        exactly one bucket's growth apart, so a perfectly steady counter comes
        out right either way. That is why the bug went unnoticed — a test with
        flat synthetic data cannot see it. Kept as the guard that the fix does
        not break the easy case; the burst test below is the discriminating one.
        """
        cid = _cid()
        # Bucket-aligned start, 30s spacing, 20 minutes -> 4 full buckets.
        base = _bucket_aligned(_30H_AGO)
        samples = [
            (base + timedelta(seconds=30 * i), 100.0 * 30 * i) for i in range(40)
        ]

        points = self._rollup(cid, samples)
        assert len(points) == 4, f"expected 4 buckets, got {len(points)}"

        for rate in self._rates(points):
            assert rate == pytest.approx(
                100.0, rel=1e-6
            ), f"a steady 100 B/s stream reads as {rate:.1f} B/s after rollup"

    def test_a_burst_stays_in_the_bucket_it_happened_in(self):
        """The case averaging gets wrong, and the reason this fix exists.

        60 kB arrives in one go halfway through bucket 2. Buckets 1 and 3 are
        idle. So the 1→2 interval carries all of it — 60 kB over 300 s, or
        200 B/s — and the 2→3 interval carries none.

        Averaging halves it. Bucket 2's mean lands at 30 kB (half its samples
        still read zero) while buckets 1 and 3 average to their exact values,
        so both intervals come out at 100 B/s: the spike is reported at half
        its true height, and the idle window afterwards is drawn as though the
        traffic were still flowing.

        The burst has to fall *inside* a bucket for this to be visible. A burst
        landing exactly on a bucket boundary averages to the same answer as
        last-value, so a boundary-aligned version of this test would pass
        either way.
        """
        cid = _cid()
        base = _bucket_aligned(_30H_AGO)
        burst_at = _FIVE_MIN_BUCKET + (_FIVE_MIN_BUCKET / 2)  # 450 s in

        samples = []
        for i in range(30):  # 15 minutes at 30s spacing -> 3 buckets
            elapsed = timedelta(seconds=30 * i)
            total = 0.0 if elapsed < burst_at else 60_000.0
            samples.append((base + elapsed, total))

        points = self._rollup(cid, samples)
        assert len(points) == 3, f"expected 3 buckets, got {len(points)}"

        first_interval, second_interval = self._rates(points)

        assert first_interval == pytest.approx(200.0), (
            f"the burst reads as {first_interval:.1f} B/s; averaging reports "
            f"~100 B/s here and leaks the rest into the next interval"
        )
        assert second_interval == pytest.approx(
            0.0
        ), f"an idle window reads as {second_interval:.1f} B/s of traffic"

    def test_rolled_up_counters_never_go_backwards(self):
        """Monotonicity has to survive the rollup.

        The frontend discards any negative delta (`if (delta < 0) return null`)
        because that means a counter reset. A reduction that let a later bucket
        fall below an earlier one would silently blank the chart instead of
        drawing a wrong number.
        """
        cid = _cid()
        base = _bucket_aligned(_30H_AGO)
        # Wildly uneven traffic: bursts, idle stretches, then a burst again.
        pattern = [0, 0, 5_000, 5_000, 5_000, 5_000, 90_000, 90_000, 90_100, 91_000]
        samples = [
            (base + timedelta(seconds=30 * i), float(total))
            for i, total in enumerate(pattern * 3)
        ]

        points = self._rollup(cid, samples)
        values = [p["net_rx_bytes"] for p in points]

        assert len(values) >= 2, "not enough buckets to compare"
        for previous, current in zip(values, values[1:]):
            assert (
                current >= previous
            ), f"a rolled-up counter went backwards: {previous} -> {current}"


class TestAbsolutePrune:
    """Points > 90 days old are deleted outright regardless of resolution."""

    def test_100_day_old_raw_points_are_pruned(self):
        """Raw points older than 90 days are deleted by _prune_old_points."""
        cid = _cid()
        write_point(cid, {"cpu_usage_ns": 1.0}, resolution="raw", timestamp=_100D_AGO)

        _prune_old_points(cid, now=_NOW)

        raw = query_range(
            cid,
            start=_100D_AGO - timedelta(days=5),
            end=_100D_AGO + timedelta(days=5),
            resolution="raw",
        )
        assert raw == []

    def test_100_day_old_5m_points_are_pruned(self):
        """5m-averaged points older than 90 days are also pruned."""
        cid = _cid()
        write_point(cid, {"cpu_usage_ns": 2.0}, resolution="5m", timestamp=_100D_AGO)

        _prune_old_points(cid, now=_NOW)

        pts = query_range(
            cid,
            start=_100D_AGO - timedelta(days=5),
            end=_100D_AGO + timedelta(days=5),
            resolution="5m",
        )
        assert pts == []

    def test_100_day_old_1h_points_are_pruned(self):
        """1h-averaged points older than 90 days are also pruned."""
        cid = _cid()
        write_point(cid, {"cpu_usage_ns": 3.0}, resolution="1h", timestamp=_100D_AGO)

        _prune_old_points(cid, now=_NOW)

        pts = query_range(
            cid,
            start=_100D_AGO - timedelta(days=5),
            end=_100D_AGO + timedelta(days=5),
            resolution="1h",
        )
        assert pts == []
