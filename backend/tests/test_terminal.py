"""
Tests for the terminal exec endpoint (Phase 12.1).

Covers the injection-safety contract of POST /api/containers/{id}/exec:
the command must be a JSON array of strings, a plain string is rejected
with 400 before anything runs, access is enforced per-container, and the
happy path returns the exec's stdout/stderr/exit code.

LXD itself is not available in the test environment, so the single
happy-path test monkeypatches lxd_client.execute_command. The security
cases (403, 400-on-string) are checked BEFORE any LXD call, so they need
no stub — that ordering is itself part of what is under test.
"""

import uuid

import pytest
from falcon import testing

import backend.resources.terminal as terminal_module
from backend.app import create_app
from backend.auth.jwt_utils import create_access_token
from backend.db import repo


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


class TestContainerExecEndpoint:
    """Tests for POST /api/containers/{id}/exec."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Create app, an admin, a user, and a container assigned to the user."""
        self.app = create_app()
        self.client = testing.TestClient(self.app)

        self.admin_id = repo.create_user(
            email=_unique_email("exec-admin"),
            role="admin",
            status="active",
            quota_ram_mb=8192,
            quota_cpu=8.0,
            quota_disk_gb=100,
        )
        self.admin_token = create_access_token(self.admin_id, "admin")

        self.user_id = repo.create_user(
            email=_unique_email("exec-user"),
            role="user",
            status="active",
            quota_ram_mb=4096,
            quota_cpu=4.0,
            quota_disk_gb=40,
        )
        self.user_token = create_access_token(self.user_id, "user")

        self.container_id = repo.create_container_record(
            lxd_name=f"exec-c-{uuid.uuid4().hex[:8]}",
            image="ubuntu:22.04",
            created_by=self.admin_id,
            limit_ram_mb=1024,
            limit_cpu=1.0,
            limit_disk_gb=10,
        )
        repo.assign_container(self.user_id, self.container_id)

        yield

    def _admin_headers(self) -> dict:
        return {"Cookie": f"access_token={self.admin_token}"}

    def _user_headers(self) -> dict:
        return {"Cookie": f"access_token={self.user_token}"}

    # --- Auth enforcement ---

    def test_returns_401_without_auth(self):
        """Unauthenticated request must return 401."""
        result = self.client.simulate_post(
            f"/api/containers/{self.container_id}/exec",
            json={"command": ["echo", "hi"]},
        )
        assert result.status_code == 401

    def test_user_without_access_gets_403(self):
        """A user without an assignment to the container gets 403.

        This must fail BEFORE any LXD call — no stub is installed, so if the
        endpoint tried to exec it would error differently.
        """
        other_id = repo.create_container_record(
            lxd_name=f"exec-other-{uuid.uuid4().hex[:8]}",
            image="ubuntu:22.04",
            created_by=self.admin_id,
            limit_ram_mb=256,
            limit_cpu=0.25,
            limit_disk_gb=2,
        )
        result = self.client.simulate_post(
            f"/api/containers/{other_id}/exec",
            headers=self._user_headers(),
            json={"command": ["echo", "hi"]},
        )
        assert result.status_code == 403

    # --- Injection-safety contract ---

    def test_string_command_is_rejected_400(self, monkeypatch):
        """A plain-string command is rejected with 400 and nothing runs.

        The injection defense: "echo hacked; rm -rf /" as a string must
        never reach exec. We install a stub that records if it was called
        and assert it was NOT — the request is refused before execution.
        """
        called = {"ran": False}

        def _fake_exec(name, command):
            called["ran"] = True
            return (0, "", "")

        monkeypatch.setattr(
            terminal_module.lxd_client, "execute_command", _fake_exec
        )

        result = self.client.simulate_post(
            f"/api/containers/{self.container_id}/exec",
            headers=self._admin_headers(),
            json={"command": "echo hacked; rm -rf /"},
        )
        assert result.status_code == 400
        assert called["ran"] is False

    def test_command_with_non_string_element_is_rejected_400(self, monkeypatch):
        """An array containing a non-string element is rejected with 400."""
        called = {"ran": False}

        def _fake_exec(name, command):
            called["ran"] = True
            return (0, "", "")

        monkeypatch.setattr(
            terminal_module.lxd_client, "execute_command", _fake_exec
        )

        result = self.client.simulate_post(
            f"/api/containers/{self.container_id}/exec",
            headers=self._admin_headers(),
            json={"command": ["echo", ["nested"]]},
        )
        assert result.status_code == 400
        assert called["ran"] is False

    def test_empty_command_array_is_rejected_400(self):
        """An empty command array is rejected with 400 (nothing to run)."""
        result = self.client.simulate_post(
            f"/api/containers/{self.container_id}/exec",
            headers=self._admin_headers(),
            json={"command": []},
        )
        assert result.status_code == 400

    def test_missing_command_field_is_rejected_400(self):
        """A body with no 'command' field is rejected with 400."""
        result = self.client.simulate_post(
            f"/api/containers/{self.container_id}/exec",
            headers=self._admin_headers(),
            json={},
        )
        assert result.status_code == 400

    # --- Happy path ---

    def test_valid_array_command_runs_and_returns_output(self, monkeypatch):
        """A valid array command is executed and its output returned.

        Stubs execute_command since LXD is unavailable in tests, and
        asserts the argv was passed through unchanged as a list.
        """
        captured = {}

        def _fake_exec(name, command):
            captured["name"] = name
            captured["command"] = command
            return (0, "hello from terminal\n", "")

        monkeypatch.setattr(
            terminal_module.lxd_client, "execute_command", _fake_exec
        )

        result = self.client.simulate_post(
            f"/api/containers/{self.container_id}/exec",
            headers=self._user_headers(),
            json={"command": ["echo", "hello from terminal"]},
        )
        assert result.status_code == 200
        body = result.json
        assert body["exit_code"] == 0
        assert "hello from terminal" in body["stdout"]
        assert body["container_id"] == self.container_id
        # The argv reached LXD as an unmodified list of strings.
        assert captured["command"] == ["echo", "hello from terminal"]

    def test_exec_writes_audit_log(self, monkeypatch):
        """A successful exec appends a container.exec entry to the audit log."""
        monkeypatch.setattr(
            terminal_module.lxd_client,
            "execute_command",
            lambda name, command: (0, "ok\n", ""),
        )

        result = self.client.simulate_post(
            f"/api/containers/{self.container_id}/exec",
            headers=self._user_headers(),
            json={"command": ["whoami"]},
        )
        assert result.status_code == 200

        # Verify an audit entry was written for this container by this user.
        conn = repo.get_connection()
        try:
            row = conn.execute(
                "SELECT action, target, detail FROM audit_log "
                "WHERE target = ? AND action = 'container.exec' "
                "ORDER BY created_at DESC LIMIT 1",
                (self.container_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["action"] == "container.exec"
        assert "whoami" in row["detail"]

    def test_response_shape(self, monkeypatch):
        """Response must contain container_id, exit_code, stdout, stderr."""
        monkeypatch.setattr(
            terminal_module.lxd_client,
            "execute_command",
            lambda name, command: (0, "out", "err"),
        )
        result = self.client.simulate_post(
            f"/api/containers/{self.container_id}/exec",
            headers=self._admin_headers(),
            json={"command": ["true"]},
        )
        assert result.status_code == 200
        assert set(result.json.keys()) == {
            "container_id",
            "exit_code",
            "stdout",
            "stderr",
        }
