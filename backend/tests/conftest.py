"""
Shared test fixtures for backend tests.

Centralizes DATABASE_PATH management so all test files use the same temp
database with schema applied. This prevents environment variable conflicts
when pytest runs multiple test files in a single process.
"""

import os
import tempfile

import pytest

# Create a temporary database file BEFORE any backend module is imported.
# This is at module level intentionally — it must run before pytest
# collects test modules that import backend code.
_TEMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TEMP_DB.close()
os.environ["DATABASE_PATH"] = _TEMP_DB.name


from backend.db.init_db import init_db


@pytest.fixture(autouse=True, scope="session")
def _shared_test_db():
    """Create the schema once for the entire test session.

    All test files share a single temp database so there's no conflict
    over which DATABASE_PATH is active. Each test class that needs data
    isolation should create its own users/containers within the shared DB.
    """
    # Remove any leftover file and create a fresh schema
    if os.path.exists(_TEMP_DB.name):
        os.remove(_TEMP_DB.name)
    init_db(_TEMP_DB.name)
    yield
    # Cleanup after the session
    if os.path.exists(_TEMP_DB.name):
        os.remove(_TEMP_DB.name)
