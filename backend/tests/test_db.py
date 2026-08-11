"""
Tests for the database repo layer (users, sessions, containers,
assignments, and audit log).

Uses the shared temp database from conftest.py so tests don't touch
the real application database.
"""

import os

import pytest

from backend.db import repo


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


class TestContainerCRUD:
    """Tests for container record creation, lookup, rename, and soft-delete."""

    def test_create_and_get_by_id(self):
        """Create a container record and look it up by UUID."""
        user = repo.get_user_by_email("alice@example.com")
        cid = repo.create_container_record(
            lxd_name="test-container-1",
            image="ubuntu:22.04",
            created_by=user["id"],
        )

        assert isinstance(cid, str)
        assert len(cid) > 0

        container = repo.get_container_by_id(cid)
        assert container is not None
        assert container["lxd_name"] == "test-container-1"
        assert container["image"] == "ubuntu:22.04"
        assert container["created_by"] == user["id"]
        assert container["deleted_at"] is None

    def test_get_by_lxd_name(self):
        """Look up a container by its LXD-side name."""
        container = repo.get_container_by_lxd_name("test-container-1")
        assert container is not None
        assert container["lxd_name"] == "test-container-1"

    def test_list_active_containers(self):
        """list_active_containers includes the created container."""
        containers = repo.list_active_containers()
        assert len(containers) >= 1
        names = [c["lxd_name"] for c in containers]
        assert "test-container-1" in names

    def test_rename_container(self):
        """Renaming updates lxd_name but keeps the same UUID."""
        container = repo.get_container_by_lxd_name("test-container-1")
        original_id = container["id"]

        repo.rename_container_lxd_name(original_id, "test-container-renamed")

        updated = repo.get_container_by_id(original_id)
        assert updated["lxd_name"] == "test-container-renamed"

        # Rename back for subsequent tests
        repo.rename_container_lxd_name(original_id, "test-container-1")

    def test_soft_delete(self):
        """Soft-deleting sets deleted_at but keeps the row in the table."""
        user = repo.get_user_by_email("alice@example.com")
        cid = repo.create_container_record(
            lxd_name="to-be-deleted",
            image="ubuntu:22.04",
            created_by=user["id"],
        )

        repo.soft_delete_container(cid)

        # Row still exists with deleted_at set
        container = repo.get_container_by_id(cid)
        assert container is not None
        assert container["deleted_at"] is not None

        # But it no longer appears in the active list
        active = repo.list_active_containers()
        active_ids = [c["id"] for c in active]
        assert cid not in active_ids


class TestAssignmentRoundTrip:
    """Full grant → access check → revoke → verify row persists cycle."""

    def test_assign_check_revoke_cycle(self):
        """The core round-trip the build guide asks for:
        create user, create container, assign, confirm access,
        revoke, confirm no access, confirm row still exists.
        """
        # Create a fresh user for this test
        uid = repo.create_user(
            email="bob@example.com",
            role="user",
            status="active",
            quota_ram_mb=1024,
            quota_cpu=1.0,
            quota_disk_gb=10,
        )

        # Get the container created in TestContainerCRUD
        container = repo.get_container_by_lxd_name("test-container-1")
        cid = container["id"]

        # Assign — user should now have access
        assignment_id = repo.assign_container(uid, cid)
        assert isinstance(assignment_id, str)
        assert repo.user_has_access(uid, cid) is True

        # list_assignments_for_user should include this container
        assignments = repo.list_assignments_for_user(uid)
        assert len(assignments) >= 1
        assert any(a["container_id"] == cid for a in assignments)

        # Revoke — user should no longer have access
        repo.revoke_assignment(uid, cid)
        assert repo.user_has_access(uid, cid) is False

        # But the assignment row must still exist in the database (just
        # with active=0), not hard-deleted — verify by querying directly
        import sqlite3
        conn = sqlite3.connect(str(os.environ["DATABASE_PATH"]))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM assignments WHERE id = ?", (assignment_id,)
        ).fetchone()
        conn.close()

        assert row is not None, "Assignment row was hard-deleted!"
        assert row["active"] == 0


class TestAuditLog:
    """Tests for the write-only audit log."""

    def test_write_audit_log(self):
        """Writing an audit entry succeeds and the row is queryable."""
        user = repo.get_user_by_email("alice@example.com")
        repo.write_audit_log(
            user_id=user["id"],
            action="container.delete",
            target="some-container-uuid",
            detail="Deleted test container",
        )

        # Verify the entry exists by querying directly
        import sqlite3
        conn = sqlite3.connect(str(os.environ["DATABASE_PATH"]))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE user_id = ? AND action = ?",
            (user["id"], "container.delete"),
        ).fetchall()
        conn.close()

        assert len(rows) >= 1
        entry = dict(rows[0])
        assert entry["target"] == "some-container-uuid"
        assert entry["detail"] == "Deleted test container"
