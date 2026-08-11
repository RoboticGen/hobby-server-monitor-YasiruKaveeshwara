"""
Tests for the usage accounting endpoint.

Covers GET /api/accounting: admin-only access, the host/allocated/per-user
split, and degradation path where LXD is unreachable but
the DB-derived figures still come back.

LXD is not available in the test environment, so get_host_resources is
monkeypatched: one test stubs a successful reading, another makes it raise
LXDUnavailableError to exercise the stale path. The normalization helpers
(_sum_cpu_threads, _sum_storage) are tested directly against the shapes LXD
reports, since those cannot be checked against a live daemon here.
"""

import uuid

import pytest
from falcon import testing

import backend.resources.accounting as accounting_module
from backend.app import create_app
from backend.auth.jwt_utils import create_access_token
from backend.db import repo
from backend.lxd import client as lxd_client
from backend.lxd.client import LXDUnavailableError

_FAKE_HOST = {
    "cpu_cores": 8.0,
    "ram_total_mb": 16384.0,
    "ram_used_mb": 4096.0,
    "disk_total_gb": 500.0,
    "disk_used_gb": 120.0,
}


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


class TestAccountingEndpoint:
    """Tests for GET /api/accounting."""

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        """Create app, an admin, a user, and one container assigned to them."""
        self.app = create_app()
        self.client = testing.TestClient(self.app)

        self.admin_id = repo.create_user(
            email=_unique_email("acct-admin"),
            role="admin",
            status="active",
            quota_ram_mb=8192,
            quota_cpu=8.0,
            quota_disk_gb=100,
        )
        self.admin_token = create_access_token(self.admin_id, "admin")

        self.user_id = repo.create_user(
            email=_unique_email("acct-user"),
            role="user",
            status="active",
            quota_ram_mb=4096,
            quota_cpu=4.0,
            quota_disk_gb=40,
        )
        self.user_token = create_access_token(self.user_id, "user")

        self.container_id = repo.create_container_record(
            lxd_name=f"acct-c-{uuid.uuid4().hex[:8]}",
            image="ubuntu:22.04",
            created_by=self.admin_id,
            limit_ram_mb=2048,
            limit_cpu=2.0,
            limit_disk_gb=20,
        )
        repo.assign_container(self.user_id, self.container_id)

        # Default to a reachable host so most tests exercise the happy path.
        monkeypatch.setattr(
            accounting_module.lxd_client,
            "get_host_resources",
            lambda: dict(_FAKE_HOST),
        )
        yield

    def _admin_headers(self) -> dict:
        return {"Cookie": f"access_token={self.admin_token}"}

    def _user_headers(self) -> dict:
        return {"Cookie": f"access_token={self.user_token}"}

    # --- Access control ---

    def test_returns_401_without_auth(self):
        """Unauthenticated request must return 401."""
        result = self.client.simulate_get("/api/accounting")
        assert result.status_code == 401

    def test_non_admin_gets_403(self):
        """A regular user must not see host-wide accounting."""
        result = self.client.simulate_get(
            "/api/accounting", headers=self._user_headers()
        )
        assert result.status_code == 403

    # --- Response shape ---

    def test_admin_gets_host_allocated_and_users(self):
        """Admin receives the three-way split plus the stale flag."""
        result = self.client.simulate_get(
            "/api/accounting", headers=self._admin_headers()
        )
        assert result.status_code == 200
        body = result.json
        assert set(body.keys()) == {
            "host",
            "allocated",
            "users",
            "stale",
            "host_error",
        }
        assert body["stale"] is False
        assert body["host_error"] is None
        assert body["host"] == _FAKE_HOST

    def test_allocated_totals_include_the_container(self):
        """Allocation sums the DB-cached limits of active containers."""
        result = self.client.simulate_get(
            "/api/accounting", headers=self._admin_headers()
        )
        allocated = result.json["allocated"]
        # Other tests share the DB, so assert "at least" our container's
        # limits rather than an exact total.
        assert allocated["ram_mb"] >= 2048
        assert allocated["cpu"] >= 2.0
        assert allocated["disk_gb"] >= 20
        assert allocated["container_count"] >= 1

    def test_per_user_row_shows_allocation_against_quota(self):
        """The assigned user's row reports their allocation and quota."""
        result = self.client.simulate_get(
            "/api/accounting", headers=self._admin_headers()
        )
        rows = {u["id"]: u for u in result.json["users"]}
        assert self.user_id in rows

        row = rows[self.user_id]
        assert row["allocation"] == {"ram_mb": 2048, "cpu": 2.0, "disk_gb": 20}
        assert row["quota"] == {"ram_mb": 4096, "cpu": 4.0, "disk_gb": 40}
        assert row["container_count"] == 1
        assert row["role"] == "user"

    def test_user_with_no_containers_shows_zero_allocation(self):
        """A user holding nothing reports zeros, not a missing row."""
        result = self.client.simulate_get(
            "/api/accounting", headers=self._admin_headers()
        )
        rows = {u["id"]: u for u in result.json["users"]}
        admin_row = rows[self.admin_id]
        assert admin_row["allocation"] == {
            "ram_mb": 0,
            "cpu": 0.0,
            "disk_gb": 0,
        }

    def test_soft_deleted_container_leaves_allocation(self):
        """Soft-deleting a container frees its allocation (7.8 keeps history)."""
        before = self.client.simulate_get(
            "/api/accounting", headers=self._admin_headers()
        ).json["allocated"]["ram_mb"]

        repo.soft_delete_container(self.container_id)

        after = self.client.simulate_get(
            "/api/accounting", headers=self._admin_headers()
        ).json["allocated"]["ram_mb"]
        assert after == before - 2048

    def test_soft_deleted_container_not_counted_per_user(self):
        """A soft-deleted container drops out of the user's container_count.

        Regression guard: soft_delete_container leaves the assignment row
        active=1, so counting assignment rows directly would report
        "1 container, 0 MB allocated" on the same row. Both numbers must
        describe the same set of live containers.
        """
        repo.soft_delete_container(self.container_id)

        result = self.client.simulate_get(
            "/api/accounting", headers=self._admin_headers()
        )
        row = {u["id"]: u for u in result.json["users"]}[self.user_id]
        assert row["container_count"] == 0
        assert row["allocation"]["ram_mb"] == 0

    # --- Decision 7.10 degradation ---

    def test_lxd_down_returns_200_with_stale_flag(self, monkeypatch):
        """LXD being unreachable must not fail the endpoint.

        The host block goes null and stale flips true, but the DB-derived
        allocation and per-user table still come back — that is the whole
        point of 7.10 for read endpoints.
        """
        def _boom():
            raise LXDUnavailableError("Cannot read host resources from LXD: down")

        monkeypatch.setattr(
            accounting_module.lxd_client, "get_host_resources", _boom
        )

        result = self.client.simulate_get(
            "/api/accounting", headers=self._admin_headers()
        )
        assert result.status_code == 200
        body = result.json
        assert body["host"] is None
        assert body["stale"] is True
        assert "down" in body["host_error"]
        # The DB half is unaffected.
        assert body["allocated"]["ram_mb"] >= 2048
        assert len(body["users"]) >= 2


class TestHostResourceNormalization:
    """Tests for the LXD resources parsing helpers.

    These cover the unit conversions and the two storage shapes LXD has
    used, which cannot be verified against a live daemon in this
    environment.
    """

    def test_cpu_threads_counted_from_sockets(self):
        """Threads are counted across sockets and cores, not cores alone."""
        cpu = {
            "sockets": [
                {"cores": [{"threads": [1, 2]}, {"threads": [3, 4]}]},
                {"cores": [{"threads": [5, 6]}]},
            ]
        }
        assert lxd_client._sum_cpu_threads(cpu) == 6.0

    def test_cpu_falls_back_to_total(self):
        """A flat 'total' is used when the nested shape is absent."""
        assert lxd_client._sum_cpu_threads({"total": 12}) == 12.0

    def test_cpu_missing_data_yields_zero(self):
        """An empty cpu block yields 0.0 rather than raising."""
        assert lxd_client._sum_cpu_threads({}) == 0.0

    def test_storage_pools_shape(self):
        """Pool space is summed and converted to GB."""
        gb = 1024 * 1024 * 1024
        storage = {
            "pools": [
                {"space_total": 100 * gb, "space_used": 40 * gb},
                {"space_total": 50 * gb, "space_used": 10 * gb},
            ]
        }
        total, used = lxd_client._sum_storage(storage)
        assert total == 150.0
        assert used == 50.0

    def test_storage_disks_shape_fallback(self):
        """The alternate 'disks' shape is handled when no pools are present."""
        gb = 1024 * 1024 * 1024
        total, used = lxd_client._sum_storage(
            {"disks": [{"size": 250 * gb}, {"size": 250 * gb}]}
        )
        assert total == 500.0
        assert used == 0.0

    def test_storage_empty_yields_zeros(self):
        """An unrecognized storage block yields zeros, not an exception."""
        assert lxd_client._sum_storage({}) == (0.0, 0.0)
