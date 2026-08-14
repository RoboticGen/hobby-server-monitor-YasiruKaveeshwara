"""
Data access layer for the SQLite database.

All database queries live in this single module. Every query uses
parameterized placeholders (?), never string formatting, to prevent
SQL injection. Functions return plain dicts (not ORM objects or Row
instances) so callers have no dependency on sqlite3 internals.
"""

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from backend.config import config

# ---------------------------------------------------------------------------
# Allowed columns for update_user — whitelisted here so that the dynamic
# SET clause in update_user cannot be tricked into writing to arbitrary
# columns. Only these names are accepted; anything else raises ValueError.
# ---------------------------------------------------------------------------
_UPDATABLE_USER_FIELDS = {
    "role",
    "status",
    "quota_ram_mb",
    "quota_cpu",
    "quota_disk_gb",
}

# How long a writer waits for a competing write lock before giving up.
# Grants serialise on that lock (see transaction()), so this is also the
# ceiling on how long a queued request can sit behind another one.
_BUSY_TIMEOUT_SECONDS = 5.0


class DuplicateKeyError(Exception):
    """A UNIQUE constraint rejected the insert.

    Raised instead of letting sqlite3.IntegrityError escape, so callers can
    map "this already exists" to a 409 without importing sqlite3 or matching
    on driver error strings. Carries the column that collided.
    """

    def __init__(self, column: str, value: str):
        self.column = column
        self.value = value
        super().__init__(f"{column} '{value}' already exists")


def get_connection() -> sqlite3.Connection:
    """Open a connection to the application database with foreign keys enabled.

    Enabling foreign keys via PRAGMA is required on every connection in
    SQLite (it defaults to OFF), otherwise REFERENCES constraints are
    silently ignored and referential integrity is not enforced.
    """
    conn = sqlite3.connect(str(config.database_path), timeout=_BUSY_TIMEOUT_SECONDS)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row  # allows column access by name
    return conn


class WriteLockTimeout(Exception):
    """Could not acquire the write lock within the busy timeout.

    Distinct from a programming error: the database is fine, it was just
    busy for longer than a caller should wait. Handlers map this to 503 so
    the client retries rather than seeing a 500.
    """


@contextmanager
def transaction():
    """Yield a connection that holds a write lock for the whole block.

    `BEGIN IMMEDIATE` takes SQLite's RESERVED lock up front rather than on
    first write, so two callers running this block serialise instead of
    interleaving. That is what makes a read-then-write decision safe: check
    a quota and insert the row it authorised, and a second caller cannot
    read the pre-insert total, because it waits for the first to commit.

    Pass the yielded connection to the functions below via their `conn=`
    parameter so every statement lands in this one transaction. Anything
    raised inside the block rolls all of it back.
    """
    conn = get_connection()
    # Autocommit mode. Left at the default, sqlite3 opens its own implicit
    # transaction around the first write, which collides with the explicit
    # BEGIN below.
    conn.isolation_level = None
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            # No transaction was opened, so there is nothing to roll back —
            # attempting one here raises "cannot rollback - no transaction
            # is active" and buries the real cause.
            if "locked" in str(exc) or "busy" in str(exc):
                raise WriteLockTimeout(str(exc)) from exc
            raise

        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


@contextmanager
def _connection(existing: sqlite3.Connection | None):
    """Reuse the caller's connection, or open and close a private one.

    Functions that can take part in a caller's transaction accept an
    optional `conn`. Given one, they join that transaction and leave the
    commit to whoever opened it; given None, they keep the original
    behaviour of one short-lived connection per call.
    """
    if existing is not None:
        yield existing
        return
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    """Convert a sqlite3.Row to a plain dict, or return None if row is None."""
    if row is None:
        return None
    return dict(row)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string for storage."""
    return datetime.now(timezone.utc).isoformat()


# ============================= Users ========================================


def create_user(
    email: str,
    role: str,
    status: str,
    quota_ram_mb: int,
    quota_cpu: float,
    quota_disk_gb: int,
) -> str:
    """Insert a new user row and return its generated UUID.

    Called when an admin invites a user or when the bootstrap admin
    signs in for the first time.

    Raises DuplicateKeyError if the email is already taken. Callers cannot
    prevent that by checking first — two concurrent invites for one address
    both pass the check and only the UNIQUE constraint separates them — so
    the collision is reported as a normal outcome rather than an error.
    """
    user_id = str(uuid.uuid4())
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO users (id, email, role, status, quota_ram_mb,
                               quota_cpu, quota_disk_gb, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                email,
                role,
                status,
                quota_ram_mb,
                quota_cpu,
                quota_disk_gb,
                _now_iso(),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        # email is the only UNIQUE column on this table; anything else
        # failing the constraint is a genuine bug and should keep raising.
        if "users.email" not in str(exc):
            raise
        raise DuplicateKeyError("email", email) from exc
    finally:
        conn.close()
    return user_id


def get_user_by_email(email: str) -> dict | None:
    """Look up a user by their email address, returning None if not found."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def get_user_by_id(user_id: str, conn: sqlite3.Connection | None = None) -> dict | None:
    """Look up a user by their UUID, returning None if not found."""
    with _connection(conn) as db:
        row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _row_to_dict(row)


def list_users() -> list[dict]:
    """Return every user row as a list of dicts."""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_user(user_id: str, **fields) -> None:
    """Update one or more mutable fields on a user row.

    Accepts keyword arguments for any column in _UPDATABLE_USER_FIELDS
    (role, status, quota_ram_mb, quota_cpu, quota_disk_gb). Raises
    ValueError if an unrecognised field name is passed — this prevents
    callers from accidentally (or maliciously) writing to columns like
    'id' or 'email' that should never change after creation.
    """
    if not fields:
        return

    # Reject any field name not in the whitelist
    bad_fields = set(fields.keys()) - _UPDATABLE_USER_FIELDS
    if bad_fields:
        raise ValueError(
            f"Cannot update disallowed fields: {bad_fields}. "
            f"Allowed: {_UPDATABLE_USER_FIELDS}"
        )

    # Build SET clause from whitelisted names — the column names are safe
    # because they come from the hardcoded whitelist, only values are
    # parameterized.
    set_clause = ", ".join(f"{col} = ?" for col in fields)
    values = list(fields.values()) + [user_id]

    conn = get_connection()
    try:
        conn.execute(
            f"UPDATE users SET {set_clause} WHERE id = ?",
            values,
        )
        conn.commit()
    finally:
        conn.close()


# =========================== Sessions =======================================


def create_session(user_id: str, refresh_token_hash: str, expires_at: str) -> str:
    """Insert a new session row and return its generated UUID.

    Stores the hashed refresh token (never the raw token) so that a
    database leak does not compromise active sessions.
    """
    session_id = str(uuid.uuid4())
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO sessions (id, user_id, refresh_token_hash,
                                  expires_at, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, user_id, refresh_token_hash, expires_at, _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    return session_id


def get_session_by_hash(refresh_token_hash: str) -> dict | None:
    """Look up a session by the hash of its refresh token."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE refresh_token_hash = ?",
            (refresh_token_hash,),
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def delete_session(session_id: str) -> None:
    """Remove a session row, effectively revoking that refresh token.

    This is a hard delete (not a soft delete) because sessions have no
    audit value once revoked — the audit_log table captures the logout
    event itself if needed.
    """
    conn = get_connection()
    try:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
    finally:
        conn.close()


def delete_sessions_for_user(user_id: str) -> None:
    """Remove all session rows for a specific user ID."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


# =========================== Containers =====================================


def create_container_record(
    lxd_name: str,
    image: str,
    created_by: str,
    limit_ram_mb: int = 0,
    limit_cpu: float = 0.0,
    limit_disk_gb: int = 0,
) -> str:
    """Insert a new container row and return its generated UUID.

    This records the container in our database; the actual LXD container
    creation happens separately via lxd/client.py. The limit_* params
    are cached here so quota calculations can read them from the DB
    without needing a live LXD call.
    """
    container_id = str(uuid.uuid4())
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO containers (id, lxd_name, image, created_by,
                                    limit_ram_mb, limit_cpu, limit_disk_gb,
                                    created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                container_id,
                lxd_name,
                image,
                created_by,
                limit_ram_mb,
                limit_cpu,
                limit_disk_gb,
                _now_iso(),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        # Same shape as create_user: lxd_name is UNIQUE, and the caller's
        # name check cannot close the window before the insert.
        if "containers.lxd_name" not in str(exc):
            raise
        raise DuplicateKeyError("container name", lxd_name) from exc
    finally:
        conn.close()
    return container_id


def get_container_by_id(
    container_id: str, conn: sqlite3.Connection | None = None
) -> dict | None:
    """Look up a container by its internal UUID."""
    with _connection(conn) as db:
        row = db.execute(
            "SELECT * FROM containers WHERE id = ?", (container_id,)
        ).fetchone()
        return _row_to_dict(row)


def get_container_by_lxd_name(lxd_name: str) -> dict | None:
    """Look up a container by its current LXD-side name."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM containers WHERE lxd_name = ?", (lxd_name,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def list_active_containers() -> list[dict]:
    """Return all containers that have not been soft-deleted."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM containers WHERE deleted_at IS NULL " "ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_assignees(container_id: str) -> list[dict]:
    """Return the users a container is actively assigned to.

    Used for quota checks on limit increases: raising a container's limits
    must be checked against everyone it is assigned to, not just the admin
    who created it.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT u.* FROM users u
            JOIN assignments a ON a.user_id = u.id
            WHERE a.container_id = ? AND a.active = 1
            ORDER BY u.created_at
            """,
            (container_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def rename_container_lxd_name(container_id: str, new_lxd_name: str) -> None:
    """Update the LXD-side name on an existing container record.

    The internal UUID stays the same, so all assignments, metrics, and
    audit entries continue to reference the correct container.
    """
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE containers SET lxd_name = ? WHERE id = ?",
            (new_lxd_name, container_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_container_limits(
    container_id: str,
    limit_ram_mb: int,
    limit_cpu: float,
    limit_disk_gb: int,
) -> None:
    """Update the DB-cached resource limits for a container.

    Called after successfully updating the limits in LXD, to keep the
    DB cache in sync. The quota module reads these cached values.
    """
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE containers SET limit_ram_mb = ?, limit_cpu = ?, "
            "limit_disk_gb = ? WHERE id = ?",
            (limit_ram_mb, limit_cpu, limit_disk_gb, container_id),
        )
        conn.commit()
    finally:
        conn.close()


# soft_delete_container does NOT hard-delete the row. The container record
# must survive deletion so that:
#   1. Audit log entries referencing this container remain valid.
#   2. TinyFlux metric history keyed by this container's UUID stays
#      queryable for historical reporting.
#   3. Assignment rows (also soft-deactivated) retain their meaning.
def soft_delete_container(container_id: str) -> None:
    """Mark a container as deleted by setting its deleted_at timestamp.

    Does not remove the row — audit history and TinyFlux metric data
    reference this container's UUID and must remain queryable.
    """
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE containers SET deleted_at = ? WHERE id = ?",
            (_now_iso(), container_id),
        )
        conn.commit()
    finally:
        conn.close()


# =========================== Assignments ====================================


def assign_container(
    user_id: str, container_id: str, conn: sqlite3.Connection | None = None
) -> str:
    """Grant a user access to a container and return the assignment UUID."""
    assignment_id = str(uuid.uuid4())
    with _connection(conn) as db:
        db.execute(
            """
            INSERT INTO assignments (id, user_id, container_id, active,
                                     created_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            (assignment_id, user_id, container_id, _now_iso()),
        )
        if conn is None:
            db.commit()
    return assignment_id


# revoke_assignment sets active=0 rather than deleting the row. This
# preserves the full grant/revoke history for audit purposes — an admin
# can see that user X was granted access on date A and revoked on date B,
# rather than the row simply vanishing.
def revoke_assignment(user_id: str, container_id: str) -> None:
    """Revoke a user's access to a container by deactivating the assignment.

    Sets active=0 on the matching row rather than deleting it, so the
    grant/revoke history is preserved for auditing.
    """
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE assignments SET active = 0 "
            "WHERE user_id = ? AND container_id = ? AND active = 1",
            (user_id, container_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_assignments_for_user(
    user_id: str, conn: sqlite3.Connection | None = None
) -> list[dict]:
    """Return all active assignments for a given user."""
    with _connection(conn) as db:
        rows = db.execute(
            "SELECT * FROM assignments WHERE user_id = ? AND active = 1 "
            "ORDER BY created_at",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def user_has_access(
    user_id: str, container_id: str, conn: sqlite3.Connection | None = None
) -> bool:
    """Check whether a user has an active assignment to a container.

    Returns True only if there is at least one active assignment row
    linking this user to this container.
    """
    with _connection(conn) as db:
        row = db.execute(
            "SELECT 1 FROM assignments "
            "WHERE user_id = ? AND container_id = ? AND active = 1",
            (user_id, container_id),
        ).fetchone()
        return row is not None


# ============================= Audit Log ====================================


def write_audit_log(
    user_id: str,
    action: str,
    target: str | None,
    detail: str | None,
) -> None:
    """Append an entry to the immutable audit log.

    This table is insert-only — entries are never updated or deleted.
    Every destructive or limit-changing action should write here so
    admins have a full trail of who did what and when.
    """
    entry_id = str(uuid.uuid4())
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO audit_log (id, user_id, action, target, detail,
                                   created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (entry_id, user_id, action, target, detail, _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
