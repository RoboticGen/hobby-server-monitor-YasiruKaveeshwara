"""
Idempotent database initialization script.

Creates the SQLite database file and applies the schema from schema.sql
if the file does not already exist. If it does exist, prints a message
and exits cleanly — this ensures running the script multiple times
(e.g., a reviewer setting up on a fresh machine, or a deploy script
that doesn't track whether init already ran) never accidentally wipes
existing data.

Usage:
    python -m backend.db.init_db
"""

import os
import sqlite3
from pathlib import Path


def init_db(db_path: str) -> None:
    """Create the SQLite database and apply the schema if it does not exist.

    This is deliberately idempotent: if the database file already exists,
    the function prints a notice and returns without modifying anything.
    This safety check matters because re-running the schema DDL against
    an existing database would either fail (tables already exist) or,
    worse, silently drop and recreate tables if the schema used
    IF NOT EXISTS / DROP IF EXISTS patterns — neither is acceptable
    for a database that may already contain real user data.

    Args:
        db_path: Filesystem path where the SQLite database file should
                 be created (e.g., './data/app.db').
    """
    if os.path.exists(db_path):
        print(f"[init_db] Database already exists at '{db_path}' — skipping.")
        return

    # Ensure the parent directory exists (e.g., ./data/)
    parent_dir = os.path.dirname(db_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    # Locate schema.sql relative to this file, not the working directory,
    # so the script works regardless of where it's invoked from.
    schema_path = Path(__file__).resolve().parent / "schema.sql"

    # Read the full DDL and execute it as a script (multiple statements)
    schema_sql = schema_path.read_text()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        print(f"[init_db] Database created at '{db_path}' with schema applied.")
    finally:
        conn.close()


if __name__ == "__main__":
    # When run as a script, read the database path from the central config
    # so it stays consistent with the rest of the application.
    from backend.config import config

    init_db(str(config.database_path))
