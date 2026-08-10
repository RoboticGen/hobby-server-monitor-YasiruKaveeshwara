"""
Tests for the metrics API endpoints.

Tests both the dashboard polling endpoint (GET /api/metrics/latest) and
the per-container history endpoint (GET /api/containers/{id}/history).

Writes synthetic TinyFlux points directly, then hits the Falcon test
client with JWT cookies to verify correct filtering, aggregation routing,
and authorization enforcement.
"""

import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from falcon import testing
from tinyflux import Point, TinyFlux

from backend.app import create_app
from backend.auth.jwt_utils import create_access_token
from backend.db import repo
import backend.tsdb.store as store_module


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture(autouse=True, scope="module")
def isolated_store():
    """Replace the module-level TinyFlux instance with a fresh temp file.

    Module-scoped so all classes in this file share a single isolated store.
    Reset to None at the end so other test modules aren't affected.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
    tmp.close()

    fresh = TinyFlux(tmp.name)
    store_module._store = fresh

    yield

    fresh.close()
    store_module._store = None
    os.remove(tmp.name)


class TestLatestMetricsEndpoint:
    """Tests for GET /api/metrics/latest?container=<id>."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Create app, user, admin, container, and seed TinyFlux points."""
        self.app = create_app()
        self.client = testing.TestClient(self.app)

        self.admin_id = repo.create_user(
            email=_unique_email("metrics-admin"),
            role="admin",
            status="active",
            quota_ram_mb=8192,
            quota_cpu=8.0,
            quota_disk_gb=100,
        )
        self.admin_token = create_access_token(self.admin_id, "admin")

        self.user_id = repo.create_user(
            email=_unique_email("metrics-user"),
            role="user",
            status="active",
            quota_ram_mb=4096,
            quota_cpu=4.0,
            quota_disk_gb=40,
        )
        self.user_token = create_access_token(self.user_id, "user")

        self.container_id = repo.create_container_record(
            lxd_name=f"metrics-c-{uuid.uuid4().hex[:8]}",
            image="ubuntu:22.04",
            created_by=self.admin_id,
            limit_ram_mb=1024,
            limit_cpu=1.0,
            limit_disk_gb=10,
        )

        # Grant the user access to this container
        repo.assign_container(self.user_id, self.container_id)

        # Seed TinyFlux with three raw points for the container, using
        # timestamps relative to now so the latest point is clearly the
        # most recent one.
        store = store_module._store
        store.remove_all()
        now = datetime.now(tz=timezone.utc)
        for minutes, cpu in zip([10, 5, 1], [10.0, 20.0, 30.0]):
            store.insert(Point(
                time=now - timedelta(minutes=minutes),
                tags={"container_id": self.container_id, "resolution": "raw"},
                fields={"cpu_pct": cpu, "ram_mb": 512.0},
            ))

        yield

    def _admin_headers(self) -> dict:
        return {"Cookie": f"access_token={self.admin_token}"}

    def _user_headers(self) -> dict:
        return {"Cookie": f"access_token={self.user_token}"}

    # --- Auth enforcement ---

    def test_returns_401_without_auth(self):
        """Unauthenticated request must return 401."""
        result = self.client.simulate_get("/api/metrics/latest")
        assert result.status_code == 401

    # --- Missing / invalid container param ---

    def test_returns_400_without_container_param(self):
        """Request without a 'container' query param must return 400."""
        result = self.client.simulate_get(
            "/api/metrics/latest", headers=self._admin_headers()
        )
        assert result.status_code == 400

    def test_returns_400_with_empty_container_param(self):
        """Empty 'container' query param must return 400."""
        result = self.client.simulate_get(
            "/api/metrics/latest?container=",
            headers=self._admin_headers(),
        )
        assert result.status_code == 400

    # --- Successful queries ---

    def test_admin_gets_latest_point(self):
        """Admin requesting the container's latest point gets the most recent sample."""
        result = self.client.simulate_get(
            f"/api/metrics/latest?container={self.container_id}",
            headers=self._admin_headers(),
        )
        assert result.status_code == 200
        point = result.json["point"]
        assert point is not None
        # The last inserted point (T2) should be returned
        assert point["cpu_pct"] == 30.0

    def test_user_gets_latest_point_for_assigned_container(self):
        """User with access gets the latest point for their assigned container."""
        result = self.client.simulate_get(
            f"/api/metrics/latest?container={self.container_id}",
            headers=self._user_headers(),
        )
        assert result.status_code == 200
        assert result.json["point"]["cpu_pct"] == 30.0

    def test_user_gets_403_for_unassigned_container(self):
        """User without access gets 403, not 404 (no container ID enumeration)."""
        other_id = repo.create_container_record(
            lxd_name=f"other-c-{uuid.uuid4().hex[:8]}",
            image="ubuntu:22.04",
            created_by=self.admin_id,
            limit_ram_mb=512,
            limit_cpu=0.5,
            limit_disk_gb=5,
        )
        result = self.client.simulate_get(
            f"/api/metrics/latest?container={other_id}",
            headers=self._user_headers(),
        )
        assert result.status_code == 403

    def test_no_data_returns_null_point(self):
        """Container with no TinyFlux data returns 200 with point: null."""
        empty_container_id = repo.create_container_record(
            lxd_name=f"empty-c-{uuid.uuid4().hex[:8]}",
            image="ubuntu:22.04",
            created_by=self.admin_id,
            limit_ram_mb=256,
            limit_cpu=0.25,
            limit_disk_gb=2,
        )
        result = self.client.simulate_get(
            f"/api/metrics/latest?container={empty_container_id}",
            headers=self._admin_headers(),
        )
        assert result.status_code == 200
        assert result.json["point"] is None
        assert result.json["container_id"] == empty_container_id

    def test_response_shape(self):
        """Response must contain exactly 'container_id' and 'point' keys."""
        result = self.client.simulate_get(
            f"/api/metrics/latest?container={self.container_id}",
            headers=self._admin_headers(),
        )
        assert result.status_code == 200
        body = result.json
        assert set(body.keys()) == {"container_id", "point"}
        assert body["container_id"] == self.container_id
        assert "cpu_pct" in body["point"]
        assert "ram_mb" in body["point"]
        assert "time" in body["point"]
        assert "resolution" in body["point"]


class TestContainerHistoryEndpoint:
    """Tests for GET /api/containers/{id}/history?window=1h|24h|7d."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Create app, user, admin, container, and seed TinyFlux points."""
        self.app = create_app()
        self.client = testing.TestClient(self.app)

        self.admin_id = repo.create_user(
            email=_unique_email("hist-admin"),
            role="admin",
            status="active",
            quota_ram_mb=8192,
            quota_cpu=8.0,
            quota_disk_gb=100,
        )
        self.admin_token = create_access_token(self.admin_id, "admin")

        self.user_id = repo.create_user(
            email=_unique_email("hist-user"),
            role="user",
            status="active",
            quota_ram_mb=4096,
            quota_cpu=4.0,
            quota_disk_gb=40,
        )
        self.user_token = create_access_token(self.user_id, "user")

        self.container_id = repo.create_container_record(
            lxd_name=f"hist-c-{uuid.uuid4().hex[:8]}",
            image="ubuntu:22.04",
            created_by=self.admin_id,
            limit_ram_mb=1024,
            limit_cpu=1.0,
            limit_disk_gb=10,
        )
        repo.assign_container(self.user_id, self.container_id)

        # Seed points for the container:
        #   - 3 raw points (for the "1h" window)
        #   - 1 five-minute averaged point (for the "24h" window)
        #   - 1 hourly averaged point (for the "7d" window)
        # Timestamps are relative to "now" because the history endpoint
        # queries [now - span, now]; fixed past timestamps would fall
        # outside every window. Each point sits comfortably inside its
        # target window but outside the narrower ones.
        store = store_module._store
        store.remove_all()

        now = datetime.now(tz=timezone.utc)

        # Raw points within the last hour (visible only to the 1h window)
        for minutes, cpu in zip([1, 2, 3], [5.0, 15.0, 25.0]):
            store.insert(Point(
                time=now - timedelta(minutes=minutes),
                tags={"container_id": self.container_id, "resolution": "raw"},
                fields={"cpu_pct": cpu, "ram_mb": 500.0},
            ))

        # 5m point ~5 hours old — inside the 24h window's 5m resolution
        store.insert(Point(
            time=now - timedelta(hours=5),
            tags={"container_id": self.container_id, "resolution": "5m"},
            fields={"cpu_pct": 15.0, "ram_mb": 510.0},
        ))

        # 1h point ~3 days old — inside the 7d window's 1h resolution
        store.insert(Point(
            time=now - timedelta(days=3),
            tags={"container_id": self.container_id, "resolution": "1h"},
            fields={"cpu_pct": 15.0, "ram_mb": 520.0},
        ))

        yield

    def _admin_headers(self) -> dict:
        return {"Cookie": f"access_token={self.admin_token}"}

    def _user_headers(self) -> dict:
        return {"Cookie": f"access_token={self.user_token}"}

    # --- Auth enforcement ---

    def test_returns_401_without_auth(self):
        """Unauthenticated request must return 401."""
        result = self.client.simulate_get(
            f"/api/containers/{self.container_id}/history"
        )
        assert result.status_code == 401

    def test_user_cannot_access_unassigned_container_history(self):
        """User without access gets 403 on the history endpoint."""
        other_id = repo.create_container_record(
            lxd_name=f"hist-other-{uuid.uuid4().hex[:8]}",
            image="ubuntu:22.04",
            created_by=self.admin_id,
            limit_ram_mb=256,
            limit_cpu=0.25,
            limit_disk_gb=2,
        )
        result = self.client.simulate_get(
            f"/api/containers/{other_id}/history",
            headers=self._user_headers(),
        )
        assert result.status_code == 403

    # --- Invalid window ---

    def test_invalid_window_returns_400(self):
        """Unknown window value must return 400."""
        result = self.client.simulate_get(
            f"/api/containers/{self.container_id}/history?window=42d",
            headers=self._admin_headers(),
        )
        assert result.status_code == 400

    # --- Window routing to correct resolution ---

    def test_1h_window_returns_raw_points(self):
        """1h window queries the 'raw' resolution."""
        result = self.client.simulate_get(
            f"/api/containers/{self.container_id}/history?window=1h",
            headers=self._admin_headers(),
        )
        assert result.status_code == 200
        body = result.json
        assert body["window"] == "1h"
        assert body["resolution"] == "raw"
        assert len(body["points"]) == 3

    def test_24h_window_returns_5m_points(self):
        """24h window queries the '5m' averaged resolution."""
        result = self.client.simulate_get(
            f"/api/containers/{self.container_id}/history?window=24h",
            headers=self._admin_headers(),
        )
        assert result.status_code == 200
        assert result.json["resolution"] == "5m"
        assert len(result.json["points"]) == 1
        assert result.json["points"][0]["cpu_pct"] == 15.0

    def test_7d_window_returns_1h_points(self):
        """7d window queries the '1h' averaged resolution."""
        result = self.client.simulate_get(
            f"/api/containers/{self.container_id}/history?window=7d",
            headers=self._admin_headers(),
        )
        assert result.status_code == 200
        assert result.json["resolution"] == "1h"
        assert len(result.json["points"]) == 1
        assert result.json["points"][0]["cpu_pct"] == 15.0

    def test_default_window_is_1h(self):
        """Omitting the window param defaults to 1h."""
        result = self.client.simulate_get(
            f"/api/containers/{self.container_id}/history",
            headers=self._admin_headers(),
        )
        assert result.status_code == 200
        assert result.json["window"] == "1h"
        assert result.json["resolution"] == "raw"

    # --- Response shape ---

    def test_response_shape(self):
        """Response must contain exactly container_id, window, resolution, points."""
        result = self.client.simulate_get(
            f"/api/containers/{self.container_id}/history?window=1h",
            headers=self._admin_headers(),
        )
        assert result.status_code == 200
        body = result.json
        assert set(body.keys()) == {"container_id", "window", "resolution", "points"}
        assert body["container_id"] == self.container_id
        for pt in body["points"]:
            assert "time" in pt
            assert "container_id" in pt
            assert "resolution" in pt
