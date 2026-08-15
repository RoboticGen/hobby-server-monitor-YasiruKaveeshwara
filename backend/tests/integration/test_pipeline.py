"""
The metrics pipeline end to end: LXD state → collector → TinyFlux → API.

The unit suite tests retention by writing points directly into TinyFlux with
synthetic timestamps. That verifies the rollup arithmetic but skips the two
seams that actually break: whether the collector writes points in the shape
retention expects, and whether the API can still read what retention produced.

Here the data always originates from the fake LXD daemon and travels the real
path. Retention is then aged forward with an injected `now`, and the assertions
are made through the HTTP endpoints — so a rollup that produced technically
correct points the chart endpoint cannot read would still fail.
"""

from datetime import datetime, timedelta, timezone

import pytest
from falcon import testing

import backend.collector.collector as collector_module
from backend.app import create_app
from backend.collector.retention import run_retention_pass
from backend.db import repo
from backend.tsdb import store

from .conftest import make_user

pytestmark = pytest.mark.usefixtures("isolated_db", "isolated_tsdb")

# A fixed reference time. Date.now-style drift in a test that asserts on
# bucket boundaries makes failures irreproducible, so every timestamp here is
# derived from this constant.
NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


class _StopLoop(Exception):
    """Breaks out of the collector's infinite loop after one pass."""


def _search_by_tag(container_id: str, resolution: str) -> list:
    """Return points by tag only, bypassing TinyFlux's time index.

    A time-bounded read goes through the index, which retention corrupts
    (see test_rolled_up_points_are_readable_by_time_range). Tests that want
    to assert on what retention *wrote* need a read path that the stale
    index cannot distort, or they fail for the wrong reason.
    """
    from tinyflux import TagQuery

    tag = TagQuery()
    points = store.get_store().search(
        (tag.container_id == container_id) & (tag.resolution == resolution)
    )
    return sorted(points, key=lambda p: p.time)


@pytest.fixture(scope="module")
def pipeline_env(isolated_db, isolated_tsdb, fake_lxd):
    """An admin, a user, and two containers with different LXD state."""
    client = testing.TestClient(create_app())
    admin_id, admin_headers = make_user("pipe-admin@example.com", role="admin")
    user_id, user_headers = make_user(
        "pipe-user@example.com", ram=8192, cpu=8.0, disk=80
    )

    ids = {}
    for name in ["pipe-one", "pipe-two"]:
        fake_lxd.add(name)
        cid = repo.create_container_record(
            lxd_name=name,
            image="ubuntu:22.04",
            created_by=admin_id,
            limit_ram_mb=1024,
            limit_cpu=1.0,
            limit_disk_gb=10,
        )
        repo.assign_container(user_id, cid)
        ids[name] = cid

    return {
        "client": client,
        "admin": admin_headers,
        "user": user_headers,
        "ids": ids,
        "lxd": fake_lxd,
        "user_id": user_id,
        "admin_id": admin_id,
    }


def _run_one_collector_pass(monkeypatch=None):
    """Execute exactly one iteration of the real collector loop.

    run_collector_loop never returns on its own, so time.sleep is replaced
    with a raise: the loop body runs in full — including its per-container
    error handling — and then unwinds at the sleep call.

    The loop also fires a retention pass on its first iteration, using the
    real wall clock. That would prune this module's deliberately-aged points
    before the retention tests could assert on them, so retention is stubbed
    out here. It is exercised directly, with a controlled `now`, in
    TestRetentionThroughTheApi.

    Callers outside a test function pass no monkeypatch; a private one is
    created and undone here so the patches never outlive the pass.
    """
    own = monkeypatch is None
    mp = pytest.MonkeyPatch() if own else monkeypatch
    mp.setattr(
        collector_module.time,
        "sleep",
        lambda _s: (_ for _ in ()).throw(_StopLoop()),
    )
    mp.setattr(collector_module, "run_retention_pass", lambda now=None: None)
    try:
        with pytest.raises(_StopLoop):
            collector_module.run_collector_loop()
    finally:
        if own:
            mp.undo()


class TestCollectorToStore:
    """What the collector writes must match what LXD reported."""

    def test_collector_converts_lxd_units_correctly(self, pipeline_env, monkeypatch):
        """Bytes from LXD become MB in TinyFlux, without rounding drift.

        The fake reports 268,435,456 bytes of RAM — exactly 256 MiB. A point
        reading 268435456 would mean the conversion never happened; 268.4
        would mean it used decimal MB instead of binary.
        """
        container = pipeline_env["lxd"].containers._store["pipe-one"]
        container.state_obj.memory.usage = 268_435_456.0  # 256 MiB
        container.state_obj.disk["root"].usage = 2_147_483_648.0  # 2 GiB
        container.state_obj.processes = 42

        _run_one_collector_pass(monkeypatch)

        point = store.get_latest_point(pipeline_env["ids"]["pipe-one"])
        assert point is not None
        assert point["ram_used_mb"] == pytest.approx(256.0)
        # get_container_state reports disk in MB, not GB: 2 GiB → 2048 MB.
        assert point["disk_used_mb"] == pytest.approx(2048.0)
        assert point["pid_count"] == 42.0

    def test_collector_writes_one_point_per_active_container(
        self, pipeline_env, monkeypatch
    ):
        """Every active container gets a point in a single pass."""
        for cid in pipeline_env["ids"].values():
            assert (
                store.get_latest_point(cid) is not None
            ), "a container assigned and active got no metrics point"

    def test_collector_tags_points_as_raw(self, pipeline_env):
        """New points must be tagged raw, or retention will never roll them up."""
        point = store.get_latest_point(pipeline_env["ids"]["pipe-one"])
        assert point["resolution"] == "raw"

    def test_collector_skips_soft_deleted_containers(self, pipeline_env, monkeypatch):
        """A deleted container must not keep generating metrics.

        It is gone from LXD, so polling it would log an error every interval
        forever — noise that would mask real failures.
        """
        cid = repo.create_container_record(
            lxd_name="pipe-ghost",
            image="ubuntu:22.04",
            created_by=pipeline_env["admin_id"],
            limit_ram_mb=256,
            limit_cpu=0.5,
            limit_disk_gb=2,
        )
        repo.soft_delete_container(cid)
        # Deliberately never added to the fake daemon.

        _run_one_collector_pass(monkeypatch)

        assert (
            store.get_latest_point(cid) is None
        ), "the collector wrote metrics for a soft-deleted container"

    def test_one_broken_container_does_not_stop_the_others(
        self, pipeline_env, monkeypatch
    ):
        """A single container failing must not abort the whole pass.

        pipe-one is removed from the daemon while pipe-two stays healthy. The
        loop must log the first and still collect the second — otherwise one
        stale record silently stops all monitoring.
        """
        env = pipeline_env
        env["lxd"].containers._store.pop("pipe-one")

        before = store.get_latest_point(env["ids"]["pipe-two"])["time"]
        _run_one_collector_pass(monkeypatch)
        after = store.get_latest_point(env["ids"]["pipe-two"])["time"]

        assert after != before, (
            "pipe-two got no new point — one failing container stopped the "
            "entire collection pass"
        )

        # Restore for later tests in the module.
        env["lxd"].add("pipe-one")


class TestRetentionThroughTheApi:
    """Rolled-up data must remain readable by the chart endpoints."""

    @pytest.fixture(scope="class")
    @staticmethod
    def aged_container(pipeline_env):
        """A container with 3 hours of raw points, aged 25 hours into the past.

        Points are written at 30-second spacing so each 5-minute bucket holds
        exactly 10 of them — a bucket count the assertions can check exactly
        rather than approximately.

        This gets its own container rather than reusing pipe-one/pipe-two.
        run_retention_pass iterates list_active_containers(), so a test
        elsewhere in this module that soft-deletes a shared container makes
        retention skip it entirely — and the bucket counts here would then
        depend on test execution order.
        """
        base = NOW - timedelta(hours=25, minutes=180)

        pipeline_env["lxd"].add("pipe-aged")
        cid = repo.create_container_record(
            lxd_name="pipe-aged",
            image="ubuntu:22.04",
            created_by=pipeline_env["admin_id"],
            limit_ram_mb=1024,
            limit_cpu=1.0,
            limit_disk_gb=10,
        )
        repo.assign_container(pipeline_env["user_id"], cid)

        # A real collector point too: the live 1h chart must still read raw
        # data after retention has rolled the aged points up.
        _run_one_collector_pass()

        for i in range(360):  # 360 * 30s = 3 hours
            store.write_point(
                container_id=cid,
                fields={
                    "cpu_pct": 50.0,
                    "ram_used_mb": 512.0,
                    "disk_used_gb": 5.0,
                    "pid_count": 10.0,
                },
                resolution="raw",
                timestamp=base + timedelta(seconds=30 * i),
            )
        return {"id": cid, "expected_buckets": 36}  # 3 hours / 5 minutes

    def test_raw_points_older_than_24h_become_5m_averages(
        self, pipeline_env, aged_container
    ):
        """The rollup collapses 360 raw points into 36 five-minute points.

        Read back with a tag-only search rather than query_range. The rollup
        writes back-dated points and then calls TinyFlux's remove(), which
        leaves the store's time index stale-but-flagged-valid — so a
        *time-range* read under-reports here for reasons that have nothing to
        do with whether retention did its job. That defect is pinned
        separately by test_rolled_up_points_are_readable_by_time_range; this
        test is about the rollup arithmetic, so it reads a path the stale
        index cannot distort.
        """
        cid = aged_container["id"]

        run_retention_pass(now=NOW)

        old_start = NOW - timedelta(hours=30)
        old_end = NOW - timedelta(hours=24)

        raw_left = store.query_range(cid, old_start, old_end, resolution="raw")
        assert (
            raw_left == []
        ), f"{len(raw_left)} raw points survived past the 24h cutoff"

        rolled = _search_by_tag(cid, "5m")
        assert len(rolled) == aged_container["expected_buckets"], (
            f"expected {aged_container['expected_buckets']} 5m buckets, "
            f"got {len(rolled)}"
        )
        # The average of a constant series is that constant.
        assert rolled[0].fields["cpu_pct"] == pytest.approx(50.0)
        assert rolled[0].fields["ram_used_mb"] == pytest.approx(512.0)

    def test_rolled_up_points_are_readable_by_time_range(
        self, pipeline_env, aged_container
    ):
        """Every rolled-up point must be readable through the normal read path.

        query_range is what the history endpoint calls, so a point that
        exists but cannot be found by time range is invisible to the chart.

        This is the app-level half of B7. The TinyFlux defect itself is still
        real — see TestTinyFluxIndexBehaviour below, which characterises it
        and stays xfail — but retention.py now goes through
        tsdb.store.remove_points, which invalidates the stale index, so the
        read path is correct again.
        """
        cid = aged_container["id"]

        run_retention_pass(now=NOW)

        old_start = NOW - timedelta(hours=30)
        old_end = NOW - timedelta(hours=24)

        stored = len(_search_by_tag(cid, "5m"))
        readable = store.query_range(cid, old_start, old_end, resolution="5m")

        assert len(readable) == stored, (
            f"{stored} 5m points are in the store but a time-range query "
            f"over [{old_start}, {old_end}] found only {len(readable)}"
        )

    def test_recent_raw_points_are_left_alone(self, pipeline_env):
        """Points inside the 24h window must not be rolled up early.

        Rolling these up would destroy the resolution the live 1h chart reads.
        """
        cid = pipeline_env["ids"]["pipe-one"]
        store.write_point(
            container_id=cid,
            fields={"cpu_pct": 12.0, "ram_used_mb": 128.0},
            resolution="raw",
            timestamp=NOW - timedelta(minutes=10),
        )

        run_retention_pass(now=NOW)

        recent = store.query_range(cid, NOW - timedelta(hours=1), NOW, resolution="raw")
        assert any(p["cpu_pct"] == 12.0 for p in recent), (
            "a 10-minute-old raw point was rolled up; the 1h chart would be "
            "missing its most recent data"
        )

    def test_5m_points_older_than_7d_become_1h_averages(self, pipeline_env):
        """The second tier rolls 5m into 1h."""
        cid = pipeline_env["ids"]["pipe-one"]
        base = NOW - timedelta(days=8)

        for i in range(24):  # 2 hours at 5-minute spacing
            store.write_point(
                container_id=cid,
                fields={"cpu_pct": 30.0, "ram_used_mb": 256.0},
                resolution="5m",
                timestamp=base + timedelta(minutes=5 * i),
            )

        run_retention_pass(now=NOW)

        window_start = NOW - timedelta(days=9)
        window_end = NOW - timedelta(days=7)

        assert (
            store.query_range(cid, window_start, window_end, resolution="5m") == []
        ), "5m points survived past the 7-day cutoff"

        hourly = store.query_range(cid, window_start, window_end, resolution="1h")
        assert len(hourly) == 2, f"expected 2 hourly buckets, got {len(hourly)}"
        assert hourly[0]["cpu_pct"] == pytest.approx(30.0)

    def test_points_older_than_90_days_are_deleted_at_every_resolution(
        self, pipeline_env
    ):
        """The absolute cap applies regardless of resolution.

        Without this, 1h points would accumulate forever — they are never
        rolled up into anything coarser, so only the hard prune bounds the
        file size.
        """
        cid = pipeline_env["ids"]["pipe-one"]
        ancient = NOW - timedelta(days=100)

        for resolution in ["raw", "5m", "1h"]:
            store.write_point(
                container_id=cid,
                fields={"cpu_pct": 99.0},
                resolution=resolution,
                timestamp=ancient,
            )

        run_retention_pass(now=NOW)

        for resolution in ["raw", "5m", "1h"]:
            survivors = store.query_range(
                cid,
                ancient - timedelta(days=1),
                ancient + timedelta(days=1),
                resolution=resolution,
            )
            assert survivors == [], (
                f"{len(survivors)} {resolution} points survived the 90-day " f"prune"
            )

    def test_history_endpoint_serves_the_rolled_up_data(
        self, pipeline_env, aged_container
    ):
        """A 24h chart request returns the 5m points retention created.

        This is the seam the unit tests cannot reach: retention wrote the
        points, and the API must find them under the resolution the window
        maps to.
        """
        result = pipeline_env["client"].simulate_get(
            f"/api/containers/{aged_container['id']}/history",
            params={"window": "24h"},
            headers=pipeline_env["user"],
        )
        assert result.status_code == 200
        assert result.json["resolution"] == "5m"

    def test_retention_is_idempotent(self, pipeline_env, aged_container):
        """Running the pass twice must not double-count or duplicate points.

        The collector calls this hourly, and a retry after a crash could run
        it twice in quick succession. The second pass should find nothing left
        to do rather than re-averaging its own output.
        """
        cid = aged_container["id"]
        window_start = NOW - timedelta(hours=30)
        window_end = NOW - timedelta(hours=24)

        before = store.query_range(cid, window_start, window_end, resolution="5m")
        run_retention_pass(now=NOW)
        after = store.query_range(cid, window_start, window_end, resolution="5m")

        assert len(after) == len(before), (
            f"a second retention pass changed the 5m point count from "
            f"{len(before)} to {len(after)}"
        )
        if before:
            assert after[0]["cpu_pct"] == pytest.approx(
                before[0]["cpu_pct"]
            ), "re-running retention re-averaged its own output"

    def test_counter_rates_survive_the_rollup_and_the_api(self, pipeline_env):
        """A counter rolled up and read back must still chart correctly.

        The unit suite proves _reduce_fields keeps the right value in isolation.
        This proves the store round-trip does not undo it: the points are
        written back-dated, rolled up by the real retention pass (which also
        deletes the originals and invalidates TinyFlux's time index), and read
        back through store.query_range — the same call the history endpoint
        makes. The rate is then computed the way ContainerResourceGraphs.tsx
        computes it, by subtracting consecutive points.

        Read through query_range rather than over HTTP because the endpoint
        anchors its window to the real clock (now-24h .. now) while retention
        runs against the injected NOW, which is already days in the past. No
        data can satisfy both, so an HTTP read here would return an empty
        series no matter what the rollup did. The endpoint's own behaviour —
        that a 24h request resolves to the 5m tier — is covered by
        test_history_endpoint_serves_the_rolled_up_data above.

        The ramp is deliberately uneven. A flat or perfectly linear counter
        reduces the same way under averaging as under last-value, so a test
        built on one cannot tell the two apart.
        """
        pipeline_env["lxd"].add("pipe-counter")
        cid = repo.create_container_record(
            lxd_name="pipe-counter",
            image="ubuntu:22.04",
            created_by=pipeline_env["admin_id"],
            limit_ram_mb=512,
            limit_cpu=1.0,
            limit_disk_gb=5,
        )
        repo.assign_container(pipeline_env["user_id"], cid)

        # 30 minutes of 30-second samples, aged past the 24h cutoff and
        # aligned to a 5-minute boundary so the buckets are whole.
        base = (NOW - timedelta(hours=26)).replace(minute=0, second=0, microsecond=0)
        # Bytes received per 30s sample: idle, a burst mid-bucket, then steady.
        # Cumulative totals are what LXD actually reports.
        per_sample = ([0.0] * 7 + [120_000.0] + [0.0] * 2) + [3_000.0] * 50
        total = 0.0
        for i, delta in enumerate(per_sample):
            total += delta
            store.write_point(
                container_id=cid,
                fields={"net_rx_bytes": total, "ram_used_mb": 256.0},
                resolution="raw",
                timestamp=base + timedelta(seconds=30 * i),
            )
        expected_final = total

        run_retention_pass(now=NOW)

        points = store.query_range(
            cid,
            base - timedelta(minutes=5),
            base + timedelta(hours=1),
            resolution="5m",
        )
        points.sort(key=lambda p: p["time"])
        assert (
            len(points) >= 4
        ), f"expected several 5m buckets over 30 minutes, got {len(points)}"

        # The last bucket must carry the true running total, not a midpoint.
        # An average would land below it by roughly half a bucket of traffic.
        assert points[-1]["net_rx_bytes"] == pytest.approx(expected_final), (
            f"the final rolled-up total is {points[-1]['net_rx_bytes']}, "
            f"but the counter actually reached {expected_final}"
        )

        # Every consecutive pair must yield a non-negative rate. The frontend
        # discards negative deltas as counter resets, so a reduction that let
        # one appear would silently blank a segment of the chart.
        for previous, current in zip(points, points[1:]):
            elapsed = (
                datetime.fromisoformat(current["time"])
                - datetime.fromisoformat(previous["time"])
            ).total_seconds()
            assert elapsed > 0
            rate = (current["net_rx_bytes"] - previous["net_rx_bytes"]) / elapsed
            assert rate >= 0, (
                f"a rolled-up counter went backwards between "
                f"{previous['time']} and {current['time']}, which the chart "
                f"renders as a gap"
            )

        # Traffic must be conserved across the series: the first bucket's total
        # plus everything the deltas account for equals what the counter really
        # reached. Averaging loses the tail of the final bucket, so this catches
        # a reduction that merely looks plausible point by point.
        spanned = points[-1]["net_rx_bytes"] - points[0]["net_rx_bytes"]
        assert spanned == pytest.approx(expected_final - points[0]["net_rx_bytes"]), (
            f"the rolled-up series accounts for {spanned} bytes of traffic, "
            f"but the counter advanced to {expected_final}"
        )

        # The gauge alongside it is still averaged, so the two reductions
        # coexist in one point rather than one winning for the whole dict.
        assert points[-1]["ram_used_mb"] == pytest.approx(256.0)

    def test_retention_survives_a_container_with_no_data(self, pipeline_env):
        """A container that never produced metrics must not break the pass."""
        cid = repo.create_container_record(
            lxd_name="pipe-empty",
            image="ubuntu:22.04",
            created_by=pipeline_env["admin_id"],
            limit_ram_mb=256,
            limit_cpu=0.5,
            limit_disk_gb=2,
        )
        run_retention_pass(now=NOW)  # must not raise
        assert store.get_latest_point(cid) is None


class TestMetricsIsolation:
    """One container's metrics must never appear under another's id."""

    def test_points_are_scoped_by_container_id(self, pipeline_env):
        """Distinct values written for two containers stay separate.

        Read back with query_range rather than get_latest_point: the
        collector has already written points at the real wall clock, which
        is later than NOW, so "latest" would return those instead of the
        two points this test is actually asserting on.
        """
        one, two = pipeline_env["ids"]["pipe-one"], pipeline_env["ids"]["pipe-two"]

        store.write_point(one, {"cpu_pct": 11.0}, timestamp=NOW)
        store.write_point(two, {"cpu_pct": 22.0}, timestamp=NOW)

        window = (NOW - timedelta(minutes=1), NOW + timedelta(minutes=1))
        got_one = store.query_range(one, *window, resolution="raw")
        got_two = store.query_range(two, *window, resolution="raw")

        assert [p["cpu_pct"] for p in got_one] == [11.0]
        assert [p["cpu_pct"] for p in got_two] == [22.0]

    def test_history_of_a_deleted_container_is_still_readable_by_admin(
        self, pipeline_env
    ):
        """Soft-delete preserves metrics, so post-mortem charts still work.

        Uses a container of its own: soft-deleting drops it out of
        list_active_containers(), which is what run_retention_pass iterates,
        so deleting a shared one would silently disable retention for it.
        """
        pipeline_env["lxd"].add("pipe-doomed")
        cid = repo.create_container_record(
            lxd_name="pipe-doomed",
            image="ubuntu:22.04",
            created_by=pipeline_env["admin_id"],
            limit_ram_mb=256,
            limit_cpu=0.5,
            limit_disk_gb=2,
        )
        store.write_point(cid, {"cpu_pct": 7.0}, timestamp=NOW)
        repo.soft_delete_container(cid)

        result = pipeline_env["client"].simulate_get(
            f"/api/containers/{cid}/history",
            params={"window": "24h"},
            headers=pipeline_env["admin"],
        )
        assert result.status_code == 200, (
            "metric history vanished with the container; soft-delete is "
            "supposed to keep it queryable"
        )

    def test_latest_returns_a_null_point_rather_than_404(self, pipeline_env):
        """A container with no data yet is a normal state, not an error.

        A freshly created container has no points until the next collector
        tick. The dashboard should render an empty card, so the endpoint
        answers 200 with point=None.
        """
        cid = repo.create_container_record(
            lxd_name="pipe-fresh",
            image="ubuntu:22.04",
            created_by=pipeline_env["admin_id"],
            limit_ram_mb=256,
            limit_cpu=0.5,
            limit_disk_gb=2,
        )
        result = pipeline_env["client"].simulate_get(
            "/api/metrics/latest",
            params={"container": cid},
            headers=pipeline_env["admin"],
        )
        assert result.status_code == 200
        assert result.json["point"] is None


class TestTinyFluxIndexBehaviour:
    """Characterisation of the library defect behind B7.

    The retention test above proves *our* rollup is affected. This proves the
    cause is TinyFlux itself rather than anything in retention.py, using raw
    library calls and no application code — so whoever picks up B7 can tell
    a library bug from an application bug without re-deriving it, and knows
    immediately if a TinyFlux upgrade fixes it.
    """

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "FINDING (B7, root cause): TinyFlux 1.2.0 leaves its time index "
            "stale after remove() is called following an out-of-order "
            "insert, while still reporting index.valid = True. This test "
            "uses raw library calls only — when it XPASSes, the library has "
            "been fixed and the invalidate() workaround recommended for "
            "retention.py is no longer needed."
        ),
    )
    def test_remove_after_an_out_of_order_insert_corrupts_the_time_index(
        self, tmp_path
    ):
        """One back-dated point plus one remove() is enough to break range reads.

        This is the minimal reproduction. TinyFlux leaves index.valid True,
        so nothing downstream can detect the staleness — the query simply
        returns fewer points than the file holds.
        """
        from tinyflux import Point, TagQuery, TimeQuery, TinyFlux

        db = TinyFlux(str(tmp_path / "index-bug.csv"))
        tag, time_q = TagQuery(), TimeQuery()

        # One recent point, then one back-dated point: the exact shape
        # _downsample_and_prune creates when it writes a rollup bucket.
        db.insert(Point(time=NOW, tags={"c": "c1", "r": "raw"}, fields={"v": 1.0}))
        db.insert(
            Point(
                time=NOW - timedelta(hours=30),
                tags={"c": "c1", "r": "raw"},
                fields={"v": 2.0},
            )
        )
        db.remove(
            (tag.c == "c1") & (tag.r == "raw") & (time_q < NOW - timedelta(hours=24))
        )

        window = (time_q >= NOW - timedelta(hours=1)) & (time_q <= NOW)
        query = (tag.c == "c1") & (tag.r == "raw") & window

        via_index = len(db.search(query))
        assert db.index.valid, (
            "TinyFlux now flags the index invalid after this sequence — the "
            "workaround in retention.py may no longer be needed"
        )

        db.index.invalidate()  # forces a full scan
        via_scan = len(db.search(query))

        assert via_index == via_scan, (
            f"TinyFlux time index is stale after remove(): an index-backed "
            f"range query found {via_index} point(s) where a full scan finds "
            f"{via_scan}, while still reporting index.valid = True"
        )
