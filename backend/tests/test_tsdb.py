"""
Tests for the TinyFlux store wrapper (backend/tsdb/store.py).

Writes a few points across known timestamps, queries them back with
query_range, and asserts count and field values round-trip correctly.
Also tests get_latest_point returns the most recent point.

Uses a temporary file-backed TinyFlux instance isolated from the real
TINYFLUX_PATH by monkey-patching the module-level _store after resetting it.
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

# Patch TINYFLUX_PATH before importing store so config picks it up
_tmp_tsdb = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
_tmp_tsdb.close()
os.environ.setdefault("TINYFLUX_PATH", _tmp_tsdb.name)

from tinyflux import TinyFlux
import backend.tsdb.store as store_module
from backend.tsdb.store import write_point, query_range, get_latest_point


@pytest.fixture(autouse=True, scope="module")
def isolated_store():
    """Replace the module-level store with a fresh temp instance.

    This prevents tests from touching the real TINYFLUX_PATH and ensures
    all points written in this module are cleaned up afterward.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
    tmp.close()

    # Inject a fresh TinyFlux instance into the module
    fresh = TinyFlux(tmp.name)
    store_module._store = fresh

    yield

    fresh.close()
    if os.path.exists(tmp.name):
        os.remove(tmp.name)
    # Reset module state so other test modules get a fresh store
    store_module._store = None


class TestWriteAndQueryRange:
    """Round-trip tests for write_point and query_range."""

    CONTAINER_A = "container-uuid-aaa"
    CONTAINER_B = "container-uuid-bbb"

    # Fixed timestamps for deterministic assertions
    T0 = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    T1 = datetime(2024, 6, 1, 12, 5, 0, tzinfo=timezone.utc)
    T2 = datetime(2024, 6, 1, 12, 10, 0, tzinfo=timezone.utc)

    @pytest.fixture(autouse=True)
    def seed_points(self):
        """Write a known set of points before each test in this class."""
        store = store_module._store
        # Clear any points written by previous test methods in this class
        store.remove_all()

        # Three raw points for container A at T0, T1, T2
        for i, t in enumerate([self.T0, self.T1, self.T2]):
            from tinyflux import Point
            store.insert(Point(
                time=t,
                tags={"container_id": self.CONTAINER_A, "resolution": "raw"},
                fields={"cpu_pct": float(i * 10), "ram_mb": float(512 + i * 64)},
            ))

        # One 5m-averaged point for container A (different resolution)
        from tinyflux import Point
        store.insert(Point(
            time=self.T0,
            tags={"container_id": self.CONTAINER_A, "resolution": "5m"},
            fields={"cpu_pct": 5.0, "ram_mb": 520.0},
        ))

        # One raw point for container B (different container)
        store.insert(Point(
            time=self.T0,
            tags={"container_id": self.CONTAINER_B, "resolution": "raw"},
            fields={"cpu_pct": 99.0, "ram_mb": 4096.0},
        ))

    def test_query_range_returns_correct_count(self):
        """query_range returns only raw points for container A in range."""
        results = query_range(
            self.CONTAINER_A,
            start=self.T0,
            end=self.T2,
            resolution="raw",
        )
        assert len(results) == 3

    def test_query_range_fields_round_trip(self):
        """Field values written are returned unchanged."""
        results = query_range(
            self.CONTAINER_A,
            start=self.T0,
            end=self.T0,  # only T0
            resolution="raw",
        )
        assert len(results) == 1
        assert results[0]["cpu_pct"] == 0.0
        assert results[0]["ram_mb"] == 512.0

    def test_query_range_filters_by_resolution(self):
        """query_range with resolution='5m' returns only the averaged point."""
        results = query_range(
            self.CONTAINER_A,
            start=self.T0,
            end=self.T2,
            resolution="5m",
        )
        assert len(results) == 1
        assert results[0]["cpu_pct"] == 5.0

    def test_query_range_filters_by_container(self):
        """query_range does not leak points from other containers."""
        results = query_range(
            self.CONTAINER_B,
            start=self.T0,
            end=self.T2,
            resolution="raw",
        )
        assert len(results) == 1
        assert results[0]["cpu_pct"] == 99.0
        assert results[0]["container_id"] == self.CONTAINER_B

    def test_query_range_returns_plain_dicts(self):
        """Each result item is a plain dict, not a TinyFlux Point object."""
        results = query_range(
            self.CONTAINER_A,
            start=self.T0,
            end=self.T2,
            resolution="raw",
        )
        for item in results:
            assert isinstance(item, dict)
            assert "time" in item
            assert "container_id" in item
            assert "resolution" in item

    def test_query_range_empty_outside_window(self):
        """query_range returns [] when the time window contains no points."""
        future = self.T2 + timedelta(hours=1)
        results = query_range(
            self.CONTAINER_A,
            start=future,
            end=future + timedelta(hours=1),
            resolution="raw",
        )
        assert results == []


class TestGetLatestPoint:
    """Tests for get_latest_point."""

    CONTAINER_C = "container-uuid-ccc"

    T_EARLY = datetime(2024, 7, 1, 8, 0, 0, tzinfo=timezone.utc)
    T_LATE = datetime(2024, 7, 1, 9, 0, 0, tzinfo=timezone.utc)

    @pytest.fixture(autouse=True)
    def seed_points(self):
        """Write an early and a late point for container C."""
        from tinyflux import Point
        store = store_module._store
        # Clear any points from previous test methods
        store.remove_all()
        store.insert(Point(
            time=self.T_EARLY,
            tags={"container_id": self.CONTAINER_C, "resolution": "raw"},
            fields={"cpu_pct": 1.0, "ram_mb": 100.0},
        ))
        store.insert(Point(
            time=self.T_LATE,
            tags={"container_id": self.CONTAINER_C, "resolution": "raw"},
            fields={"cpu_pct": 2.0, "ram_mb": 200.0},
        ))

    def test_returns_most_recent_point(self):
        """get_latest_point returns the point with the highest timestamp."""
        result = get_latest_point(self.CONTAINER_C)
        assert result is not None
        assert result["cpu_pct"] == 2.0
        assert result["ram_mb"] == 200.0

    def test_returns_plain_dict(self):
        """get_latest_point returns a plain dict."""
        result = get_latest_point(self.CONTAINER_C)
        assert isinstance(result, dict)
        assert "time" in result

    def test_returns_none_for_unknown_container(self):
        """get_latest_point returns None if no points exist."""
        result = get_latest_point("no-such-container-xyz")
        assert result is None
