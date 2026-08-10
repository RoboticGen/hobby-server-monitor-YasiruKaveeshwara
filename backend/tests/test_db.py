"""
Tests for the database repo layer (users and sessions).

Uses a temporary SQLite file for each test run so tests are isolated
and don't touch the real application database.
"""

import os
import tempfile

import pytest

# Override DATABASE_PATH before importing repo, so all repo functions
# operate against our temp database instead of the real one.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_PATH"] = _tmp.name

from backend.db.init_db import init_db
from backend.db import repo


@pytest.fixture(autouse=True, scope="module")
def setup_db():
    """Create the schema in the temp database once for the whole module."""
    # Remove the temp file so init_db sees a fresh path and creates it
    if os.path.exists(_tmp.name):
        os.remove(_tmp.name)
    init_db(_tmp.name)
    yield
    # Cleanup after all tests in this module
    if os.path.exists(_tmp.name):
        os.remove(_tmp.name)


class TestUserCRUD:
    """Round-trip tests for user creation, lookup, listing, and update."""

    def test_create_and_get_by_email(self):
        """Create a user, then look them up by email and verify fields."""
        user_id = repo.create_user(
            email="alice@example.com",
            role="admin",
            status="active",
            quota_ram_mb=2048,
            quota_cpu=2.0,
            quota_disk_gb=20,
        )

        # Should be a non-empty UUID string
        assert isinstance(user_id, str)
        assert len(user_id) > 0

        # Look up by email
        user = repo.get_user_by_email("alice@example.com")
        assert user is not None
        assert user["id"] == user_id
        assert user["email"] == "alice@example.com"
        assert user["role"] == "admin"
        assert user["status"] == "active"
        assert user["quota_ram_mb"] == 2048
        assert user["quota_cpu"] == 2.0
        assert user["quota_disk_gb"] == 20
        assert user["created_at"] is not None

    def test_get_by_id(self):
        """Look up the same user by UUID."""
        user_by_email = repo.get_user_by_email("alice@example.com")
        user_by_id = repo.get_user_by_id(user_by_email["id"])
        assert user_by_id is not None
        assert user_by_id["email"] == "alice@example.com"

    def test_get_nonexistent_returns_none(self):
        """Looking up an email that doesn't exist returns None, not an error."""
        assert repo.get_user_by_email("nobody@example.com") is None
        assert repo.get_user_by_id("nonexistent-uuid") is None

    def test_list_users(self):
        """list_users returns at least the user we created."""
        users = repo.list_users()
        assert isinstance(users, list)
        assert len(users) >= 1
        emails = [u["email"] for u in users]
        assert "alice@example.com" in emails

    def test_update_user_role(self):
        """Update a single field (role) and verify it changed."""
        user = repo.get_user_by_email("alice@example.com")
        repo.update_user(user["id"], role="user")
        updated = repo.get_user_by_id(user["id"])
        assert updated["role"] == "user"

    def test_update_user_multiple_fields(self):
        """Update multiple quota fields in one call."""
        user = repo.get_user_by_email("alice@example.com")
        repo.update_user(
            user["id"],
            quota_ram_mb=4096,
            quota_cpu=4.0,
            quota_disk_gb=50,
        )
        updated = repo.get_user_by_id(user["id"])
        assert updated["quota_ram_mb"] == 4096
        assert updated["quota_cpu"] == 4.0
        assert updated["quota_disk_gb"] == 50

    def test_update_user_rejects_bad_fields(self):
        """Attempting to update a non-whitelisted field raises ValueError."""
        user = repo.get_user_by_email("alice@example.com")
        with pytest.raises(ValueError, match="disallowed"):
            repo.update_user(user["id"], email="hacker@evil.com")


class TestSessionCRUD:
    """Round-trip tests for session creation, lookup, and deletion."""

    def test_create_and_get_session(self):
        """Create a session, look it up by token hash, verify fields."""
        user = repo.get_user_by_email("alice@example.com")
        session_id = repo.create_session(
            user_id=user["id"],
            refresh_token_hash="fakehash123",
            expires_at="2099-12-31T23:59:59Z",
        )

        assert isinstance(session_id, str)
        assert len(session_id) > 0

        session = repo.get_session_by_hash("fakehash123")
        assert session is not None
        assert session["id"] == session_id
        assert session["user_id"] == user["id"]
        assert session["refresh_token_hash"] == "fakehash123"

    def test_delete_session(self):
        """Deleting a session makes it unfindable by hash."""
        session = repo.get_session_by_hash("fakehash123")
        assert session is not None

        repo.delete_session(session["id"])

        assert repo.get_session_by_hash("fakehash123") is None
