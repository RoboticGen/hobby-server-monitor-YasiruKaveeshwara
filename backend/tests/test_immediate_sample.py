"""
Immediate metric sampling — the cold-start path for live graphs.

A container that has just been created and started has no metric points, and
the live view cannot draw anything from zero points. Worse, CPU and network
are counters, so a *rate* needs two points to difference: at a 5-second
collector interval the graphs stay blank for up to 5 seconds for the gauges
and 10 for the rates, and the tiles read "Measuring…" the whole time with no
indication that anything is happening.

Two mechanisms close that window, and this module tests both:

1. The API takes one sample immediately after a state action that leaves the
   container running, so a container the user just started already has a point.
2. `GET /api/metrics/latest?fresh=1` takes one live sample, but only while the
   container is below the two-point floor.

The second is the one that needs guarding, because it is the single place where
a dashboard request can reach LXD — the design rule everywhere else is that
dashboard polling never touches the daemon. The gate is the stored point count
rather than the caller, so the tests below pin down that a client cannot use
`fresh=1` to generate LXD load, and that it cannot use it to bypass
authorization either.
"""

import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from falcon import testing
from tinyflux import Point, TinyFlux

import backend.tsdb.store as store_module
from backend.app import create_app
from backend.auth.jwt_utils import create_access_token
from backend.collector import sampler
from backend.db import repo
from backend.lxd import client as lxd_client
from backend.lxd.client import LXDUnavailableError

# A complete metrics dict in the shape get_container_state returns, so a fake
# never drifts from the real field layout the charts read.
_FAKE_STATE = {
    "cpu_usage_ns": 1_000_000_000.0,
    "ram_used_mb": 256.0,
    "ram_peak_mb": 300.0,
    "disk_used_mb": 1024.0,
    "net_rx_bytes": 5000.0,
    "net_tx_bytes": 2500.0,
    "pid_count": 17.0,
}


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture(autouse=True, scope="module")
def isolated_store():
    """Give this module its own TinyFlux file, as test_metrics.py does."""
    tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
    tmp.close()

    fresh = TinyFlux(tmp.name)
    store_module._store = fresh

    yield

    fresh.close()
    store_module._store = None
    os.remove(tmp.name)


@pytest.fixture
def spy_lxd(monkeypatch):
    """Count get_container_state calls and control whether they succeed.

    Patches the attribute on backend.lxd.client, which is where the sampler
    resolves it at call time, so both the endpoint and the state-action path
    go through this fake.
    """

    calls: list[str] = []
    state = {"down": False}

    def _fake_get_container_state(lxd_name: str) -> dict:
        calls.append(lxd_name)
        if state["down"]:
            raise LXDUnavailableError(f"simulated outage for {lxd_name}")
        return dict(_FAKE_STATE)

    monkeypatch.setattr(lxd_client, "get_container_state", _fake_get_container_state)

    class _Spy:
        """Handle on the recorded calls plus a switch to simulate an outage."""

        def __init__(self, recorded: list[str], flags: dict):
            self.calls = recorded
            self._flags = flags

        def set_down(self, value: bool = True) -> None:
            self._flags["down"] = value

    return _Spy(calls, state)


class _Fixture:
    """Shared admin/user/container setup for the endpoint tests."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.app = create_app()
        self.client = testing.TestClient(self.app)

        self.admin_id = repo.create_user(
            email=_unique_email("cold-admin"),
            role="admin",
            status="active",
            quota_ram_mb=8192,
            quota_cpu=8.0,
            quota_disk_gb=100,
        )
        self.admin_token = create_access_token(self.admin_id, "admin")

        self.outsider_id = repo.create_user(
            email=_unique_email("cold-outsider"),
            role="user",
            status="active",
            quota_ram_mb=1024,
            quota_cpu=1.0,
            quota_disk_gb=10,
        )
        self.outsider_token = create_access_token(self.outsider_id, "user")

        self.lxd_name = f"cold-c-{uuid.uuid4().hex[:8]}"
        self.container_id = repo.create_container_record(
            lxd_name=self.lxd_name,
            image="ubuntu:22.04",
            created_by=self.admin_id,
            limit_ram_mb=1024,
            limit_cpu=1.0,
            limit_disk_gb=10,
        )
        yield

    def _admin(self) -> dict:
        return {"Cookie": f"access_token={self.admin_token}"}

    def _outsider(self) -> dict:
        return {"Cookie": f"access_token={self.outsider_token}"}

    def _seed(self, count: int) -> None:
        """Insert `count` raw points for this container, oldest first."""
        store = store_module._store
        now = datetime.now(tz=timezone.utc)
        for i in range(count):
            store.insert(
                Point(
                    time=now - timedelta(seconds=(count - i) * 5),
                    tags={
                        "container_id": self.container_id,
                        "resolution": "raw",
                    },
                    fields=dict(_FAKE_STATE),
                )
            )

    def _latest(self, headers: dict, fresh: bool = False):
        url = f"/api/metrics/latest?container={self.container_id}"
        if fresh:
            url += "&fresh=1"
        return self.client.simulate_get(url, headers=headers)


# --------------------------------------------------------------------------
# The sampler itself
# --------------------------------------------------------------------------


class TestSampler:
    """One definition of "read LXD and store a raw point"."""

    def test_sample_writes_a_point_tagged_raw(self, spy_lxd):
        """Tags must match what retention and /history filter on."""
        container_id = str(uuid.uuid4())
        returned = sampler.sample_container(
            {"id": container_id, "lxd_name": "sampler-a"}
        )

        assert returned["ram_used_mb"] == 256.0
        stored = store_module.get_latest_point(container_id)
        assert stored is not None, "sample_container wrote no point"
        assert stored["resolution"] == "raw"
        assert stored["pid_count"] == 17.0

    def test_sample_propagates_lxd_failure(self, spy_lxd):
        """The collector loop needs the exception to log which container failed."""
        spy_lxd.set_down()
        with pytest.raises(LXDUnavailableError):
            sampler.sample_container({"id": "x", "lxd_name": "sampler-b"})

    def test_try_sample_swallows_failure_and_returns_none(self, spy_lxd):
        """On the request path a failed sample must never raise."""
        spy_lxd.set_down()
        assert (
            sampler.try_sample_container({"id": "x", "lxd_name": "sampler-c"}) is None
        )

    def test_try_sample_writes_nothing_when_lxd_is_down(self, spy_lxd):
        """A failed sample must not leave a partial or zeroed point behind."""
        spy_lxd.set_down()
        container_id = str(uuid.uuid4())
        sampler.try_sample_container({"id": container_id, "lxd_name": "d"})
        assert store_module.get_latest_point(container_id) is None

    def test_collector_uses_the_same_sampler(self):
        """The loop must not keep its own copy of the write path.

        If _collect_one stops delegating, the tag layout can drift between the
        collector's points and the API's, which retention would silently split.
        """
        import backend.collector.collector as collector_module

        assert collector_module.sampler is sampler


# --------------------------------------------------------------------------
# GET /api/metrics/latest?fresh=1
# --------------------------------------------------------------------------


class TestFreshParameterGate(_Fixture):
    """`fresh=1` may only reach LXD while the container cannot be charted."""

    def test_cold_container_gets_a_live_sample(self, spy_lxd):
        """Zero stored points: the request must come back with data.

        This is the reported bug — without the fresh path the response is
        {"point": null} and every tile renders "Measuring…" until the
        collector's next tick.
        """
        result = self._latest(self._admin(), fresh=True)

        assert result.status_code == 200
        assert result.json["point"] is not None
        assert result.json["point"]["ram_used_mb"] == 256.0
        assert spy_lxd.calls == [self.lxd_name]

    def test_the_live_sample_is_persisted_not_just_returned(self, spy_lxd):
        """The sample must land in the TSDB so it counts toward the floor.

        If it were only returned, every poll would re-sample and the client
        would never accumulate the two points a rate needs.
        """
        self._latest(self._admin(), fresh=True)
        assert store_module.get_latest_point(self.container_id) is not None

    def test_one_stored_point_is_still_below_the_floor(self, spy_lxd):
        """A gauge can render from one point, but a rate cannot."""
        self._seed(1)
        result = self._latest(self._admin(), fresh=True)

        assert result.status_code == 200
        assert len(spy_lxd.calls) == 1, "should have sampled to reach two points"

    def test_two_stored_points_stop_the_lxd_call(self, spy_lxd):
        """The gate: once chartable, fresh=1 is inert.

        This is what bounds the cost. Without it a page left open would put a
        permanent 1-per-poll load on the LXD daemon, which is the exact
        coupling the metrics endpoints exist to avoid.
        """
        self._seed(2)
        result = self._latest(self._admin(), fresh=True)

        assert result.status_code == 200
        assert result.json["point"] is not None
        assert spy_lxd.calls == [], "fresh=1 must not reach LXD when chartable"

    def test_repeated_fresh_requests_converge_and_stop_sampling(self, spy_lxd):
        """Hammering fresh=1 must sample at most twice, then stop for good."""
        for _ in range(10):
            assert self._latest(self._admin(), fresh=True).status_code == 200

        assert len(spy_lxd.calls) <= 2, (
            f"fresh=1 sampled {len(spy_lxd.calls)} times; the point-count gate "
            "should cap it at the two-point floor"
        )

    def test_without_the_parameter_lxd_is_never_touched(self, spy_lxd):
        """Default behaviour is unchanged: TSDB-only, even with no data."""
        result = self._latest(self._admin(), fresh=False)

        assert result.status_code == 200
        assert result.json["point"] is None
        assert spy_lxd.calls == []

    def test_fresh_does_not_bypass_authorization(self, spy_lxd):
        """A user without an assignment gets 403 and no sample is taken.

        The access check runs before the sampling branch; if that order were
        reversed, an unauthorized caller could still make the server hit LXD.
        """
        result = self._latest(self._outsider(), fresh=True)

        assert result.status_code == 403
        assert spy_lxd.calls == []

    def test_fresh_requires_authentication(self, spy_lxd):
        """No cookie: 401 before anything else, and no LXD call."""
        result = self.client.simulate_get(
            f"/api/metrics/latest?container={self.container_id}&fresh=1"
        )
        assert result.status_code == 401
        assert spy_lxd.calls == []

    def test_unknown_container_id_does_not_crash(self, spy_lxd):
        """An id with no DB row must not raise on the repo lookup."""
        missing = str(uuid.uuid4())
        result = self.client.simulate_get(
            f"/api/metrics/latest?container={missing}&fresh=1",
            headers=self._admin(),
        )
        assert result.status_code == 200
        assert result.json["point"] is None
        assert spy_lxd.calls == []

    def test_lxd_failure_degrades_to_empty_not_500(self, spy_lxd):
        """A stopped container raises inside LXD; that is not a server error."""
        spy_lxd.set_down()
        result = self._latest(self._admin(), fresh=True)

        assert result.status_code == 200
        assert result.json["point"] is None


# --------------------------------------------------------------------------
# Immediate sample after a state action
# --------------------------------------------------------------------------


class TestImmediateSampleOnStateAction(_Fixture):
    """Starting a container should leave a point behind straight away."""

    @pytest.fixture(autouse=True)
    def fake_state_change(self, monkeypatch):
        """Let PATCH succeed without a real daemon."""
        self.state_changes: list[tuple[str, str]] = []

        def _change(lxd_name: str, action: str) -> dict:
            self.state_changes.append((lxd_name, action))
            return {"status": "Running"}

        monkeypatch.setattr(lxd_client, "change_container_state", _change)
        yield

    def _patch(self, action: str):
        return self.client.simulate_patch(
            f"/api/containers/{self.container_id}",
            headers=self._admin(),
            json={"action": action},
        )

    @pytest.mark.parametrize("action", ["start", "restart", "unfreeze"])
    def test_running_actions_leave_a_point(self, action, spy_lxd):
        """After the action the live view has something to render immediately."""
        result = self._patch(action)

        assert result.status_code == 200
        assert store_module.get_latest_point(self.container_id) is not None
        assert spy_lxd.calls == [self.lxd_name]

    @pytest.mark.parametrize("action", ["stop", "freeze"])
    def test_non_running_actions_do_not_sample(self, action, spy_lxd):
        """Sampling something we just stopped would only log a skip."""
        result = self._patch(action)

        assert result.status_code == 200
        assert spy_lxd.calls == []

    def test_action_still_succeeds_when_the_sample_fails(self, spy_lxd):
        """A container that is slow to boot must not fail its own start.

        The sample is an optimisation. If a failure there could surface as a
        non-2xx, this feature would have made starting containers less
        reliable than before it existed.
        """
        spy_lxd.set_down()
        result = self._patch("start")

        assert result.status_code == 200
        assert result.json["action"] == "start"
        assert self.state_changes == [(self.lxd_name, "start")]
        assert store_module.get_latest_point(self.container_id) is None

    def test_the_sample_is_tagged_to_the_right_container(self, spy_lxd):
        """A point written under the wrong id would show on another graph."""
        self._patch("start")
        point = store_module.get_latest_point(self.container_id)
        assert point is not None
        assert point["container_id"] == self.container_id
        assert point["resolution"] == "raw"
