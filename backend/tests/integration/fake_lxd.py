"""
An in-memory stand-in for a pylxd Client.

The unit tests monkeypatch the wrapper functions in backend.lxd.client
(execute_command, get_container, ...) which means the wrapper's own logic —
unit conversion, the try/except that turns LXD errors into None, the
sockets->cores->threads walk — is never executed by them.

The integration suite instead replaces `_get_client`, one level deeper, so
every real line of backend/lxd/client.py runs against this double. Anything
client.py touches on a pylxd object is modelled here; anything it does not
touch is deliberately absent, so a future call to an unmodelled attribute
fails loudly in tests instead of silently passing.

Set `down=True` to simulate an unreachable LXD daemon: _get_client itself
raises, which is what actually happens when the unix socket is missing.
"""


class LXDDown(Exception):
    """Raised by the fake client factory when the daemon is 'unreachable'."""


class FakeExecResult:
    """Mirrors the object pylxd's container.execute() returns."""

    def __init__(self, exit_code: int, stdout: str, stderr: str):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _Usage:
    """A pylxd state sub-object that exposes a single `usage` counter."""

    def __init__(self, usage: float):
        self.usage = usage


class _Memory:
    def __init__(self, usage: float, usage_peak: float):
        self.usage = usage
        self.usage_peak = usage_peak


class _CPU:
    def __init__(self, usage: float):
        self.usage = usage


class FakeState:
    """Mirrors pylxd's container.state() return value."""

    def __init__(
        self,
        cpu_usage=1_500_000_000.0,
        ram_used=268_435_456.0,
        ram_peak=536_870_912.0,
        disk_used=1_073_741_824.0,
        rx=4096.0,
        tx=2048.0,
        processes=17,
    ):
        self.cpu = _CPU(cpu_usage)
        self.memory = _Memory(ram_used, ram_peak)
        self.disk = {"root": _Usage(disk_used)}
        self.network = {
            "eth0": {"counters": {"bytes_received": rx, "bytes_sent": tx}}
        }
        self.processes = processes


class FakeContainer:
    """An LXD container as seen through the small slice pylxd exposes."""

    def __init__(self, name, image="ubuntu:22.04", status="Running", config=None):
        self.name = name
        self.image = image
        self.status = status
        self.architecture = "x86_64"
        self.created_at = "2026-08-10T00:00:00Z"
        self.config = dict(config or {})
        self.saved = False
        # Recorded so tests can assert on real call ordering rather than
        # trusting that delete() happened to stop a running container first.
        self.calls: list[str] = []
        self.executed: list[list[str]] = []
        self.exec_result = FakeExecResult(0, "", "")
        self.state_obj = FakeState()
        self._client = None

    # --- state transitions -------------------------------------------------
    def _transition(self, action: str, new_status: str, wait=False):
        self.calls.append(action)
        self.status = new_status

    def start(self, wait=False):
        self._transition("start", "Running", wait)

    def stop(self, wait=False):
        self._transition("stop", "Stopped", wait)

    def restart(self, wait=False):
        self._transition("restart", "Running", wait)

    def freeze(self, wait=False):
        self._transition("freeze", "Frozen", wait)

    def unfreeze(self, wait=False):
        self._transition("unfreeze", "Running", wait)

    def delete(self, wait=False):
        self.calls.append("delete")
        if self._client is not None:
            self._client.containers._store.pop(self.name, None)

    def rename(self, new_name: str, wait=False):
        self.calls.append(f"rename:{new_name}")
        if self._client is not None:
            self._client.containers._store.pop(self.name, None)
            self._client.containers._store[new_name] = self
        self.name = new_name

    def save(self, wait=False):
        self.calls.append("save")
        self.saved = True

    def execute(self, command):
        self.executed.append(list(command))
        return self.exec_result

    def state(self):
        return self.state_obj


class _Containers:
    """The `client.containers` collection."""

    def __init__(self, client):
        self._client = client
        self._store: dict[str, FakeContainer] = {}

    def all(self):
        return list(self._store.values())

    def get(self, name):
        if name not in self._store:
            raise KeyError(f"no such container: {name}")
        return self._store[name]

    def create(self, config, wait=False):
        name = config["name"]
        if name in self._store:
            raise ValueError(f"container already exists: {name}")
        container = FakeContainer(
            name=name,
            image=config.get("source", {}).get("alias", ""),
            status="Stopped",
            config=config.get("config", {}),
        )
        container._client = self._client
        self._store[name] = container
        return container


class FakeLXDClient:
    """The object backend.lxd.client._get_client is patched to return."""

    def __init__(self):
        self.containers = _Containers(self)
        self.host_info = {
            "environment": {"server_version": "5.21.6-fake"},
        }
        # Nested sockets->cores->threads shape, so _sum_cpu_threads' real
        # walk is exercised: 1 socket x 4 cores x 2 threads = 8 threads.
        self.resources = {
            "cpu": {
                "sockets": [
                    {"cores": [{"threads": [{}, {}]} for _ in range(4)]}
                ]
            },
            "memory": {"total": 16 * 1024**3, "used": 4 * 1024**3},
            "storage": {
                "pools": [
                    {"space_total": 500 * 1024**3, "space_used": 100 * 1024**3}
                ]
            },
        }

    def add(self, name, **kwargs) -> FakeContainer:
        """Seed a pre-existing container into the fake daemon."""
        container = FakeContainer(name, **kwargs)
        container._client = self
        self.containers._store[name] = container
        return container
