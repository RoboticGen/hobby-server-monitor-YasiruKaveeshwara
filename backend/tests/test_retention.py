"""
Tests for the retention and downsampling job (Step 10.2).

Calls the internal retention functions (_downsample_and_prune,
_prune_old_points) directly with explicit container IDs and synthetic
timestamps so tests are fully isolated from the shared DB session and
from the repo.list_active_containers() path.

Implement three steps per container:
- Raw points older than 24h -> rolled into 5m averages, originals deleted.
- 5m points older than 7 days -> rolled into 1h averages, originals deleted.
- Any points older than 90 days -> deleted outright.
"""

import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from tinyflux import TinyFlux

import backend.tsdb.store as store_module
from backend.tsdb.store import write_point, query_range
from backend.collector.retention import (
    _downsample_and_prune,
    _prune_old_points,
    _RAW_MAX_AGE,
    _FIVE_MIN_MAX_AGE,
    _FIVE_MIN_BUCKET,
    _ONE_HOUR_BUCKET,
)


def _cid() -> str:
    """Return a unique container UUID for each call."""
    return f"ret-{uuid.uuid4().hex[:12]}"


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
    """Raw points > 24h old are rolled up to 5m averages."""

    def test_old_raw_points_are_removed(self):
        """After downsample, raw points older than 24h are gone."""
        cid = _cid()
        write_point(cid, {"cpu_usage_ns": 100.0}, resolution="raw", timestamp=_30H_AGO)
        write_point(cid, {"cpu_usage_ns": 200.0}, resolution="raw",
                    timestamp=_30H_AGO + timedelta(minutes=2))

        _downsample_and_prune(cid, "raw", "5m", _RAW_MAX_AGE, _FIVE_MIN_BUCKET, _NOW)

        raw_left = query_range(cid,
                               start=_30H_AGO - timedelta(hours=1),
                               end=_30H_AGO + timedelta(hours=1),
                               resolution="raw")
        assert raw_left == []

    def test_five_min_averaged_point_is_created(self):
        """After downsample, a 5m-averaged point with correct average exists."""
        cid = _cid()
        write_point(cid, {"cpu_usage_ns": 100.0}, resolution="raw", timestamp=_30H_AGO)
        write_point(cid, {"cpu_usage_ns": 200.0}, resolution="raw",
                    timestamp=_30H_AGO + timedelta(minutes=2))

        _downsample_and_prune(cid, "raw", "5m", _RAW_MAX_AGE, _FIVE_MIN_BUCKET, _NOW)

        five_min = query_range(cid,
                               start=_30H_AGO - timedelta(hours=1),
                               end=_30H_AGO + timedelta(hours=1),
                               resolution="5m")
        assert len(five_min) == 1
        assert five_min[0]["cpu_usage_ns"] == pytest.approx(150.0)

    def test_recent_raw_points_are_kept(self):
        """Raw points less than 24h old are NOT touched by the raw to 5m step."""
        cid = _cid()
        write_point(cid, {"cpu_usage_ns": 50.0}, resolution="raw", timestamp=_30H_AGO)
        write_point(cid, {"cpu_usage_ns": 75.0}, resolution="raw", timestamp=_1H_AGO)

        _downsample_and_prune(cid, "raw", "5m", _RAW_MAX_AGE, _FIVE_MIN_BUCKET, _NOW)

        # Query all raw points within the last 48h — should contain only the
        # 1h-ago point (the 30h-ago one was rolled up). Use a wide end bound to
        # avoid any off-by-one issues with TinyFlux's <= operator.
        recent = query_range(cid,
                             start=_NOW - timedelta(hours=48),
                             end=_NOW + timedelta(hours=1),
                             resolution="raw")
        assert len(recent) == 1
        assert recent[0]["cpu_usage_ns"] == pytest.approx(75.0)


class TestFiveMinToOneHourDownsample:
    """5m points > 7 days old are rolled up to 1h averages."""

    def test_old_5m_points_are_removed(self):
        """After 5m to 1h downsample, old 5m points are gone."""
        cid = _cid()
        write_point(cid, {"cpu_usage_ns": 300.0}, resolution="5m", timestamp=_10D_AGO)
        write_point(cid, {"cpu_usage_ns": 400.0}, resolution="5m",
                    timestamp=_10D_AGO + timedelta(minutes=30))

        _downsample_and_prune(cid, "5m", "1h", _FIVE_MIN_MAX_AGE, _ONE_HOUR_BUCKET, _NOW)

        five_min_left = query_range(cid,
                                    start=_10D_AGO - timedelta(hours=1),
                                    end=_10D_AGO + timedelta(hours=2),
                                    resolution="5m")
        assert five_min_left == []

    def test_one_hour_averaged_point_is_created(self):
        """After 5m to 1h downsample, a 1h-averaged point exists with correct value."""
        cid = _cid()
        write_point(cid, {"cpu_usage_ns": 300.0}, resolution="5m", timestamp=_10D_AGO)
        write_point(cid, {"cpu_usage_ns": 400.0}, resolution="5m",
                    timestamp=_10D_AGO + timedelta(minutes=30))

        _downsample_and_prune(cid, "5m", "1h", _FIVE_MIN_MAX_AGE, _ONE_HOUR_BUCKET, _NOW)

        one_hour = query_range(cid,
                               start=_10D_AGO - timedelta(hours=1),
                               end=_10D_AGO + timedelta(hours=2),
                               resolution="1h")
        assert len(one_hour) == 1
        assert one_hour[0]["cpu_usage_ns"] == pytest.approx(350.0)


class TestAbsolutePrune:
    """Points > 90 days old are deleted outright regardless of resolution."""

    def test_100_day_old_raw_points_are_pruned(self):
        """Raw points older than 90 days are deleted by _prune_old_points."""
        cid = _cid()
        write_point(cid, {"cpu_usage_ns": 1.0}, resolution="raw", timestamp=_100D_AGO)

        _prune_old_points(cid, now=_NOW)

        raw = query_range(cid,
                          start=_100D_AGO - timedelta(days=5),
                          end=_100D_AGO + timedelta(days=5),
                          resolution="raw")
        assert raw == []

    def test_100_day_old_5m_points_are_pruned(self):
        """5m-averaged points older than 90 days are also pruned."""
        cid = _cid()
        write_point(cid, {"cpu_usage_ns": 2.0}, resolution="5m", timestamp=_100D_AGO)

        _prune_old_points(cid, now=_NOW)

        pts = query_range(cid,
                          start=_100D_AGO - timedelta(days=5),
                          end=_100D_AGO + timedelta(days=5),
                          resolution="5m")
        assert pts == []

    def test_100_day_old_1h_points_are_pruned(self):
        """1h-averaged points older than 90 days are also pruned."""
        cid = _cid()
        write_point(cid, {"cpu_usage_ns": 3.0}, resolution="1h", timestamp=_100D_AGO)

        _prune_old_points(cid, now=_NOW)

        pts = query_range(cid,
                          start=_100D_AGO - timedelta(days=5),
                          end=_100D_AGO + timedelta(days=5),
                          resolution="1h")
        assert pts == []
