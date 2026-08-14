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

from urllib.parse import urlparse

import pylxd

from backend.config import config


def _normalize_lxd_endpoint(endpoint: str) -> str:
    """Normalize the configured LXD endpoint for pylxd.

    pylxd accepts a raw unix socket filesystem path, not a URI with
    the `unix://` scheme. This helper rewrites both `unix://...` and
    `unix:///...` forms into a bare path that pylxd accepts.
    """
    parsed = urlparse(endpoint)
    if parsed.scheme != "unix":
        return endpoint

    # unix://var/snap/lxd/common/lxd/unix.socket parses as netloc='var'
    # and path='/snap/...'. Reconstruct the true filesystem path by
    # prepending a slash when netloc is present.
    if parsed.netloc:
        return "/" + parsed.netloc + parsed.path
    return parsed.path


def _get_client() -> pylxd.Client:
    """Create a pylxd Client using the configured LXD endpoint.

    If LXD_CERT_PATH and LXD_KEY_PATH are set (non-empty), they are
    passed for remote HTTPS connections. Otherwise a unix socket
    connection is assumed.
    """
    endpoint = _normalize_lxd_endpoint(config.lxd_endpoint)
    if config.lxd_cert_path and config.lxd_key_path:
        return pylxd.Client(
            endpoint=endpoint,
            cert=(config.lxd_cert_path, config.lxd_key_path),
            verify=False,  # self-signed LXD certs in typical setups
        )
    return pylxd.Client(endpoint=endpoint)


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
            # Exposed so a description set at creation can be read back;
            # without it the field would be write-only and unverifiable.
            "description": getattr(c, "description", "") or "",
        }
    except Exception:
        return None


def get_container_details(name: str) -> dict | None:
    """Return full container details including live LXD state and network info.

    This is used for the detail page, where the UI needs IP addresses,
    runtime state, and the current image metadata. It is deliberately
    separate from get_container() so list views do not pay the cost of
    an extra state() call for every container.
    """
    try:
        client = _get_client()
        c = client.containers.get(name)
        state = c.state()
        return {
            "name": c.name,
            "status": c.status,
            "architecture": c.architecture,
            "created_at": str(c.created_at),
            "config": dict(c.config) if c.config else {},
            "description": getattr(c, "description", "") or "",
            "state": {
                "status": getattr(state, "status", "Unknown"),
                "status_code": getattr(state, "status_code", 0),
                "processes": getattr(state, "processes", 0),
                "pid": getattr(state, "pid", 0),
                "cpu": getattr(state, "cpu", {}),
                "memory": getattr(state, "memory", {}),
                "disk": getattr(state, "disk", {}),
                "network": getattr(state, "network", {}),
            },
        }
    except Exception:
        return None


def create_container(
    name: str,
    image: str,
    limits: dict,
    *,
    network: str = "",
    storage_pool: str = "",
    ephemeral: bool = False,
    description: str = "",
) -> dict:
    """Create a new LXD container with the given name, image, and limits.

    The limits dict should contain keys like 'limits.memory' and
    'limits.cpu' matching the LXD config format. Autostart is passed in
    that same dict as 'boot.autostart', since LXD models it as ordinary
    config rather than a top-level property.

    `network` and `storage_pool` are applied as device overrides. Both are
    optional and omitted entirely when blank, in which case the container
    inherits whatever the default profile specifies — which is the
    behaviour every container had before these arguments existed.

    Returns a dict describing the created container.
    Raises on failure (callers should handle this).
    """
    client = _get_client()
    # Support both image aliases (e.g. 'ubuntu:22.04') and raw image
    # fingerprints. If the caller supplied a fingerprint, ask LXD to
    # use it directly rather than a (possibly-missing) alias.
    source = {"type": "image"}
    # A simple fingerprint heuristic: hex string of at least 12 chars.
    # Short fingerprints are commonly used in output listing, so accept
    # those too.
    import re

    if isinstance(image, str) and re.fullmatch(r"[0-9a-fA-F]{12,64}", image):
        source["fingerprint"] = image
    else:
        source["alias"] = image

    container_config = {
        "name": name,
        "source": source,
        "config": limits,
        # LXD deletes an ephemeral container as soon as it stops, so this
        # is a top-level property rather than a config key.
        "ephemeral": ephemeral,
    }

    # Only set when non-empty: LXD treats a description of "" as an
    # explicit blank, which is the same as omitting it, but sending the key
    # unconditionally makes the intent harder to read.
    if description:
        container_config["description"] = description

    # Devices are built only for the fields the caller actually supplied.
    # An empty devices dict is left off completely so the default profile's
    # eth0/root devices apply untouched — sending a partial override would
    # replace them rather than merge.
    devices: dict[str, dict] = {}
    if network:
        devices["eth0"] = {"type": "nic", "network": network, "name": "eth0"}
    if storage_pool:
        # NOTE: no "size" key here, so this selects which pool the root
        # disk lives on without imposing a disk quota. See the step summary
        # — limit_disk_gb is recorded in the DB but has never been applied
        # to LXD, and starting to enforce it is a behaviour change beyond
        # the scope of adding pool selection.
        devices["root"] = {"type": "disk", "pool": storage_pool, "path": "/"}
    if devices:
        container_config["devices"] = devices

    container = client.containers.create(container_config, wait=True)
    return {
        "name": container.name,
        "status": container.status,
        "architecture": container.architecture,
        "created_at": str(container.created_at),
        "config": dict(container.config) if container.config else {},
    }


# ====================== Host Options ========================================
# The three listings below back the container-creation form's dropdowns.
# Each raises LXDUnavailableError rather than returning an empty list on
# failure, so the caller can tell "this host genuinely has no custom
# networks" from "we could not ask" — presenting the second as the first
# would show an empty dropdown that looks authoritative.


def list_images() -> list[dict]:
    """Return the aliases of images cached locally on this host.

    IMPORTANT LIMITATION: this lists images already downloaded to the local
    image store, NOT everything installable. Remote aliases like
    'ubuntu:22.04' resolve through remotes, which are a client-side CLI
    concept that the LXD HTTP API does not expose — so a fresh host with an
    empty cache returns an empty list even though thousands of images are
    installable. The creation form therefore keeps its image field
    free-text and treats this list as suggestions, not as the valid set.
    """
    try:
        client = _get_client()
        images = []
        for image in client.images.all():
            # Prefer reporting aliases when present — that's what admins
            # normally type. But include an entry for an image that has no
            # alias by exposing its short fingerprint so the form can still
            # show and select it.
            aliases = image.aliases or []
            if aliases:
                for alias in aliases:
                    images.append(
                        {
                            "alias": alias.get("name", ""),
                            "description": (
                                image.properties.get("description", "")
                                if image.properties
                                else ""
                            ),
                        }
                    )
            else:
                fp = getattr(image, "fingerprint", "") or ""
                display = fp[:12] if fp else ""
                images.append(
                    {
                        "alias": display,
                        "description": (
                            image.properties.get("description", "")
                            if image.properties
                            else ""
                        ),
                    }
                )
        return sorted(images, key=lambda item: item["alias"])
    except Exception as exc:
        raise LXDUnavailableError(f"Cannot list images from LXD: {exc}") from exc


def list_networks() -> list[dict]:
    """Return the networks LXD knows about.

    Includes unmanaged interfaces (the host's physical NICs, bridges
    created by other software) alongside LXD-managed ones, because a
    container can legitimately be attached to either. `managed` is passed
    through so the caller can distinguish them.
    """
    try:
        client = _get_client()
        return sorted(
            (
                {
                    "name": network.name,
                    "type": getattr(network, "type", ""),
                    "managed": bool(getattr(network, "managed", False)),
                }
                for network in client.networks.all()
            ),
            key=lambda item: item["name"],
        )
    except Exception as exc:
        raise LXDUnavailableError(f"Cannot list networks from LXD: {exc}") from exc


def list_storage_pools() -> list[dict]:
    """Return the storage pools available for a container's root disk."""
    try:
        client = _get_client()
        pools: list[dict] = []
        gb = 1024 * 1024 * 1024

        for pool in client.storage_pools.all():
            total_gb = 0.0
            used_gb = 0.0
            try:
                resources = pool.resources.get()
                space = getattr(resources, "space", {}) or {}
                total_gb = float(space.get("total") or 0) / gb
                used_gb = float(space.get("used") or 0) / gb
            except Exception:
                # If the per-pool resources shape is unexpected, still return
                # the pool name so the admin can choose it; disk bounds will
                # fall back to host-level capacity instead.
                total_gb = 0.0
                used_gb = 0.0

            pools.append(
                {
                    "name": pool.name,
                    "driver": getattr(pool, "driver", ""),
                    "total_gb": round(total_gb, 2),
                    "used_gb": round(used_gb, 2),
                    "available_gb": round(max(0.0, total_gb - used_gb), 2),
                }
            )

        return sorted(pools, key=lambda item: item["name"])
    except Exception as exc:
        raise LXDUnavailableError(f"Cannot list storage pools from LXD: {exc}") from exc


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


def execute_command(name: str, command: list[str]) -> tuple[int, str, str]:
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
            f"Invalid state action '{action}'. " f"Must be one of: {_STATE_ACTIONS}"
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


def _get_val(obj, key: str, default=None):
    """Safely get a value from either a dict or an object attribute."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


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
    # Handles both dict and object structures polymorphically.
    cpu = _get_val(state, "cpu")
    cpu_usage = float(_get_val(cpu, "usage", 0.0) or 0.0)

    # Memory in bytes
    mem = _get_val(state, "memory")
    ram_used_mb = float(_get_val(mem, "usage", 0.0) or 0.0) / (1024 * 1024)
    ram_peak_mb = float(_get_val(mem, "usage_peak", 0.0) or 0.0) / (1024 * 1024)

    # Disk usage: look up the root device
    disk = _get_val(state, "disk")
    root_disk = _get_val(disk, "root")
    disk_used_mb = float(_get_val(root_disk, "usage", 0.0) or 0.0) / (1024 * 1024)

    # Network I/O: sum across all interfaces
    net_rx_bytes = 0.0
    net_tx_bytes = 0.0
    net = _get_val(state, "network")
    if net:
        if isinstance(net, dict) or hasattr(net, "values"):
            ifaces = net.values()
        else:
            ifaces = [net]
        for iface_data in ifaces:
            counters = _get_val(iface_data, "counters")
            net_rx_bytes += float(_get_val(counters, "bytes_received", 0.0) or 0.0)
            net_tx_bytes += float(_get_val(counters, "bytes_sent", 0.0) or 0.0)

    # Process count
    pid_count = 0.0
    procs = _get_val(state, "processes")
    if procs is not None:
        pid_count = float(procs)

    return {
        "cpu_usage_ns": cpu_usage,
        "ram_used_mb": ram_used_mb,
        "ram_peak_mb": ram_peak_mb,
        "disk_used_mb": disk_used_mb,
        "net_rx_bytes": net_rx_bytes,
        "net_tx_bytes": net_tx_bytes,
        "pid_count": pid_count,
    }
