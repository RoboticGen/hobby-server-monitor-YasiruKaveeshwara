"""
Data access layer for the SQLite database.

All database queries live in this single module. Every query uses
parameterized placeholders (?), never string formatting, to prevent
SQL injection. Functions return plain dicts (not ORM objects or Row
instances) so callers have no dependency on sqlite3 internals.
"""

import sqlite3
import uuid
from datetime import datetime, timezone

from backend.config import config


# ---------------------------------------------------------------------------
# Allowed columns for update_user — whitelisted here so that the dynamic
# SET clause in update_user cannot be tricked into writing to arbitrary
# columns. Only these names are accepted; anything else raises ValueError.
# ---------------------------------------------------------------------------
_UPDATABLE_USER_FIELDS = {
    "role", "status", "quota_ram_mb", "quota_cpu", "quota_disk_gb",
}


def get_connection() -> sqlite3.Connection:
    """Open a connection to the application database with foreign keys enabled.

    Enabling foreign keys via PRAGMA is required on every connection in
    SQLite (it defaults to OFF), otherwise REFERENCES constraints are
    silently ignored and referential integrity is not enforced.
    """
    conn = sqlite3.connect(str(config.database_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row  # allows column access by name
    return conn


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
            (user_id, email, role, status, quota_ram_mb, quota_cpu,
             quota_disk_gb, _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    return user_id


def get_user_by_email(email: str) -> dict | None:
    """Look up a user by their email address, returning None if not found."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def get_user_by_id(user_id: str) -> dict | None:
    """Look up a user by their UUID, returning None if not found."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


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


def create_session(
    user_id: str, refresh_token_hash: str, expires_at: str
) -> str:
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
