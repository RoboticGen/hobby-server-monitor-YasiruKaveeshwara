"""
Tests for user management endpoints (invite, list, update).

Uses Falcon TestClient with a valid admin JWT cookie to test the
full request/response cycle including auth middleware.

All test-created users use UUID-suffixed emails so tests can run in
any order without UNIQUE constraint collisions in the shared session DB.
"""

import uuid

import pytest
from falcon import testing

from backend.app import create_app
from backend.auth.jwt_utils import create_access_token
from backend.db import repo


def _unique_email(prefix: str) -> str:
    """Generate a unique email address for a test user.

    Uses uuid4 so each test invocation creates a fresh email that will
    never collide with other tests in the shared session database.
    """
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


class TestUserEndpoints:
    """Integration tests for /api/users endpoints using TestClient."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Create a test client and a fresh admin user for each test."""
        self.app = create_app()
        self.client = testing.TestClient(self.app)

        # Each test gets its own admin user with a unique email so
        # tests are fully independent and never collide.
        self.admin_email = _unique_email("admin")
        self.admin_id = repo.create_user(
            email=self.admin_email,
            role="admin",
            status="active",
            quota_ram_mb=0,
            quota_cpu=0.0,
            quota_disk_gb=0,
        )

        # Create a valid JWT access token for the admin
        self.admin_token = create_access_token(self.admin_id, "admin")

    def _admin_headers(self) -> dict:
        """Return headers with the admin JWT set as a cookie."""
        return {"Cookie": f"access_token={self.admin_token}"}

    def test_invite_user_returns_201(self):
        """POST /api/users with valid data should return 201 with invited status."""
        email = _unique_email("invited")
        result = self.client.simulate_post(
            "/api/users",
            headers=self._admin_headers(),
            json={
                "email": email,
                "role": "user",
                "quota_ram_mb": 2048,
                "quota_cpu": 2.0,
                "quota_disk_gb": 20,
            },
        )
        assert result.status_code == 201, result.text
        body = result.json
        assert body["email"] == email
        assert body["status"] == "invited"
        assert body["role"] == "user"

    def test_invited_user_appears_in_list(self):
        """GET /api/users should include the newly invited user."""
        email = _unique_email("list-user")

        # Invite a user
        self.client.simulate_post(
            "/api/users",
            headers=self._admin_headers(),
            json={
                "email": email,
                "role": "user",
                "quota_ram_mb": 1024,
                "quota_cpu": 1.0,
                "quota_disk_gb": 10,
            },
        )

        # List all users — invited user must appear
        result = self.client.simulate_get(
            "/api/users",
            headers=self._admin_headers(),
        )
        assert result.status_code == 200
        users = result.json["users"]
        emails = [u["email"] for u in users]
        assert email in emails

        # Every user record must carry an 'allocation' field
        for user in users:
            assert "allocation" in user

    def test_update_user_role(self):
        """PATCH /api/users/{id} should update the user's role."""
        email = _unique_email("promote")

        invite_result = self.client.simulate_post(
            "/api/users",
            headers=self._admin_headers(),
            json={
                "email": email,
                "role": "user",
                "quota_ram_mb": 1024,
                "quota_cpu": 1.0,
                "quota_disk_gb": 10,
            },
        )
        assert invite_result.status_code == 201, invite_result.text
        user_id = invite_result.json["id"]

        # Promote to admin
        result = self.client.simulate_patch(
            f"/api/users/{user_id}",
            headers=self._admin_headers(),
            json={"role": "admin"},
        )
        assert result.status_code == 200
        assert result.json["role"] == "admin"

    def test_cannot_demote_last_admin(self):
        """PATCH should reject demoting an admin when they are the only active one.

        We revoke all other active admins created by other tests first, so
        self.admin_id is provably the last active admin in the shared DB.
        Then attempt to demote it — must get 400 with 'last admin' message.
        """
        # Find and revoke all OTHER active admins in the shared DB so
        # self.admin_id becomes the provably last active admin.
        all_users = repo.list_users()
        for u in all_users:
            if u["id"] != self.admin_id and u["role"] == "admin" and u["status"] == "active":
                repo.update_user(u["id"], status="revoked")

        # Now self.admin_id is the only active admin — demotion must be blocked
        result = self.client.simulate_patch(
            f"/api/users/{self.admin_id}",
            headers=self._admin_headers(),
            json={"role": "user"},
        )
        assert result.status_code == 400
        assert "last admin" in result.json["title"].lower()

    def test_invite_duplicate_email_returns_409(self):
        """POST /api/users with an existing email should return 409."""
        email = _unique_email("dup")

        # First invite succeeds
        self.client.simulate_post(
            "/api/users",
            headers=self._admin_headers(),
            json={"email": email, "role": "user",
                  "quota_ram_mb": 1024, "quota_cpu": 1.0, "quota_disk_gb": 10},
        )

        # Second invite with same email must conflict
        result = self.client.simulate_post(
            "/api/users",
            headers=self._admin_headers(),
            json={"email": email, "role": "user",
                  "quota_ram_mb": 1024, "quota_cpu": 1.0, "quota_disk_gb": 10},
        )
        assert result.status_code == 409

    def test_non_admin_gets_403(self):
        """A regular user must get 403 trying to list users."""
        regular_id = repo.create_user(
            email=_unique_email("regular"),
            role="user",
            status="active",
            quota_ram_mb=1024,
            quota_cpu=1.0,
            quota_disk_gb=10,
        )
        user_token = create_access_token(regular_id, "user")
        headers = {"Cookie": f"access_token={user_token}"}

        result = self.client.simulate_get("/api/users", headers=headers)
        assert result.status_code == 403
