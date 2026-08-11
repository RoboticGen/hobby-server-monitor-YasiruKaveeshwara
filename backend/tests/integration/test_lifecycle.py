"""
End-to-end lifecycle: one continuous story across every backend subsystem.

The unit suite tests each phase against fixtures it created itself. This file
instead runs a single ordered narrative — bootstrap admin, invite a user,
create a container in LXD, assign it, collect metrics, read them back, exec a
command, change limits, delete, audit — where each step consumes the real
output of the previous one.

That ordering is the point. A container created through the API (not
repo.create_container_record) is what the assignment endpoint then reads, what
the collector then polls, and what the metrics endpoint then serves. Bugs that
live in the seams between phases — a UUID vs lxd_name mixup, a DB cache that
drifts from LXD, an assignment that outlives its container — only surface when
the steps are chained like this.

Tests share module-scoped state deliberately and run in declaration order.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from falcon import testing

import backend.resources.auth as auth_module
from backend.app import create_app
from backend.collector.collector import run_collector_loop
from backend.db import repo
from backend.tsdb import store

pytestmark = pytest.mark.usefixtures("isolated_db", "isolated_tsdb")

# Shared state threaded through the ordered steps below.
STATE: dict = {}

ADMIN_EMAIL = "lifecycle-admin@example.com"
USER_EMAIL = "lifecycle-user@example.com"


@pytest.fixture(scope="module")
def app_client(isolated_db, isolated_tsdb):
    """One test client for the whole narrative."""
    return testing.TestClient(create_app())


def _cookies(result) -> dict:
    """Return {name: value} for the cookies a response set.

    Falcon parses Set-Cookie into result.cookies (name -> Cookie), which also
    carries the httpOnly/SameSite/Secure flags asserted on in step 1.
    """
    assert result.cookies, (
        f"response set no cookies (status {result.status_code})"
    )
    return {name: c.value for name, c in result.cookies.items()}


class TestFullLifecycle:
    """Ordered end-to-end flow. Each test builds on the previous one's output."""

    # ---------------------------------------------------------------- step 1
    def test_01_bootstrap_admin_is_created_on_first_signin(
        self, app_client, monkeypatch
    ):
        """The ADMIN_BOOTSTRAP_EMAIL account is created as an active admin.

        Google's token exchange and ID-token verification are stubbed — those
        are network calls to Google, not our logic. Everything after the
        verified email (the users-table lookup, the bootstrap branch, session
        creation, cookie issuance) is the real code path.
        """
        from backend.config import config

        object.__setattr__(config, "admin_bootstrap_email", ADMIN_EMAIL)

        monkeypatch.setattr(
            auth_module, "exchange_code_for_tokens",
            lambda code: {"id_token": "fake-id-token"},
        )
        monkeypatch.setattr(
            auth_module, "verify_and_decode_id_token",
            lambda tok: {"email": ADMIN_EMAIL, "email_verified": True},
        )

        result = app_client.simulate_get(
            "/api/auth/google/callback", params={"code": "fake-auth-code"}
        )

        # Redirects to the frontend on success
        assert result.status_code == 302

        admin = repo.get_user_by_email(ADMIN_EMAIL)
        assert admin is not None, "bootstrap admin was not created"
        assert admin["role"] == "admin"
        assert admin["status"] == "active"

        # A server-side session row must exist — logout has to have something
        # to revoke, otherwise "logout" is only a client-side illusion (7.1).
        conn = repo.get_connection()
        try:
            sessions = conn.execute(
                "SELECT * FROM sessions WHERE user_id = ?", (admin["id"],)
            ).fetchall()
        finally:
            conn.close()
        assert len(sessions) == 1
        # The raw refresh token must never be stored — only its hash.
        assert len(sessions[0]["refresh_token_hash"]) == 64

        cookies = _cookies(result)
        assert "access_token" in cookies and "refresh_token" in cookies
        assert sessions[0]["refresh_token_hash"] != cookies["refresh_token"]

        # Both cookies must be httpOnly (unreadable from JS, so XSS cannot
        # exfiltrate the session) and SameSite to blunt CSRF.
        for name in ("access_token", "refresh_token"):
            cookie = result.cookies[name]
            assert cookie.http_only is True, f"{name} is not httpOnly"
            assert cookie.same_site.lower() == "lax", (
                f"{name} SameSite={cookie.same_site!r}"
            )
        # The short access-token lifetime is what makes a 15-minute
        # revocation window rather than a 7-day one.
        assert result.cookies["access_token"].max_age == 900

        STATE["admin_id"] = admin["id"]
        STATE["admin_headers"] = {
            "Cookie": f"access_token={cookies['access_token']}"
        }
        STATE["admin_refresh"] = cookies["refresh_token"]

    def test_02_uninvited_email_is_rejected(self, app_client, monkeypatch):
        """An email that is neither invited nor the bootstrap address gets 403."""
        monkeypatch.setattr(
            auth_module, "exchange_code_for_tokens",
            lambda code: {"id_token": "fake"},
        )
        monkeypatch.setattr(
            auth_module, "verify_and_decode_id_token",
            lambda tok: {"email": "stranger@example.com"},
        )

        result = app_client.simulate_get(
            "/api/auth/google/callback", params={"code": "x"}
        )
        assert result.status_code == 403
        assert repo.get_user_by_email("stranger@example.com") is None

    def test_03_me_returns_the_admin_profile(self, app_client):
        """The issued cookie authenticates against /api/auth/me."""
        result = app_client.simulate_get(
            "/api/auth/me", headers=STATE["admin_headers"]
        )
        assert result.status_code == 200
        assert result.json["email"] == ADMIN_EMAIL
        assert result.json["role"] == "admin"

    # ---------------------------------------------------------------- step 2
    def test_04_admin_invites_a_container_user(self, app_client):
        """Invite creates an 'invited' user with the given quota."""
        result = app_client.simulate_post(
            "/api/users",
            headers=STATE["admin_headers"],
            json={
                "email": USER_EMAIL,
                "role": "user",
                "quota_ram_mb": 2048,
                "quota_cpu": 2.0,
                "quota_disk_gb": 20,
            },
        )
        assert result.status_code == 201
        assert result.json["status"] == "invited"
        STATE["user_id"] = result.json["id"]

    def test_05_invited_user_becomes_active_on_first_signin(
        self, app_client, monkeypatch
    ):
        """Signing in flips 'invited' to 'active' and does NOT grant admin.

        The build guide calls this out explicitly: only the bootstrap email
        gets admin. An invited user signing in must keep role='user'.
        """
        monkeypatch.setattr(
            auth_module, "exchange_code_for_tokens",
            lambda code: {"id_token": "fake"},
        )
        monkeypatch.setattr(
            auth_module, "verify_and_decode_id_token",
            lambda tok: {"email": USER_EMAIL},
        )

        result = app_client.simulate_get(
            "/api/auth/google/callback", params={"code": "y"}
        )
        assert result.status_code == 302

        user = repo.get_user_by_id(STATE["user_id"])
        assert user["status"] == "active"
        assert user["role"] == "user", "invited user must not become admin"

        cookies = _cookies(result)
        STATE["user_headers"] = {
            "Cookie": f"access_token={cookies['access_token']}"
        }

    def test_06_non_admin_cannot_reach_admin_endpoints(self, app_client):
        """A signed-in 'user' is forbidden (403, not 401) from admin routes."""
        for method, path in [
            ("get", "/api/users"),
            ("post", "/api/users"),
            ("get", "/api/accounting"),
            ("post", "/api/containers"),
        ]:
            result = getattr(app_client, f"simulate_{method}")(
                path, headers=STATE["user_headers"], json={}
            )
            assert result.status_code == 403, (
                f"{method.upper()} {path} gave {result.status_code} to a "
                f"non-admin; expected 403"
            )

    # ---------------------------------------------------------------- step 3
    def test_07_admin_creates_a_container_through_the_api(
        self, app_client, fake_lxd
    ):
        """POST /api/containers creates it in LXD and records it in the DB."""
        name = "lifecycle-web"
        result = app_client.simulate_post(
            "/api/containers",
            headers=STATE["admin_headers"],
            json={
                "name": name,
                "image": "ubuntu:22.04",
                "limits": {"ram_mb": 1024, "cpu": 1.0, "disk_gb": 10},
            },
        )
        assert result.status_code == 201, result.json
        STATE["container_id"] = result.json["id"]
        STATE["container_name"] = name

        # It exists in the (fake) LXD daemon, created via the real wrapper.
        assert name in fake_lxd.containers._store
        created = fake_lxd.containers._store[name]
        assert created.config["limits.memory"] == "1024MB"

        # And the DB cached the limits quota math depends on.
        record = repo.get_container_by_id(STATE["container_id"])
        assert record["limit_ram_mb"] == 1024
        assert record["deleted_at"] is None

    def test_08_invalid_container_names_are_rejected_server_side(
        self, app_client, fake_lxd
    ):
        """Names that break LXD rules are refused with 400 and nothing created."""
        before = set(fake_lxd.containers._store)
        for bad in ["UPPERCASE", "has spaces", "-leading", "trailing-",
                    "under_score", "", "a" * 64]:
            result = app_client.simulate_post(
                "/api/containers",
                headers=STATE["admin_headers"],
                json={"name": bad, "image": "ubuntu:22.04",
                      "limits": {"ram_mb": 256, "cpu": 0.5, "disk_gb": 2}},
            )
            assert result.status_code == 400, (
                f"name {bad!r} was accepted with {result.status_code}"
            )
        assert set(fake_lxd.containers._store) == before, (
            "a rejected name still created a container"
        )

    def test_09_duplicate_container_name_conflicts(self, app_client, fake_lxd):
        """Re-using an existing name returns 409, not a second container."""
        result = app_client.simulate_post(
            "/api/containers",
            headers=STATE["admin_headers"],
            json={"name": STATE["container_name"], "image": "ubuntu:22.04",
                  "limits": {"ram_mb": 256, "cpu": 0.5, "disk_gb": 2}},
        )
        assert result.status_code == 409

    # ---------------------------------------------------------------- step 4
    def test_10_user_cannot_see_unassigned_container(self, app_client):
        """Before assignment the container is invisible and 403 by id (not 404).

        403-not-404 is the explicit requirement: 404 would let an attacker
        enumerate valid container ids by comparing response codes.
        """
        listing = app_client.simulate_get(
            "/api/containers", headers=STATE["user_headers"]
        )
        assert listing.status_code == 200
        assert listing.json["containers"] == []

        detail = app_client.simulate_get(
            f"/api/containers/{STATE['container_id']}",
            headers=STATE["user_headers"],
        )
        assert detail.status_code == 403

    def test_11_nonexistent_container_also_returns_403_for_users(
        self, app_client
    ):
        """A non-existent id must be indistinguishable from an unassigned one."""
        result = app_client.simulate_get(
            f"/api/containers/{uuid.uuid4()}", headers=STATE["user_headers"]
        )
        assert result.status_code == 403, (
            "a non-existent id returned a different code than an unassigned "
            "one, which leaks container existence"
        )

    def test_12_admin_assigns_the_container(self, app_client):
        """Granting access makes the container visible to that user."""
        result = app_client.simulate_post(
            f"/api/users/{STATE['user_id']}/containers/{STATE['container_id']}",
            headers=STATE["admin_headers"],
        )
        assert result.status_code == 201

        listing = app_client.simulate_get(
            "/api/containers", headers=STATE["user_headers"]
        )
        names = [c["lxd_name"] for c in listing.json["containers"]]
        assert names == [STATE["container_name"]]

    def test_13_double_assignment_conflicts(self, app_client):
        """Granting an already-active assignment returns 409, not a duplicate row."""
        result = app_client.simulate_post(
            f"/api/users/{STATE['user_id']}/containers/{STATE['container_id']}",
            headers=STATE["admin_headers"],
        )
        assert result.status_code == 409

    def test_14_assignment_beyond_quota_is_blocked_with_the_number(
        self, app_client, fake_lxd
    ):
        """Over-quota assignment is a hard 400 naming the exact overage (7.3)."""
        # The user's quota is 2048MB and 1024MB is already assigned. A second
        # 1536MB container pushes them 512MB over.
        result = app_client.simulate_post(
            "/api/containers",
            headers=STATE["admin_headers"],
            json={"name": "lifecycle-big", "image": "ubuntu:22.04",
                  "limits": {"ram_mb": 1536, "cpu": 0.5, "disk_gb": 5}},
        )
        assert result.status_code == 201
        big_id = result.json["id"]

        grant = app_client.simulate_post(
            f"/api/users/{STATE['user_id']}/containers/{big_id}",
            headers=STATE["admin_headers"],
        )
        assert grant.status_code == 400
        # The message must carry the specific number, not a vague refusal.
        assert "512" in grant.json["description"]
        assert "RAM" in grant.json["description"]
        STATE["big_container_id"] = big_id

    # ---------------------------------------------------------------- step 5
    def test_15_collector_writes_metrics_for_active_containers(
        self, fake_lxd, monkeypatch
    ):
        """One collector pass polls LXD and writes a TinyFlux point each.

        run_collector_loop is an infinite loop, so time.sleep is patched to
        raise after the first iteration — this runs the real loop body
        (including its per-container error handling) exactly once.
        """
        import backend.collector.collector as collector_module

        class _StopLoop(Exception):
            pass

        def _one_pass(_seconds):
            raise _StopLoop

        monkeypatch.setattr(collector_module.time, "sleep", _one_pass)

        with pytest.raises(_StopLoop):
            run_collector_loop()

        point = store.get_latest_point(STATE["container_id"])
        assert point is not None, "collector wrote no point"
        # Fields must match the TinyFlux layout the metrics endpoint serves.
        assert point["ram_used_mb"] == pytest.approx(256.0)
        assert point["pid_count"] == 17.0
        assert point["resolution"] == "raw"

    def test_16_collector_survives_lxd_being_down(self, fake_lxd, monkeypatch):
        """A fully-down LXD produces warnings, not a crashed collector.

        This is the brief's independence requirement: the collector must not
        die because the daemon restarted.
        """
        import backend.collector.collector as collector_module

        class _StopLoop(Exception):
            pass

        monkeypatch.setattr(
            collector_module.time, "sleep",
            lambda _s: (_ for _ in ()).throw(_StopLoop()),
        )
        fake_lxd.set_down(True)

        # The loop body must complete and reach sleep() despite every
        # container failing — no LXDUnavailableError escapes.
        with pytest.raises(_StopLoop):
            run_collector_loop()

        fake_lxd.set_down(False)

    def test_17_metrics_latest_serves_the_collected_point(self, app_client):
        """The assigned user can poll /api/metrics/latest for their container."""
        result = app_client.simulate_get(
            "/api/metrics/latest",
            params={"container": STATE["container_id"]},
            headers=STATE["user_headers"],
        )
        assert result.status_code == 200
        assert result.json["point"]["ram_used_mb"] == pytest.approx(256.0)

    def test_18_metrics_latest_never_calls_lxd(self, app_client, fake_lxd):
        """Polling must keep working with LXD down (decision 7.5).

        The endpoint reads TinyFlux only, so dashboard cost is independent of
        LXD load and survives an outage. With the daemon down it must still
        return 200 and real numbers.
        """
        fake_lxd.set_down(True)
        result = app_client.simulate_get(
            "/api/metrics/latest",
            params={"container": STATE["container_id"]},
            headers=STATE["user_headers"],
        )
        fake_lxd.set_down(False)

        assert result.status_code == 200, (
            "metrics/latest failed while LXD was down — it must not depend "
            "on LXD (decision 7.5)"
        )
        assert result.json["point"] is not None

    def test_19_metrics_for_unassigned_container_is_forbidden(self, app_client):
        """A user cannot poll metrics for a container they were not granted."""
        result = app_client.simulate_get(
            "/api/metrics/latest",
            params={"container": STATE["big_container_id"]},
            headers=STATE["user_headers"],
        )
        assert result.status_code == 403

    def test_20_history_window_maps_to_the_right_resolution(self, app_client):
        """Each window returns its pre-aggregated resolution (7.9).

        A 24h or 7d chart must never be served from raw 10-second points —
        that would push 8,640 points at the browser.
        """
        expected = {"1h": "raw", "24h": "5m", "7d": "1h"}
        for window, resolution in expected.items():
            result = app_client.simulate_get(
                f"/api/containers/{STATE['container_id']}/history",
                params={"window": window},
                headers=STATE["user_headers"],
            )
            assert result.status_code == 200
            assert result.json["resolution"] == resolution

        bad = app_client.simulate_get(
            f"/api/containers/{STATE['container_id']}/history",
            params={"window": "30d"},
            headers=STATE["user_headers"],
        )
        assert bad.status_code == 400

    # ---------------------------------------------------------------- step 6
    def test_21_user_can_exec_in_their_assigned_container(
        self, app_client, fake_lxd
    ):
        """A valid argv array runs and returns stdout/stderr/exit code."""
        fake_lxd.containers._store[
            STATE["container_name"]
        ].exec_result.stdout = "hello from terminal\n"

        result = app_client.simulate_post(
            f"/api/containers/{STATE['container_id']}/exec",
            headers=STATE["user_headers"],
            json={"command": ["echo", "hello from terminal"]},
        )
        assert result.status_code == 200
        assert "hello from terminal" in result.json["stdout"]

        # The argv reached LXD unmodified as a list — never a joined string.
        executed = fake_lxd.containers._store[STATE["container_name"]].executed
        assert executed[-1] == ["echo", "hello from terminal"]

    def test_22_string_command_is_refused_before_execution(
        self, app_client, fake_lxd
    ):
        """A shell-style string is rejected with 400 and nothing runs (7.7)."""
        container = fake_lxd.containers._store[STATE["container_name"]]
        before = len(container.executed)

        result = app_client.simulate_post(
            f"/api/containers/{STATE['container_id']}/exec",
            headers=STATE["user_headers"],
            json={"command": "echo hacked; rm -rf /"},
        )
        assert result.status_code == 400
        assert len(container.executed) == before, (
            "a string command reached LXD — the injection defense failed"
        )

    def test_23_revoked_access_blocks_the_very_next_exec(
        self, app_client, fake_lxd
    ):
        """Revocation takes effect immediately — no cached authorization.

        require_container_access re-checks on every call precisely so a
        mid-session revoke cannot be outlived by an in-flight session.
        """
        # Works before revocation.
        ok = app_client.simulate_post(
            f"/api/containers/{STATE['container_id']}/exec",
            headers=STATE["user_headers"],
            json={"command": ["whoami"]},
        )
        assert ok.status_code == 200

        revoke = app_client.simulate_delete(
            f"/api/users/{STATE['user_id']}/containers/{STATE['container_id']}",
            headers=STATE["admin_headers"],
        )
        assert revoke.status_code == 200

        # The same cookie, one call later, is now refused.
        denied = app_client.simulate_post(
            f"/api/containers/{STATE['container_id']}/exec",
            headers=STATE["user_headers"],
            json={"command": ["whoami"]},
        )
        assert denied.status_code == 403, (
            "access was still granted after revocation — the check is cached"
        )

        # The assignment row survives as history rather than being deleted.
        conn = repo.get_connection()
        try:
            row = conn.execute(
                "SELECT active FROM assignments WHERE user_id = ? AND "
                "container_id = ?",
                (STATE["user_id"], STATE["container_id"]),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, "revoke hard-deleted the assignment row"
        assert row["active"] == 0

        # Re-grant so later steps still have an assigned user.
        app_client.simulate_post(
            f"/api/users/{STATE['user_id']}/containers/{STATE['container_id']}",
            headers=STATE["admin_headers"],
        )

    # ---------------------------------------------------------------- step 7
    def test_24_admin_changes_container_state(self, app_client, fake_lxd):
        """Each state action reaches LXD and is written to the audit log."""
        container = fake_lxd.containers._store[STATE["container_name"]]
        for action, expected_status in [
            ("start", "Running"), ("freeze", "Frozen"),
            ("unfreeze", "Running"), ("restart", "Running"),
            ("stop", "Stopped"),
        ]:
            result = app_client.simulate_patch(
                f"/api/containers/{STATE['container_id']}",
                headers=STATE["admin_headers"],
                json={"action": action},
            )
            assert result.status_code == 200, f"{action}: {result.json}"
            assert container.status == expected_status
            assert action in container.calls

        bad = app_client.simulate_patch(
            f"/api/containers/{STATE['container_id']}",
            headers=STATE["admin_headers"],
            json={"action": "selfdestruct"},
        )
        assert bad.status_code == 400

    def test_25_limit_change_updates_lxd_and_the_db_cache(
        self, app_client, fake_lxd
    ):
        """A limit change writes through to LXD and the DB stays in sync.

        Drift here would silently corrupt quota math, which reads the DB
        cache rather than calling LXD.
        """
        result = app_client.simulate_patch(
            f"/api/containers/{STATE['container_id']}",
            headers=STATE["admin_headers"],
            json={"limits": {"ram_mb": 512, "cpu": 0.5, "disk_gb": 5}},
        )
        assert result.status_code == 200

        container = fake_lxd.containers._store[STATE["container_name"]]
        assert container.config["limits.memory"] == "512MB"
        assert container.saved is True

        record = repo.get_container_by_id(STATE["container_id"])
        assert record["limit_ram_mb"] == 512
        assert record["limit_cpu"] == 0.5

    def test_26_patch_rejects_ambiguous_and_empty_bodies(self, app_client):
        """Both action+limits together and neither are 400."""
        both = app_client.simulate_patch(
            f"/api/containers/{STATE['container_id']}",
            headers=STATE["admin_headers"],
            json={"action": "start", "limits": {"ram_mb": 256}},
        )
        assert both.status_code == 400

        neither = app_client.simulate_patch(
            f"/api/containers/{STATE['container_id']}",
            headers=STATE["admin_headers"],
            json={},
        )
        assert neither.status_code == 400

    # ---------------------------------------------------------------- step 8
    def test_27_accounting_reports_host_and_per_user_totals(self, app_client):
        """Accounting separates host capacity, allocation, and per-user usage."""
        result = app_client.simulate_get(
            "/api/accounting", headers=STATE["admin_headers"]
        )
        assert result.status_code == 200
        body = result.json

        # Host capacity comes from the fake daemon's resources block:
        # 1 socket x 4 cores x 2 threads = 8, 16GiB RAM, 500GiB pool.
        assert body["host"]["cpu_cores"] == 8.0
        assert body["host"]["ram_total_mb"] == pytest.approx(16384.0)
        assert body["host"]["disk_total_gb"] == pytest.approx(500.0)
        assert body["stale"] is False

        # Allocation is summed from the containers table: 512 + 1536.
        assert body["allocated"]["ram_mb"] == 2048
        assert body["allocated"]["container_count"] == 2

        user_row = next(u for u in body["users"] if u["email"] == USER_EMAIL)
        assert user_row["allocation"]["ram_mb"] == 512
        assert user_row["quota"]["ram_mb"] == 2048
        assert user_row["container_count"] == 1

    def test_28_accounting_degrades_when_lxd_is_down(self, app_client, fake_lxd):
        """With LXD down, host is null and stale=True but the table still renders.

        Decision 7.10: an admin should get a banner over working data, not an
        error page — the allocation figures come from SQLite and are current.
        """
        fake_lxd.set_down(True)
        result = app_client.simulate_get(
            "/api/accounting", headers=STATE["admin_headers"]
        )
        fake_lxd.set_down(False)

        assert result.status_code == 200
        assert result.json["host"] is None
        assert result.json["stale"] is True
        assert result.json["host_error"]
        # The DB-derived half is unaffected by the outage.
        assert result.json["allocated"]["ram_mb"] == 2048
        assert len(result.json["users"]) >= 2

    # ---------------------------------------------------------------- step 9
    def test_29_delete_is_soft_and_preserves_history(self, app_client, fake_lxd):
        """Delete removes it from LXD but keeps the row and its metrics (7.8)."""
        result = app_client.simulate_delete(
            f"/api/containers/{STATE['container_id']}",
            headers=STATE["admin_headers"],
        )
        assert result.status_code == 200

        # Gone from LXD.
        assert STATE["container_name"] not in fake_lxd.containers._store

        # Row survives, flagged deleted.
        record = repo.get_container_by_id(STATE["container_id"])
        assert record is not None, "container row was hard-deleted"
        assert record["deleted_at"] is not None

        # Metric history keyed by the UUID is still queryable.
        assert store.get_latest_point(STATE["container_id"]) is not None

        # And it no longer appears in the active listing.
        listing = app_client.simulate_get(
            "/api/containers", headers=STATE["admin_headers"]
        )
        ids = [c["id"] for c in listing.json["containers"]]
        assert STATE["container_id"] not in ids

    def test_30_deleting_twice_returns_410(self, app_client):
        """A second delete is 410 Gone, not a confusing 500."""
        result = app_client.simulate_delete(
            f"/api/containers/{STATE['container_id']}",
            headers=STATE["admin_headers"],
        )
        assert result.status_code == 410

    def test_31_soft_deleted_container_drops_out_of_quota_math(self, app_client):
        """A deleted container stops counting against its user's allocation.

        The assignment row is still active=1, so the quota code must filter on
        the container's deleted_at — otherwise a user stays charged forever
        for containers that no longer exist.
        """
        result = app_client.simulate_get(
            "/api/accounting", headers=STATE["admin_headers"]
        )
        user_row = next(
            u for u in result.json["users"] if u["email"] == USER_EMAIL
        )
        assert user_row["allocation"]["ram_mb"] == 0
        assert user_row["container_count"] == 0

    # --------------------------------------------------------------- step 10
    def test_32_every_mutation_left_an_audit_trail(self):
        """The audit log captured each destructive and limit-changing action."""
        conn = repo.get_connection()
        try:
            actions = [
                r["action"]
                for r in conn.execute(
                    "SELECT action FROM audit_log ORDER BY created_at"
                ).fetchall()
            ]
        finally:
            conn.close()

        for required in [
            "auth.bootstrap_admin", "user.invite", "container.create",
            "assignment.grant", "assignment.revoke", "container.exec",
            "container.start", "container.stop", "container.limits",
            "container.delete",
        ]:
            assert required in actions, f"no audit entry for {required}"

    def test_33_audit_log_records_the_exact_command_run(self):
        """Terminal entries store the argv, so an admin can see what ran."""
        conn = repo.get_connection()
        try:
            rows = conn.execute(
                "SELECT detail FROM audit_log WHERE action = 'container.exec'"
            ).fetchall()
        finally:
            conn.close()
        details = " ".join(r["detail"] for r in rows)
        assert "echo hello from terminal" in details
        # The refused string command must NOT appear — it never ran.
        assert "rm -rf /" not in details

    def test_34_logout_revokes_the_session_server_side(self, app_client):
        """Logout deletes the session row, not just the cookie (7.1)."""
        conn = repo.get_connection()
        try:
            before = conn.execute(
                "SELECT COUNT(*) c FROM sessions WHERE user_id = ?",
                (STATE["admin_id"],),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert before >= 1

        result = app_client.simulate_post(
            "/api/auth/logout",
            headers={
                "Cookie": f"refresh_token={STATE['admin_refresh']}"
            },
        )
        assert result.status_code == 200

        conn = repo.get_connection()
        try:
            after = conn.execute(
                "SELECT COUNT(*) c FROM sessions WHERE user_id = ?",
                (STATE["admin_id"],),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert after == before - 1, (
            "logout did not delete the server-side session row"
        )

    def test_35_revoked_user_cannot_sign_back_in(self, app_client, monkeypatch):
        """A revoked user is refused at the OAuth callback."""
        repo.update_user(STATE["user_id"], status="revoked")

        monkeypatch.setattr(
            auth_module, "exchange_code_for_tokens",
            lambda code: {"id_token": "fake"},
        )
        monkeypatch.setattr(
            auth_module, "verify_and_decode_id_token",
            lambda tok: {"email": USER_EMAIL},
        )

        result = app_client.simulate_get(
            "/api/auth/google/callback", params={"code": "z"}
        )
        assert result.status_code == 403
        # Still revoked — a failed sign-in must not reactivate the account.
        assert repo.get_user_by_id(STATE["user_id"])["status"] == "revoked"
