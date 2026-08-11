"""
Database invariants that the rest of the system quietly assumes.

Every layer above SQLite takes certain things on faith: that a container's
UUID never changes, that a revoked assignment leaves a trace, that the audit
log cannot be edited, that two users cannot share an email. Those guarantees
come from schema constraints and soft-delete conventions rather than from any
single code path, so nothing above tests them directly.

This file tests them as properties, and then checks that concurrent access
does not violate them either — SQLite allows exactly one writer, and the
quota check is a read-then-write, so both are worth pinning down.
"""

import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from falcon import testing

from backend.app import create_app
from backend.db import repo

from .conftest import make_user

pytestmark = pytest.mark.usefixtures("isolated_db", "isolated_tsdb")


@pytest.fixture(scope="module")
def db_env(isolated_db, isolated_tsdb, fake_lxd):
    """An admin and a container to hang integrity assertions off."""
    client = testing.TestClient(create_app())
    admin_id, admin_headers = make_user("int-admin@example.com", role="admin")
    fake_lxd.add("int-box")
    container_id = repo.create_container_record(
        lxd_name="int-box", image="ubuntu:22.04", created_by=admin_id,
        limit_ram_mb=512, limit_cpu=1.0, limit_disk_gb=5,
    )
    return {
        "client": client, "admin": admin_headers, "admin_id": admin_id,
        "container_id": container_id, "lxd": fake_lxd,
    }


class TestSchemaConstraints:
    """The guarantees the schema itself is responsible for."""

    def test_foreign_keys_are_actually_enforced(self, db_env):
        """PRAGMA foreign_keys must be ON for every connection.

        SQLite defaults this to OFF, and when it is off, REFERENCES clauses
        are parsed and then silently ignored. A connection helper that forgot
        the pragma would leave the schema looking correct while enforcing
        nothing — so this asserts on behaviour, not on the pragma value.
        """
        conn = repo.get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO assignments (id, user_id, container_id, "
                    "active, created_at) VALUES (?, ?, ?, 1, ?)",
                    (str(uuid.uuid4()), "no-such-user",
                     db_env["container_id"], "2026-08-10T00:00:00Z"),
                )
                conn.commit()
        finally:
            conn.close()

    def test_duplicate_email_is_rejected(self, db_env):
        """Email is the identity key from Google, so it must be unique."""
        repo.create_user(
            email="int-dup@example.com", role="user", status="active",
            quota_ram_mb=1024, quota_cpu=1.0, quota_disk_gb=10,
        )
        with pytest.raises(sqlite3.IntegrityError):
            repo.create_user(
                email="int-dup@example.com", role="user", status="active",
                quota_ram_mb=1024, quota_cpu=1.0, quota_disk_gb=10,
            )

    def test_duplicate_lxd_name_is_rejected(self, db_env):
        """Two DB rows pointing at one LXD container would corrupt accounting."""
        with pytest.raises(sqlite3.IntegrityError):
            repo.create_container_record(
                lxd_name="int-box", image="ubuntu:22.04",
                created_by=db_env["admin_id"],
                limit_ram_mb=256, limit_cpu=0.5, limit_disk_gb=2,
            )

    @pytest.mark.parametrize("role", ["superuser", "root", "", "ADMIN"])
    def test_role_check_constraint_rejects_unknown_roles(self, db_env, role):
        """Only 'admin' and 'user' exist; the CHECK is the last line of defense.

        Authorization branches on `role == "admin"`, so a third role would
        fall through to the user branch everywhere — silently, and
        differently in each endpoint.
        """
        with pytest.raises(sqlite3.IntegrityError):
            repo.create_user(
                email=f"int-role-{role or 'blank'}@example.com", role=role,
                status="active", quota_ram_mb=0, quota_cpu=0.0,
                quota_disk_gb=0,
            )

    @pytest.mark.parametrize("status", ["deleted", "banned", "", "Active"])
    def test_status_check_constraint_rejects_unknown_statuses(
        self, db_env, status
    ):
        """Sign-in branches on status, so an unknown value is a security hole."""
        with pytest.raises(sqlite3.IntegrityError):
            repo.create_user(
                email=f"int-status-{status or 'blank'}@example.com",
                role="user", status=status, quota_ram_mb=0, quota_cpu=0.0,
                quota_disk_gb=0,
            )

    def test_update_user_rejects_non_whitelisted_columns(self, db_env):
        """The dynamic SET clause must refuse any column not on the whitelist.

        update_user interpolates field names into SQL, so the whitelist is
        what keeps that from being an injection point.
        """
        user_id = repo.create_user(
            email="int-whitelist@example.com", role="user", status="active",
            quota_ram_mb=1024, quota_cpu=1.0, quota_disk_gb=10,
        )
        for bad_field in ["id", "email", "created_at",
                          "role = 'admin', status"]:
            with pytest.raises(ValueError):
                repo.update_user(user_id, **{bad_field: "x"})


class TestSoftDeleteInvariants:
    """Soft delete has to hide rows without destroying history."""

    def test_uuid_survives_an_lxd_rename(self, db_env):
        """The primary key is ours, so renaming in LXD must not orphan metrics.

        Metrics and audit rows key off this UUID. If the identity were the
        LXD name, a rename would sever every historical record from its
        container — which is exactly why the schema keys on a UUID we mint.
        """
        cid = db_env["container_id"]
        before = repo.get_container_by_id(cid)

        repo.rename_container_lxd_name(cid, "int-box-renamed")

        after = repo.get_container_by_id(cid)
        assert after["id"] == before["id"], "the UUID changed on rename"
        assert after["lxd_name"] == "int-box-renamed"
        assert repo.get_container_by_lxd_name("int-box") is None
        assert repo.get_container_by_lxd_name("int-box-renamed")["id"] == cid

        repo.rename_container_lxd_name(cid, "int-box")

    def test_soft_deleted_container_keeps_its_row_and_assignments(self, db_env):
        """Delete hides the container without cascading away the history."""
        user_id, _ = make_user("int-softdel@example.com")
        cid = repo.create_container_record(
            lxd_name="int-softdel-box", image="ubuntu:22.04",
            created_by=db_env["admin_id"], limit_ram_mb=256,
            limit_cpu=0.5, limit_disk_gb=2,
        )
        repo.assign_container(user_id, cid)
        repo.soft_delete_container(cid)

        record = repo.get_container_by_id(cid)
        assert record is not None, "the row was hard-deleted"
        assert record["deleted_at"] is not None

        conn = repo.get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM assignments WHERE container_id = ?", (cid,)
            ).fetchall()
        finally:
            conn.close()
        assert rows, "assignment history was destroyed with the container"

    def test_soft_deleted_containers_leave_the_active_listing(self, db_env):
        """list_active_containers is the only view most callers use."""
        active_ids = {c["id"] for c in repo.list_active_containers()}
        cid = repo.create_container_record(
            lxd_name="int-vanish-box", image="ubuntu:22.04",
            created_by=db_env["admin_id"], limit_ram_mb=256,
            limit_cpu=0.5, limit_disk_gb=2,
        )
        assert cid in {c["id"] for c in repo.list_active_containers()}

        repo.soft_delete_container(cid)
        assert cid not in {c["id"] for c in repo.list_active_containers()}

    def test_revoked_assignment_stops_granting_access(self, db_env):
        """user_has_access must read the active flag, not just row existence."""
        user_id, _ = make_user("int-revoke@example.com")
        cid = repo.create_container_record(
            lxd_name="int-revoke-box", image="ubuntu:22.04",
            created_by=db_env["admin_id"], limit_ram_mb=256,
            limit_cpu=0.5, limit_disk_gb=2,
        )
        repo.assign_container(user_id, cid)
        assert repo.user_has_access(user_id, cid) is True

        repo.revoke_assignment(user_id, cid)
        assert repo.user_has_access(user_id, cid) is False

    def test_regranting_after_revoke_restores_access(self, db_env):
        """Revoke then grant must work — a UNIQUE index would break this."""
        user_id, _ = make_user("int-regrant@example.com")
        cid = repo.create_container_record(
            lxd_name="int-regrant-box", image="ubuntu:22.04",
            created_by=db_env["admin_id"], limit_ram_mb=256,
            limit_cpu=0.5, limit_disk_gb=2,
        )
        repo.assign_container(user_id, cid)
        repo.revoke_assignment(user_id, cid)
        repo.assign_container(user_id, cid)

        assert repo.user_has_access(user_id, cid) is True


class TestAuditLogIntegrity:
    """The audit log is the record of last resort."""

    def test_audit_entries_reference_a_real_user(self, db_env):
        """user_id is a foreign key, so entries cannot be forged for ghosts."""
        with pytest.raises(sqlite3.IntegrityError):
            repo.write_audit_log(
                user_id="nonexistent-user", action="container.delete",
                target="whatever", detail="forged",
            )

    def test_audit_entries_survive_the_deletion_of_their_target(self, db_env):
        """Deleting a container must not erase the record of who deleted it.

        This is the whole reason delete is soft: a CASCADE here would let
        someone destroy the evidence by destroying the container.
        """
        cid = repo.create_container_record(
            lxd_name="int-audit-box", image="ubuntu:22.04",
            created_by=db_env["admin_id"], limit_ram_mb=256,
            limit_cpu=0.5, limit_disk_gb=2,
        )
        repo.write_audit_log(
            user_id=db_env["admin_id"], action="container.delete",
            target=cid, detail="Deleted container 'int-audit-box'",
        )
        repo.soft_delete_container(cid)

        conn = repo.get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE target = ?", (cid,)
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0]["detail"] == "Deleted container 'int-audit-box'"

    def test_audit_entries_are_timestamped_and_ordered(self, db_env):
        """Entries carry a timestamp, so a trail can be reconstructed."""
        for i in range(3):
            repo.write_audit_log(
                user_id=db_env["admin_id"], action="test.sequence",
                target=None, detail=f"entry {i}",
            )
        conn = repo.get_connection()
        try:
            rows = conn.execute(
                "SELECT detail, created_at FROM audit_log "
                "WHERE action = 'test.sequence' ORDER BY created_at"
            ).fetchall()
        finally:
            conn.close()
        assert [r["detail"] for r in rows] == [f"entry {i}" for i in range(3)]
        assert all(r["created_at"] for r in rows)


class TestConcurrency:
    """SQLite has one writer; the API must not corrupt or deadlock under load."""

    def test_concurrent_reads_are_consistent(self, db_env):
        """Parallel GETs must all succeed and agree.

        SQLite serialises writers but allows concurrent readers. A missing
        connection close or a shared cursor would show up here as an
        exception or as disagreeing responses.
        """
        def fetch():
            return db_env["client"].simulate_get(
                "/api/containers", headers=db_env["admin"]
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = [f.result() for f in [pool.submit(fetch) for _ in range(24)]]

        assert all(r.status_code == 200 for r in results), (
            f"status codes: {sorted({r.status_code for r in results})}"
        )
        counts = {len(r.json["containers"]) for r in results}
        assert len(counts) == 1, (
            f"concurrent reads disagreed on the container count: {counts}"
        )

    def test_concurrent_writes_do_not_lose_rows(self, db_env):
        """Twelve parallel invites must produce exactly twelve users.

        A lost row means a write was silently dropped; an exception means the
        'database is locked' case is unhandled.
        """
        before = len(repo.list_users())

        def invite(i):
            return db_env["client"].simulate_post(
                "/api/users", headers=db_env["admin"],
                json={"email": f"int-concurrent-{i}@example.com",
                      "role": "user", "quota_ram_mb": 512,
                      "quota_cpu": 1.0, "quota_disk_gb": 5},
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = [f.result() for f in
                       [pool.submit(invite, i) for i in range(12)]]

        created = [r for r in results if r.status_code == 201]
        assert len(created) == 12, (
            f"only {len(created)}/12 concurrent invites succeeded; "
            f"codes: {[r.status_code for r in results]}"
        )
        assert len(repo.list_users()) == before + 12

    def test_concurrent_duplicate_invites_produce_exactly_one_user(self, db_env):
        """A race on the same email must resolve to one row, not two.

        The check-then-insert in the invite handler is not atomic, so the
        UNIQUE constraint is what actually prevents the duplicate. This
        confirms the loser of the race gets an error rather than a second row.
        """
        email = "int-race@example.com"

        def invite():
            return db_env["client"].simulate_post(
                "/api/users", headers=db_env["admin"],
                json={"email": email, "role": "user", "quota_ram_mb": 512,
                      "quota_cpu": 1.0, "quota_disk_gb": 5},
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = [f.result() for f in [pool.submit(invite) for _ in range(6)]]

        conn = repo.get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) c FROM users WHERE email = ?", (email,)
            ).fetchone()["c"]
        finally:
            conn.close()

        assert count == 1, f"{count} rows created for one email"
        assert sum(r.status_code == 201 for r in results) == 1, (
            "more than one caller was told it created the user"
        )
        # What the losers are *told* is asserted separately below — it is
        # currently wrong, and folding it in here would stop these two
        # invariants (which do hold) from being checked at all.

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "FINDING: the loser of a duplicate-invite race gets a 500. "
            "users.py:68 checks get_user_by_email and users.py:85 inserts, "
            "with no transaction between them, so under concurrency the "
            "UNIQUE constraint fires as a bare sqlite3.IntegrityError and "
            "Falcon renders it as a 500. The row count stays correct, but "
            "the caller cannot tell 'already invited' from 'the server is "
            "broken' and a retry loop would hammer it. Wrap create_user in "
            "try/except sqlite3.IntegrityError and raise HTTPConflict."
        ),
    )
    def test_duplicate_invite_race_reports_conflict_not_server_error(
        self, db_env
    ):
        """The losers of the race must get a clean 4xx, not a leaked 500."""
        email = "int-race-status@example.com"

        def invite():
            return db_env["client"].simulate_post(
                "/api/users", headers=db_env["admin"],
                json={"email": email, "role": "user", "quota_ram_mb": 512,
                      "quota_cpu": 1.0, "quota_disk_gb": 5},
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = [f.result() for f in [pool.submit(invite) for _ in range(6)]]

        assert all(r.status_code < 500 for r in results), (
            f"a duplicate invite surfaced as a 5xx: "
            f"{[r.status_code for r in results]}"
        )

    @pytest.fixture(scope="class")
    @staticmethod
    def quota_race(db_env):
        """Four 512MB grants fired at once against a 1024MB quota.

        Each grant fits on its own, so any two of them busting the quota is
        purely a function of the race. Run once and asserted from two angles
        below: whether it crashed, and whether it over-allocated.

        The interleaving is forced rather than hoped for. Firing four threads
        and trusting the OS scheduler reproduced the race about nine runs in
        ten; the tenth serialised by luck, the quota held, and the strict xfail
        below turned that into a suite failure. A test that reports a defect
        90% of the time is not evidence of anything.

        So `check_quota` is wrapped with a barrier that releases only once all
        four callers have finished their read. That is precisely the
        interleaving the defect needs — every thread reads the pre-grant total,
        every thread concludes it fits — and it makes the outcome deterministic
        without touching production code. The barrier carries a timeout so a
        future fix that serialises the grants (which is the point of B1) fails
        the test rather than hanging the suite.
        """
        user_id, _ = make_user("int-quota-race@example.com",
                               ram=1024, cpu=8.0, disk=80)
        container_ids = []
        for i in range(4):
            name = f"int-race-box-{i}"
            db_env["lxd"].add(name)
            container_ids.append(repo.create_container_record(
                lxd_name=name, image="ubuntu:22.04",
                created_by=db_env["admin_id"], limit_ram_mb=512,
                limit_cpu=0.5, limit_disk_gb=5,
            ))

        from backend.resources import assignments as assignments_module

        real_check_quota = assignments_module.check_quota
        barrier = threading.Barrier(4)
        timed_out = threading.Event()

        def check_quota_then_wait(*args, **kwargs):
            result = real_check_quota(*args, **kwargs)
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                # Fewer than four callers reached the check: the grants are
                # being serialised upstream, so the race cannot be staged.
                timed_out.set()
            return result

        def grant(cid):
            return db_env["client"].simulate_post(
                f"/api/users/{user_id}/containers/{cid}",
                headers=db_env["admin"],
            )

        patch = pytest.MonkeyPatch()
        patch.setattr(assignments_module, "check_quota", check_quota_then_wait)
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = [f.result() for f in
                           [pool.submit(grant, c) for c in container_ids]]
        finally:
            patch.undo()

        from backend.lxd.quota import compute_user_allocation
        return {
            "results": results,
            "granted": sum(r.status_code == 201 for r in results),
            "final_ram": compute_user_allocation(user_id)["ram_mb"],
            "staged": not timed_out.is_set(),
        }

    def test_racing_grants_never_crash(self, quota_race):
        """Whatever the outcome, no caller may get a 5xx."""
        codes = [r.status_code for r in quota_race["results"]]
        assert all(c < 500 for c in codes), (
            f"a racing grant produced a 5xx: {codes}"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "FINDING: concurrent grants bust the quota. quota.py check_quota "
            "is a read-then-write with no transaction spanning the two "
            "halves, so four grants racing each read the same pre-grant "
            "total and all four conclude they fit: 2048MB ends up allocated "
            "against a 1024MB quota. The quota stops being an upper bound "
            "under exactly the load it exists to bound. Fix by holding a "
            "write transaction (BEGIN IMMEDIATE) across the read and the "
            "insert, so the grants serialise. Note the fixture forces the "
            "interleaving with a barrier at the quota check rather than "
            "relying on the scheduler — unforced, this reproduced about nine "
            "runs in ten, and the tenth XPASSed and failed the suite."
        ),
    )
    def test_concurrent_grants_cannot_exceed_quota(self, quota_race):
        """Parallel grants that individually fit must not collectively bust quota.

        With a 1024MB quota and four 512MB containers, at most two may be
        granted no matter how the requests interleave.
        """
        if not quota_race["staged"]:
            # The barrier broke, so the four grants never overlapped in the
            # quota check. Either they are serialised now (B1 fixed, and this
            # marker should go) or the fixture no longer stages what it thinks
            # it does. Both need a human, and neither is "the quota holds".
            pytest.fail(
                "the race could not be staged: fewer than four callers "
                "reached the quota check concurrently, so this run proves "
                "nothing about the quota under load"
            )
        assert quota_race["final_ram"] <= 1024, (
            f"quota busted under concurrency: {quota_race['granted']} grants "
            f"succeeded, leaving {quota_race['final_ram']}MB allocated "
            f"against a 1024MB quota"
        )
