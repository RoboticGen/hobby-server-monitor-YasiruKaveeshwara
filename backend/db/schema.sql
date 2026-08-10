-- ============================================================================
-- Hobby Server Monitor — SQLite Schema
-- ============================================================================
-- This file is executed once by backend/db/init_db.py on a fresh machine.
-- It creates all relational tables used by the application.
-- ============================================================================

-- Users table: stores every account (admin or container-user) that has
-- been invited or has signed in. Rows are never hard-deleted; revoked
-- users keep their row with status='revoked' so audit history stays intact.
CREATE TABLE users (
    id            TEXT PRIMARY KEY,      -- uuid
    email         TEXT UNIQUE NOT NULL,
    role          TEXT NOT NULL CHECK(role IN ('admin','user')),
    status        TEXT NOT NULL CHECK(status IN ('invited','active','revoked')),
    quota_ram_mb  INTEGER NOT NULL DEFAULT 0,
    quota_cpu     REAL    NOT NULL DEFAULT 0,
    quota_disk_gb INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

-- Sessions table: one row per active refresh token. The raw token is never
-- stored — only its SHA-256 hash — so a leaked database dump does not
-- hand out working sessions.
CREATE TABLE sessions (
    id                 TEXT PRIMARY KEY,   -- uuid
    user_id            TEXT NOT NULL REFERENCES users(id),
    refresh_token_hash TEXT NOT NULL,
    expires_at         TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

-- Containers table: our internal record of every LXD container managed
-- through this app. Keyed by a stable UUID we generate (not the LXD name,
-- which can change via rename). Soft-deleted via deleted_at so historical
-- metrics and audit entries remain queryable.
CREATE TABLE containers (
    id           TEXT PRIMARY KEY,        -- our uuid, stable across renames
    lxd_name     TEXT UNIQUE NOT NULL,    -- current LXD-side name
    image        TEXT NOT NULL,
    created_by   TEXT NOT NULL REFERENCES users(id),
    deleted_at   TEXT,                    -- soft delete, NULL = active
    created_at   TEXT NOT NULL
);

-- Assignments table: maps users to containers they are allowed to access.
-- Revoking sets active=0 rather than deleting the row, preserving the
-- grant/revoke history for audit purposes.
CREATE TABLE assignments (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id),
    container_id TEXT NOT NULL REFERENCES containers(id),
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
);

-- Audit log: immutable (insert-only) record of every destructive or
-- limit-changing action. Used by admins to trace who did what and when.
CREATE TABLE audit_log (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id),
    action       TEXT NOT NULL,           -- e.g. 'container.delete', 'terminal.exec'
    target       TEXT,                    -- container id / user id / null
    detail       TEXT,                    -- free-text, e.g. the exact command run
    created_at   TEXT NOT NULL
);
