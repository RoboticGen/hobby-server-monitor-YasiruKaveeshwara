"""
Per-user quota calculation and enforcement.

Computes how much of a user's resource quota is currently allocated
across their assigned containers, and checks whether a proposed new
allocation would exceed their limits.

Design choice: container limits are read from the DB cache (the
limit_ram_mb, limit_cpu, limit_disk_gb columns on the containers table)
rather than from live LXD calls. This keeps quota checks fast and
available even when LXD itself is slow or temporarily unreachable.
The DB cache is written when a container is created or its limits are
updated, so it stays in sync with the actual LXD state during normal
operation.
"""

from backend.db import repo


def compute_user_allocation(user_id: str) -> dict:
    """Sum RAM/CPU/disk across all containers actively assigned to a user.

    Reads limits from the DB-cached columns (not live LXD), so this
    function is fast and independent of LXD availability.

    Returns a dict with keys: ram_mb, cpu, disk_gb.
    """
    assignments = repo.list_assignments_for_user(user_id)
    total_ram = 0
    total_cpu = 0.0
    total_disk = 0

    for assignment in assignments:
        container = repo.get_container_by_id(assignment["container_id"])
        # Skip containers that have been soft-deleted or can't be found
        if container is None or container["deleted_at"] is not None:
            continue
        total_ram += container.get("limit_ram_mb", 0)
        total_cpu += container.get("limit_cpu", 0.0)
        total_disk += container.get("limit_disk_gb", 0)

    return {
        "ram_mb": total_ram,
        "cpu": total_cpu,
        "disk_gb": total_disk,
    }


def check_quota(
    user_id: str,
    additional_ram_mb: int,
    additional_cpu: float,
    additional_disk_gb: int,
) -> tuple[bool, str]:
    """Check whether adding the given resources would stay within quota.

    Returns (True, "") if the addition is within the user's limits.
    Returns (False, "<specific reason>") if it would exceed quota,
    naming the exact resource and how much it would be exceeded by
    (decision 7.3: a hard block with the specific number, not a vague
    rejection, so the admin can see exactly what to adjust).
    """
    user = repo.get_user_by_id(user_id)
    if user is None:
        return (False, "User not found")

    current = compute_user_allocation(user_id)

    # Check each resource individually so the error message names the
    # specific resource that would be exceeded
    new_ram = current["ram_mb"] + additional_ram_mb
    if new_ram > user["quota_ram_mb"] and user["quota_ram_mb"] > 0:
        excess = new_ram - user["quota_ram_mb"]
        return (
            False,
            f"Would exceed RAM quota by {excess}MB "
            f"(current: {current['ram_mb']}MB + requested: {additional_ram_mb}MB "
            f"= {new_ram}MB, quota: {user['quota_ram_mb']}MB)",
        )

    new_cpu = current["cpu"] + additional_cpu
    if new_cpu > user["quota_cpu"] and user["quota_cpu"] > 0:
        excess = round(new_cpu - user["quota_cpu"], 2)
        return (
            False,
            f"Would exceed CPU quota by {excess} cores "
            f"(current: {current['cpu']} + requested: {additional_cpu} "
            f"= {new_cpu}, quota: {user['quota_cpu']})",
        )

    new_disk = current["disk_gb"] + additional_disk_gb
    if new_disk > user["quota_disk_gb"] and user["quota_disk_gb"] > 0:
        excess = new_disk - user["quota_disk_gb"]
        return (
            False,
            f"Would exceed disk quota by {excess}GB "
            f"(current: {current['disk_gb']}GB + requested: {additional_disk_gb}GB "
            f"= {new_disk}GB, quota: {user['quota_disk_gb']}GB)",
        )

    return (True, "")
