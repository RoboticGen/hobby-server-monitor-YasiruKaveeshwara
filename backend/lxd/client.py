"""
LXD client wrapper — the ONLY module that imports pylxd.

Every other module in the project accesses LXD functionality through the
functions exported here. This single-point-of-import rule exists for
security auditability: a reviewer can open this one file and see the
complete set of operations the application performs against the host's
LXD daemon, without having to grep the entire codebase for pylxd calls.

All functions in this module are designed to be safe to call even if LXD
is unreachable — they return None/False/empty rather than crashing, and
leave error handling to their callers.
"""

import pylxd

from backend.config import config


def _get_client() -> pylxd.Client:
    """Create a pylxd Client using the configured LXD endpoint.

    If LXD_CERT_PATH and LXD_KEY_PATH are set (non-empty), they are
    passed for remote HTTPS connections. Otherwise a unix socket
    connection is assumed.
    """
    if config.lxd_cert_path and config.lxd_key_path:
        return pylxd.Client(
            endpoint=config.lxd_endpoint,
            cert=(config.lxd_cert_path, config.lxd_key_path),
            verify=False,  # self-signed LXD certs in typical setups
        )
    return pylxd.Client(endpoint=config.lxd_endpoint)


def check_lxd_reachable() -> bool:
    """Check whether the LXD daemon is reachable.

    Returns True if we can connect and query LXD, False otherwise.
    This function must never raise — it is called by the health
    endpoint and a crash there would hide the real issue
    (LXD being down) behind a 500 error.
    """
    try:
        client = _get_client()
        # Accessing .host_info performs a lightweight GET /1.0 call
        _ = client.host_info
        return True
    except Exception:
        return False


def list_containers() -> list[dict]:
    """Return a list of all containers known to LXD.

    Each dict contains at minimum: name, status, architecture,
    created_at, and config (which includes memory/cpu limits).
    Returns an empty list if LXD is unreachable.
    """
    try:
        client = _get_client()
        containers = client.containers.all()
        return [
            {
                "name": c.name,
                "status": c.status,
                "architecture": c.architecture,
                "created_at": str(c.created_at),
                "config": dict(c.config) if c.config else {},
            }
            for c in containers
        ]
    except Exception:
        return []


def get_container(name: str) -> dict | None:
    """Look up a single container by its LXD-side name.

    Returns a dict with the container's details, or None if not found
    or if LXD is unreachable.
    """
    try:
        client = _get_client()
        c = client.containers.get(name)
        return {
            "name": c.name,
            "status": c.status,
            "architecture": c.architecture,
            "created_at": str(c.created_at),
            "config": dict(c.config) if c.config else {},
        }
    except Exception:
        return None


def create_container(name: str, image: str, limits: dict) -> dict:
    """Create a new LXD container with the given name, image, and limits.

    The limits dict should contain keys like 'limits.memory' and
    'limits.cpu' matching the LXD config format.

    Returns a dict describing the created container.
    Raises on failure (callers should handle this).
    """
    client = _get_client()
    container_config = {
        "name": name,
        "source": {
            "type": "image",
            "alias": image,
        },
        "config": limits,
    }
    container = client.containers.create(container_config, wait=True)
    return {
        "name": container.name,
        "status": container.status,
        "architecture": container.architecture,
        "created_at": str(container.created_at),
        "config": dict(container.config) if container.config else {},
    }


def delete_container(name: str) -> None:
    """Stop (if running) and delete an LXD container.

    Stops the container first if it is currently running, then
    deletes it. Raises on failure (callers should handle this).
    """
    client = _get_client()
    container = client.containers.get(name)
    if container.status.lower() == "running":
        container.stop(wait=True)
    container.delete(wait=True)


def rename_container(old_name: str, new_name: str) -> None:
    """Rename an LXD container.

    The container must be stopped for rename to succeed in most
    LXD configurations. Raises on failure.
    """
    client = _get_client()
    container = client.containers.get(old_name)
    container.rename(new_name, wait=True)


def execute_command(
    name: str, command: list[str]
) -> tuple[int, str, str]:
    """Execute a command inside a running LXD container.

    Returns a tuple of (exit_code, stdout, stderr).
    The container must be in the 'Running' state.
    Raises on failure (e.g., container not running, command not found).
    """
    client = _get_client()
    container = client.containers.get(name)
    result = container.execute(command)
    return (result.exit_code, result.stdout, result.stderr)


# ====================== State Management ====================================

# Valid state actions that map to pylxd container methods.
_STATE_ACTIONS = {"start", "stop", "restart", "freeze", "unfreeze"}


def change_container_state(name: str, action: str) -> None:
    """Change a container's state (start, stop, restart, freeze, unfreeze).

    Raises ValueError if the action is not one of the allowed actions.
    Raises on LXD failure (e.g., trying to start an already running container).
    """
    if action not in _STATE_ACTIONS:
        raise ValueError(
            f"Invalid state action '{action}'. "
            f"Must be one of: {_STATE_ACTIONS}"
        )
    client = _get_client()
    container = client.containers.get(name)
    getattr(container, action)(wait=True)


# ====================== Limit Updates =======================================


def update_container_limits(name: str, limits: dict) -> None:
    """Update resource limits on a running or stopped LXD container.

    The limits dict should contain LXD config keys like 'limits.memory'
    and 'limits.cpu'. Existing keys not in the dict are left unchanged.
    """
    client = _get_client()
    container = client.containers.get(name)
    for key, value in limits.items():
        container.config[key] = value
    container.save(wait=True)


# ====================== Error Types =========================================


class LXDUnavailableError(Exception):
    """Raised when LXD is unreachable or returns an unexpected error.

    The collector catches this specifically so it can log a warning and
    continue without crashing — matching the brief's independence
    requirement. Callers that cannot tolerate LXD being down (e.g., the
    health endpoint) should also catch this.
    """


# ====================== Host Resources ======================================


def _sum_cpu_threads(cpu_info: dict) -> float:
    """Count total CPU threads from an LXD resources 'cpu' block.

    LXD reports CPUs as sockets -> cores -> threads. We count threads
    (not cores) because that is what container 'limits.cpu' values are
    allocated against, so host capacity and allocation use the same unit.
    Newer LXD versions also expose a flat 'total'; we prefer the explicit
    walk and fall back to 'total' when the nested shape is missing.
    """
    total = 0
    for socket in cpu_info.get("sockets") or []:
        for core in socket.get("cores") or []:
            threads = core.get("threads")
            total += len(threads) if threads else 1
    if total == 0:
        total = int(cpu_info.get("total") or 0)
    return float(total)


def _sum_storage(storage_info: dict) -> tuple[float, float]:
    """Return (total_gb, used_gb) from an LXD resources 'storage' block.

    LXD has reported storage under two different shapes across versions
    ('pools' with space_total/space_used, and 'disks' with size), so both
    are handled here rather than assuming one. Anything unrecognized
    yields 0.0 instead of raising — a missing disk figure should not take
    down the whole accounting endpoint.
    """
    gb = 1024 * 1024 * 1024
    total = 0.0
    used = 0.0

    for pool in storage_info.get("pools") or []:
        total += float(pool.get("space_total") or 0) / gb
        used += float(pool.get("space_used") or 0) / gb

    # Older/alternate shape: physical disks, which report size but no usage.
    if total == 0.0:
        for disk in storage_info.get("disks") or []:
            total += float(disk.get("size") or 0) / gb

    return (total, used)


def get_host_resources() -> dict:
    """Return the host's total CPU/RAM/disk capacity as reported by LXD.

    This is the "what the machine physically has" half of the accounting
    endpoint; the "what we've handed out" half comes from the DB. Values
    are normalized to the same units the rest of the app uses (MB for
    RAM, GB for disk, threads for CPU) so the caller can compare host
    capacity against allocation without converting anything.

    Raises LXDUnavailableError if LXD is unreachable or does not support
    the resources API extension. Callers are expected to catch this and
    degrade (decision 7.10) rather than fail the whole request — host
    capacity is the only part of accounting that needs LXD at all.
    """
    try:
        client = _get_client()
        resources = client.resources
    except Exception as exc:
        raise LXDUnavailableError(
            f"Cannot read host resources from LXD: {exc}"
        ) from exc

    cpu_cores = _sum_cpu_threads(resources.get("cpu") or {})

    memory = resources.get("memory") or {}
    mb = 1024 * 1024
    ram_total_mb = float(memory.get("total") or 0) / mb
    ram_used_mb = float(memory.get("used") or 0) / mb

    disk_total_gb, disk_used_gb = _sum_storage(resources.get("storage") or {})

    return {
        "cpu_cores": cpu_cores,
        "ram_total_mb": ram_total_mb,
        "ram_used_mb": ram_used_mb,
        "disk_total_gb": disk_total_gb,
        "disk_used_gb": disk_used_gb,
    }


# ====================== Metrics State =======================================


def get_container_state(lxd_name: str) -> dict:
    """Return a flat metrics dict for a running container.

    Reads live state from LXD (CPU usage, RAM, disk, network I/O, and
    process count) and shapes it into a flat dict matching the TinyFlux
    field layout described in PROJECT-PLAN.md section 8.2. All values
    are floats for TinyFlux compatibility.

    Raises LXDUnavailableError if LXD is unreachable or the container
    is not in a state where metrics are readable (e.g. stopped). The
    collector catches this and logs a warning instead of crashing.
    """
    try:
        client = _get_client()
        container = client.containers.get(lxd_name)
        state = container.state()
    except Exception as exc:
        raise LXDUnavailableError(
            f"Cannot read state for container '{lxd_name}': {exc}"
        ) from exc

    # CPU: LXD returns total nanoseconds used; we expose it as a float.
    # The retention/downsampling job (Phase 10) converts successive
    # snapshots to a percentage average before writing "5m" points.
    cpu_usage = 0.0
    if state.cpu and state.cpu.usage is not None:
        cpu_usage = float(state.cpu.usage)

    # Memory in bytes
    ram_used_mb = 0.0
    ram_total_mb = 0.0
    if state.memory:
        ram_used_mb = float(state.memory.usage or 0) / (1024 * 1024)
        ram_total_mb = float(state.memory.usage_peak or 0) / (1024 * 1024)

    # Disk usage: sum the root disk device
    disk_used_mb = 0.0
    if state.disk:
        root = state.disk.get("root")
        if root and root.usage is not None:
            disk_used_mb = float(root.usage) / (1024 * 1024)

    # Network I/O: sum across all interfaces
    net_rx_bytes = 0.0
    net_tx_bytes = 0.0
    if state.network:
        for iface_data in state.network.values():
            counters = iface_data.get("counters", {})
            net_rx_bytes += float(counters.get("bytes_received", 0))
            net_tx_bytes += float(counters.get("bytes_sent", 0))

    # Process count
    pid_count = 0.0
    if state.processes is not None:
        pid_count = float(state.processes)

    return {
        "cpu_usage_ns": cpu_usage,
        "ram_used_mb": ram_used_mb,
        "ram_peak_mb": ram_total_mb,
        "disk_used_mb": disk_used_mb,
        "net_rx_bytes": net_rx_bytes,
        "net_tx_bytes": net_tx_bytes,
        "pid_count": pid_count,
    }
