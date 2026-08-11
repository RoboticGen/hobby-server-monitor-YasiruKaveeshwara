"""
Tests for container assignment grant and revoke endpoints.

- Grant the test container to a user, confirm GET /api/containers
  shows exactly that one container for them.
- Confirm a user requesting a container they're NOT assigned to
  returns 403, not 404 (brief requirement: no container ID enumeration).

Uses Falcon TestClient with JWT cookies for full request/response cycle.
"""

import uuid

import pytest
from falcon import testing

from backend.app import create_app
from backend.auth.jwt_utils import create_access_token
from backend.db import repo


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


class TestAssignmentEndpoints:
    """Integration tests for assignment grant/revoke and 403-not-404 rule."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Create a fresh admin, a regular user, and a container for each test."""
        self.app = create_app()
        self.client = testing.TestClient(self.app)

        # Fresh admin
        self.admin_id = repo.create_user(
            email=_unique_email("assign-admin"),
            role="admin",
            status="active",
            quota_ram_mb=8192,
            quota_cpu=8.0,
            quota_disk_gb=100,
        )
        self.admin_token = create_access_token(self.admin_id, "admin")

        # Fresh regular user with enough quota for the test container
        self.user_id = repo.create_user(
            email=_unique_email("assign-user"),
            role="user",
            status="active",
            quota_ram_mb=4096,
            quota_cpu=4.0,
            quota_disk_gb=40,
        )
        self.user_token = create_access_token(self.user_id, "user")

        # Container with limits well within the user's quota
        self.container_id = repo.create_container_record(
            lxd_name=f"test-c-{uuid.uuid4().hex[:8]}",
            image="ubuntu:22.04",
            created_by=self.admin_id,
            limit_ram_mb=1024,
            limit_cpu=1.0,
            limit_disk_gb=10,
        )

        # A second container that will NOT be assigned to the user
        self.other_container_id = repo.create_container_record(
            lxd_name=f"other-c-{uuid.uuid4().hex[:8]}",
            image="ubuntu:22.04",
            created_by=self.admin_id,
            limit_ram_mb=512,
            limit_cpu=0.5,
            limit_disk_gb=5,
        )

    def _admin_headers(self) -> dict:
        return {"Cookie": f"access_token={self.admin_token}"}

    def _user_headers(self) -> dict:
        return {"Cookie": f"access_token={self.user_token}"}

    # ------------------------------------------------------------------ #
    # Grant                                                               #
    # ------------------------------------------------------------------ #

    def test_grant_returns_201(self):
        """POST /api/users/{uid}/containers/{cid} returns 201 on success."""
        result = self.client.simulate_post(
            f"/api/users/{self.user_id}/containers/{self.container_id}",
            headers=self._admin_headers(),
        )
        assert result.status_code == 201, result.text
        body = result.json
        assert body["user_id"] == self.user_id
        assert body["container_id"] == self.container_id

    def test_user_sees_only_assigned_container(self):
        """After grant, user's GET /api/containers shows exactly that container."""
        # Grant access
        self.client.simulate_post(
            f"/api/users/{self.user_id}/containers/{self.container_id}",
            headers=self._admin_headers(),
        )

        # User lists containers — should see exactly the assigned one
        result = self.client.simulate_get(
            "/api/containers",
            headers=self._user_headers(),
        )
        assert result.status_code == 200
        containers = result.json["containers"]
        ids = [c["id"] for c in containers]
        assert self.container_id in ids
        # The unassigned container must NOT appear
        assert self.other_container_id not in ids

    def test_user_can_get_assigned_container_by_id(self):
        """User can GET /api/containers/{id} for an assigned container."""
        self.client.simulate_post(
            f"/api/users/{self.user_id}/containers/{self.container_id}",
            headers=self._admin_headers(),
        )

        result = self.client.simulate_get(
            f"/api/containers/{self.container_id}",
            headers=self._user_headers(),
        )
        assert result.status_code == 200
        assert result.json["id"] == self.container_id

    def test_unassigned_container_returns_403_not_404(self):
        """Requesting an unassigned container by ID must return 403, not 404.

        This is an explicit security requirement from the project brief:
        returning 404 would allow container ID enumeration (attacker
        could tell the difference between 'exists but no access' and
        'doesn't exist'). 403 reveals nothing about existence.
        """
        # Do NOT grant access — request should return 403
        result = self.client.simulate_get(
            f"/api/containers/{self.other_container_id}",
            headers=self._user_headers(),
        )
        assert result.status_code == 403, (
            f"Expected 403 for unassigned container, got {result.status_code}. "
            "The brief requires 403 (not 404) to prevent container ID enumeration."
        )

    def test_duplicate_grant_returns_409(self):
        """Granting the same container twice returns 409 Conflict."""
        url = f"/api/users/{self.user_id}/containers/{self.container_id}"
        self.client.simulate_post(url, headers=self._admin_headers())
        result = self.client.simulate_post(url, headers=self._admin_headers())
        assert result.status_code == 409

    def test_non_admin_cannot_grant(self):
        """A regular user cannot grant container access."""
        result = self.client.simulate_post(
            f"/api/users/{self.user_id}/containers/{self.container_id}",
            headers=self._user_headers(),
        )
        assert result.status_code == 403

    # ------------------------------------------------------------------ #
    # Revoke                                                              #
    # ------------------------------------------------------------------ #

    def test_revoke_removes_access(self):
        """After revoke, user no longer sees the container in their list."""
        url = f"/api/users/{self.user_id}/containers/{self.container_id}"

        # Grant then revoke
        self.client.simulate_post(url, headers=self._admin_headers())
        result = self.client.simulate_delete(url, headers=self._admin_headers())
        assert result.status_code == 200

        # User's container list should now be empty for this container
        list_result = self.client.simulate_get(
            "/api/containers", headers=self._user_headers()
        )
        ids = [c["id"] for c in list_result.json["containers"]]
        assert self.container_id not in ids

    def test_revoke_nonexistent_assignment_returns_404(self):
        """Revoking an access that was never granted returns 404."""
        result = self.client.simulate_delete(
            f"/api/users/{self.user_id}/containers/{self.container_id}",
            headers=self._admin_headers(),
        )
        assert result.status_code == 404
