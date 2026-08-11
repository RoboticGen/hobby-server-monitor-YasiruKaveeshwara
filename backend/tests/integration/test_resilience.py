"""
Degraded-mode behaviour: what every endpoint does when LXD is unreachable.

Decision 7.10 draws a line through the API. Endpoints that only need SQLite or
TinyFlux must keep working during an LXD outage; endpoints that genuinely
require the daemon must fail cleanly with 503 rather than 500 or a hang.

Getting this wrong in either direction is a real outage: a dashboard that
white-screens because /api/containers raised, or a delete that reports success
while the container is still running.

Each test here runs the request twice — healthy and down — so the assertion is
about the *difference*, not about a hardcoded expectation that might be passing
for the wrong reason.
"""

import pytest
from falcon import testing

from backend.app import create_app
from backend.db import repo

from .conftest import make_user

pytestmark = pytest.mark.usefixtures("isolated_db", "isolated_tsdb")


# Endpoints that must survive an LXD outage, because their data comes from
# SQLite or TinyFlux. A 5xx here means an outage in one subsystem took down
# a page that had no reason to need it.
SURVIVES_OUTAGE = "must keep serving during an LXD outage"

# Endpoints that cannot work without the daemon. These must answer 503
# Service Unavailable — a retryable, honest "the backend is down", not a 500
# (which reads as a bug) and not a 200 (which would be a lie).
REQUIRES_LXD = "must answer 503, not 500 or a false success"


@pytest.fixture(scope="module")
def env(isolated_db, isolated_tsdb, fake_lxd):
    """An admin, a user, and one container assigned to that user."""
    client = testing.TestClient(create_app())
    admin_id, admin_headers = make_user("resilience-admin@example.com", role="admin")
    user_id, user_headers = make_user("resilience-user@example.com")

    fake_lxd.add("resilience-box")
    container_id = repo.create_container_record(
        lxd_name="resilience-box",
        image="ubuntu:22.04",
        created_by=admin_id,
        limit_ram_mb=512,
        limit_cpu=1.0,
        limit_disk_gb=5,
    )
    repo.assign_container(user_id, container_id)

    return {
        "client": client,
        "admin": admin_headers,
        "user": user_headers,
        "user_id": user_id,
        "container_id": container_id,
        "lxd": fake_lxd,
    }


class TestReadPathSurvivesOutage:
    """DB- and TSDB-backed reads must not depend on LXD."""

    def test_health_reports_degraded_instead_of_failing(self, env):
        """/health is the outage signal, so it must never itself fail.

        It always returns 200; the body carries the state. A 503 here would
        be self-defeating — the endpoint whose job is to report the outage
        would be unreachable during one.
        """
        healthy = env["client"].simulate_get("/health")
        assert healthy.status_code == 200
        assert healthy.json == {"status": "ok", "lxd_reachable": True}

        env["lxd"].set_down(True)
        degraded = env["client"].simulate_get("/health")
        env["lxd"].set_down(False)

        assert degraded.status_code == 200, (
            "/health returned non-200 during an outage; it is the one "
            "endpoint that must always answer"
        )
        assert degraded.json == {"status": "degraded", "lxd_reachable": False}

    def test_container_list_degrades_to_unknown_status(self, env):
        """Listing containers still works; live status becomes 'Unknown'.

        The DB knows which containers exist and who they belong to. Only the
        running/stopped status needs LXD, so an outage should blank that one
        field rather than fail the request.
        """
        healthy = env["client"].simulate_get(
            "/api/containers", headers=env["admin"]
        )
        assert healthy.status_code == 200
        assert healthy.json["containers"][0]["lxd_status"] == "Running"

        env["lxd"].set_down(True)
        degraded = env["client"].simulate_get(
            "/api/containers", headers=env["admin"]
        )
        env["lxd"].set_down(False)

        assert degraded.status_code == 200, SURVIVES_OUTAGE
        entry = degraded.json["containers"][0]
        assert entry["lxd_status"] == "Unknown"
        # The DB-sourced identity fields are untouched by the outage.
        assert entry["lxd_name"] == "resilience-box"
        assert entry["id"] == env["container_id"]

    def test_user_list_is_unaffected(self, env):
        """/api/users reads SQLite only and must be byte-identical."""
        healthy = env["client"].simulate_get("/api/users", headers=env["admin"])
        env["lxd"].set_down(True)
        degraded = env["client"].simulate_get("/api/users", headers=env["admin"])
        env["lxd"].set_down(False)

        assert degraded.status_code == 200, SURVIVES_OUTAGE
        assert degraded.json == healthy.json

    def test_auth_still_works(self, env):
        """Authentication must not consult LXD — otherwise nobody can sign in."""
        env["lxd"].set_down(True)
        result = env["client"].simulate_get("/api/auth/me", headers=env["user"])
        env["lxd"].set_down(False)

        assert result.status_code == 200, (
            "authentication failed during an LXD outage — sign-in must not "
            "depend on the container daemon"
        )

    def test_metrics_history_still_serves_stored_points(self, env):
        """Charts read TinyFlux, so history is fully available during an outage."""
        env["lxd"].set_down(True)
        result = env["client"].simulate_get(
            f"/api/containers/{env['container_id']}/history",
            params={"window": "1h"},
            headers=env["user"],
        )
        env["lxd"].set_down(False)

        assert result.status_code == 200, SURVIVES_OUTAGE
        assert "points" in result.json


class TestWritePathFailsCleanly:
    """Operations that genuinely need LXD must 503, and must not lie."""

    def test_create_returns_503_and_writes_no_db_row(self, env):
        """A failed create must not leave an orphan DB record.

        If the row were written before the LXD call, an outage would leave a
        container that exists in the UI and in quota math but nowhere else.
        """
        before = len(repo.list_active_containers())

        env["lxd"].set_down(True)
        result = env["client"].simulate_post(
            "/api/containers",
            headers=env["admin"],
            json={"name": "ghost-box", "image": "ubuntu:22.04",
                  "limits": {"ram_mb": 256, "cpu": 0.5, "disk_gb": 2}},
        )
        env["lxd"].set_down(False)

        assert result.status_code == 503, REQUIRES_LXD
        assert len(repo.list_active_containers()) == before, (
            "a DB row was created for a container LXD never made"
        )
        assert repo.get_container_by_lxd_name("ghost-box") is None

    def test_state_change_returns_503(self, env):
        """Start/stop cannot be faked, so it must fail loudly."""
        env["lxd"].set_down(True)
        result = env["client"].simulate_patch(
            f"/api/containers/{env['container_id']}",
            headers=env["admin"],
            json={"action": "start"},
        )
        env["lxd"].set_down(False)
        assert result.status_code == 503, REQUIRES_LXD

    def test_exec_returns_503(self, env):
        """Terminal commands need a live daemon."""
        env["lxd"].set_down(True)
        result = env["client"].simulate_post(
            f"/api/containers/{env['container_id']}/exec",
            headers=env["user"],
            json={"command": ["uptime"]},
        )
        env["lxd"].set_down(False)
        assert result.status_code == 503, REQUIRES_LXD

    def test_delete_returns_503_and_does_not_soft_delete(self, env):
        """A failed delete must not mark the row deleted.

        Soft-deleting on a failed LXD call would orphan a still-running
        container: invisible to the UI, still consuming host resources, and
        no longer counted against anyone's quota.
        """
        env["lxd"].set_down(True)
        result = env["client"].simulate_delete(
            f"/api/containers/{env['container_id']}", headers=env["admin"]
        )
        env["lxd"].set_down(False)

        assert result.status_code == 503, REQUIRES_LXD
        record = repo.get_container_by_id(env["container_id"])
        assert record["deleted_at"] is None, (
            "container was marked deleted even though LXD never removed it"
        )
        # And it is still really there.
        assert "resilience-box" in env["lxd"].containers._store

    def test_limit_change_does_not_desync_the_db_cache(self, env):
        """A failed limit change must leave the DB cache at the old value.

        The DB copy of the limits is what quota math reads. If it advanced
        while LXD kept the old value, every subsequent quota decision would
        be made on numbers that do not match reality.
        """
        before = repo.get_container_by_id(env["container_id"])["limit_ram_mb"]

        env["lxd"].set_down(True)
        result = env["client"].simulate_patch(
            f"/api/containers/{env['container_id']}",
            headers=env["admin"],
            json={"limits": {"ram_mb": 4096, "cpu": 2.0, "disk_gb": 40}},
        )
        env["lxd"].set_down(False)

        assert result.status_code == 503, REQUIRES_LXD
        after = repo.get_container_by_id(env["container_id"])["limit_ram_mb"]
        assert after == before, (
            f"DB cache drifted to {after}MB after a failed LXD update "
            f"(was {before}MB) — quota math would now use a fiction"
        )


class TestOutageIsRecoverable:
    """The system must resume normally once the daemon returns."""

    def test_operations_work_again_after_recovery(self, env):
        """No sticky failure state: a restored daemon restores full function.

        A memoized client handle that cached the failed connection would show
        up here as a permanent outage after a transient one.
        """
        env["lxd"].set_down(True)
        failed = env["client"].simulate_patch(
            f"/api/containers/{env['container_id']}",
            headers=env["admin"],
            json={"action": "stop"},
        )
        assert failed.status_code == 503

        env["lxd"].set_down(False)
        recovered = env["client"].simulate_patch(
            f"/api/containers/{env['container_id']}",
            headers=env["admin"],
            json={"action": "stop"},
        )
        assert recovered.status_code == 200, (
            "the API stayed broken after LXD came back — a failed connection "
            "is being cached"
        )

    def test_health_flips_back_to_ok(self, env):
        """The degraded banner must clear on its own once LXD returns."""
        env["lxd"].set_down(True)
        assert env["client"].simulate_get("/health").json["status"] == "degraded"

        env["lxd"].set_down(False)
        assert env["client"].simulate_get("/health").json["status"] == "ok"


class TestMalformedInput:
    """Bad request bodies must be 400s, never unhandled 500s."""

    # Each case uses a distinct container name. They previously shared
    # "ok-box", which meant the negative-limit case (which wrongly succeeded)
    # created it and every later case then short-circuited on the 409
    # duplicate-name check — passing without ever reaching the code they
    # were written to exercise.

    @pytest.mark.parametrize(
        "body,label",
        [
            (None, "no body"),
            ({}, "empty object"),
            ({"name": None, "image": None}, "null fields"),
            ({"name": 12345, "image": "ubuntu:22.04"}, "non-string name"),
            ({"name": "bad-no-image"}, "missing image"),
            ({"name": "bad-limits-type", "image": "ubuntu:22.04",
              "limits": "not-an-object"}, "limits wrong type"),
            ({"name": "bad-negative", "image": "ubuntu:22.04",
              "limits": {"ram_mb": -512}}, "negative limit"),
            ({"name": "bad-nonnumeric", "image": "ubuntu:22.04",
              "limits": {"ram_mb": "lots"}}, "non-numeric limit"),
        ],
    )
    def test_container_create_rejects_bad_bodies(self, env, body, label):
        """Every malformed create body is a 4xx, not a crash."""
        result = env["client"].simulate_post(
            "/api/containers", headers=env["admin"], json=body
        )
        assert 400 <= result.status_code < 500, (
            f"{label} produced {result.status_code}; malformed input must be "
            f"a 4xx, and a 5xx means an unhandled exception"
        )

    def test_invalid_json_body_is_a_400(self, env):
        """Syntactically broken JSON must not reach the handler as a crash."""
        result = env["client"].simulate_post(
            "/api/containers",
            headers={**env["admin"], "Content-Type": "application/json"},
            body="{not valid json",
        )
        assert 400 <= result.status_code < 500, (
            f"malformed JSON gave {result.status_code}"
        )

    @pytest.mark.parametrize("window", ["", "0h", "1y", "-1h", "24", "abc",
                                        "1h; DROP TABLE users"])
    def test_history_rejects_bad_windows(self, env, window):
        """Unknown chart windows are 400s, never a fallback to raw data."""
        result = env["client"].simulate_get(
            f"/api/containers/{env['container_id']}/history",
            params={"window": window},
            headers=env["user"],
        )
        assert result.status_code == 400, (
            f"window={window!r} returned {result.status_code}"
        )

    def test_metrics_latest_without_container_param_is_400(self, env):
        """A missing required query param is a client error."""
        result = env["client"].simulate_get(
            "/api/metrics/latest", headers=env["user"]
        )
        assert 400 <= result.status_code < 500

    def test_malformed_container_id_does_not_500(self, env):
        """Non-UUID path segments must be handled, not crash the lookup."""
        for bad_id in ["not-a-uuid", "../../etc/passwd", "%00", "1 OR 1=1"]:
            result = env["client"].simulate_get(
                f"/api/containers/{bad_id}", headers=env["admin"]
            )
            assert result.status_code < 500, (
                f"container id {bad_id!r} caused {result.status_code}"
            )
