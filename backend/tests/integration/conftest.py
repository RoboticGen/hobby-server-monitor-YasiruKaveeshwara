"""
Shared fixtures for the integration suite.

Two things differ from the unit-test conftest:

1. **Isolation.** Each integration module gets its own SQLite file and its own
   TinyFlux file, created fresh and torn down after. The unit suite shares one
   database for the whole session, which is fine for asserting on rows you
   just inserted but wrong for integration tests that assert on *totals*
   ("the accounting endpoint reports 3 containers") — those must not see
   another module's leftovers.

2. **LXD is faked one level deeper.** Unit tests monkeypatch the wrapper
   functions in backend.lxd.client. Here we patch `_get_client` instead, so
   the wrapper's own conversion and error-handling code actually runs. See
   fake_lxd.py for why that distinction matters.
"""

import os
import tempfile

import pytest
from falcon import testing
from tinyflux import TinyFlux

import backend.lxd.client as lxd_client_module
import backend.tsdb.store as store_module
from backend.app import create_app
from backend.auth.jwt_utils import create_access_token
from backend.db import repo
from backend.db.init_db import init_db

from .fake_lxd import FakeLXDClient, LXDDown


@pytest.fixture(scope="module")
def isolated_db():
    """Point config.database_path at a fresh per-module SQLite file.

    config is a frozen dataclass, so the path is swapped with
    object.__setattr__ and restored afterwards — the alternative (reloading
    the config module) would rebind the `config` object that every other
    module already imported by reference.
    """
    from backend.config import config

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.remove(tmp.name)  # init_db wants to create it itself

    original = config.database_path
    object.__setattr__(config, "database_path", tmp.name)
    init_db(tmp.name)

    yield tmp.name

    object.__setattr__(config, "database_path", original)
    if os.path.exists(tmp.name):
        os.remove(tmp.name)


@pytest.fixture(scope="module")
def isolated_tsdb():
    """Replace the memoized TinyFlux instance with a fresh per-module file."""
    tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
    tmp.close()

    fresh = TinyFlux(tmp.name)
    original = store_module._store
    store_module._store = fresh

    yield fresh

    fresh.close()
    store_module._store = original
    if os.path.exists(tmp.name):
        os.remove(tmp.name)


@pytest.fixture(scope="module")
def fake_lxd():
    """Install a fake LXD daemon behind the real client wrapper.

    Module-scoped, matching isolated_db and isolated_tsdb: all three backing
    stores share one lifetime so an ordered narrative can create a container
    in one step and act on it in the next. A function-scoped daemon would
    hand each test an empty LXD and quietly break every cross-step assertion.

    Yields the FakeLXDClient so a test can seed containers, assert on the
    calls the wrapper made, or call set_down() to simulate an outage.
    """
    fake = FakeLXDClient()
    state = {"down": False}

    def _factory():
        if state["down"]:
            raise LXDDown("LXD daemon is not reachable (simulated)")
        return fake

    # pytest's monkeypatch fixture is function-scoped, so a module-scoped
    # fixture has to drive MonkeyPatch directly.
    mp = pytest.MonkeyPatch()
    mp.setattr(lxd_client_module, "_get_client", _factory)

    def _set_down(value: bool = True):
        state["down"] = value

    fake.set_down = _set_down
    yield fake
    mp.undo()


@pytest.fixture
def client(isolated_db):
    """A Falcon test client backed by the isolated database."""
    return testing.TestClient(create_app())


def make_user(email, role="user", status="active",
              ram=4096, cpu=4.0, disk=40) -> tuple[str, dict]:
    """Create a user and return (user_id, cookie-headers) for that user."""
    user_id = repo.create_user(
        email=email, role=role, status=status,
        quota_ram_mb=ram, quota_cpu=cpu, quota_disk_gb=disk,
    )
    token = create_access_token(user_id, role)
    return user_id, {"Cookie": f"access_token={token}"}
