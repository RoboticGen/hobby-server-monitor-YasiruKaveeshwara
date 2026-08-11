"""
Usage accounting endpoint (admin-only).

Answers two questions an admin needs before creating anything new:
how much of the host is actually handed out, and which user is holding it.

The response deliberately separates three different kinds of number, because
conflating them is how capacity planning goes wrong:

  - host      — what the machine physically has (from LXD)
  - allocated — what we have promised to containers (from the DB)
  - per-user  — allocation vs quota for each user (from the DB)

"Allocated" is not "used". A container with limits.memory=2GB counts 2GB
against allocation whether it is running flat out or stopped. Live
consumption is a separate concern served by the metrics endpoints, which
read TinyFlux; mixing the two into one figure would make it impossible to
tell "we are out of capacity" from "the containers are busy right now".
"""

import falcon

from backend.auth.middleware import require_role
from backend.db import repo
from backend.lxd import client as lxd_client
from backend.lxd.client import LXDUnavailableError
from backend.lxd.quota import compute_user_allocation


class AccountingResource:
    """GET /api/accounting — host and per-user usage totals (admin only)."""

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        """Return host capacity, total allocation, and a per-user breakdown."""
        require_role(req, "admin")

        # --- Host capacity (the only LXD-dependent part) ---
        # Read endpoint degrades instead of failing: if
        # LXD is down we still return the DB-derived allocation and per-user
        # table (which need no LXD at all) and flag the response stale, so
        # the admin page renders a banner over a working table rather than
        # an error page with nothing on it.
        host = None
        stale = False
        host_error = None
        try:
            host = lxd_client.get_host_resources()
        except LXDUnavailableError as exc:
            stale = True
            host_error = str(exc)

        # --- Total allocation across every live container ---
        # Counted straight from the containers table rather than by summing
        # the per-user rows below: a container assigned to nobody still
        # consumes host capacity, and one assigned to two users would
        # otherwise be double-counted. This figure is what to compare
        # against host capacity.
        containers = repo.list_active_containers()
        allocated_ram_mb = 0
        allocated_cpu = 0.0
        allocated_disk_gb = 0
        for container in containers:
            allocated_ram_mb += container.get("limit_ram_mb") or 0
            allocated_cpu += container.get("limit_cpu") or 0.0
            allocated_disk_gb += container.get("limit_disk_gb") or 0

        allocated = {
            "ram_mb": allocated_ram_mb,
            "cpu": round(allocated_cpu, 2),
            "disk_gb": allocated_disk_gb,
            "container_count": len(containers),
        }

        # --- Per-user allocation vs quota ---
        # Reuses compute_user_allocation so this endpoint and the quota
        # enforcement path can never disagree about what a user is holding.
        users = []
        for user in repo.list_users():
            allocation = compute_user_allocation(user["id"])

            # Count only assignments whose container is still live. An
            # assignment row stays active=1 after its container is
            # soft-deleted, so counting rows directly would report a
            # container that contributes nothing to `allocation` above —
            # the same row would show "1 container, 0 MB". Filtering here
            # keeps the two figures describing the same set.
            container_count = 0
            for assignment in repo.list_assignments_for_user(user["id"]):
                container = repo.get_container_by_id(assignment["container_id"])
                if container is not None and container["deleted_at"] is None:
                    container_count += 1

            users.append(
                {
                    "id": user["id"],
                    "email": user["email"],
                    "role": user["role"],
                    "status": user["status"],
                    "allocation": allocation,
                    "quota": {
                        "ram_mb": user["quota_ram_mb"],
                        "cpu": user["quota_cpu"],
                        "disk_gb": user["quota_disk_gb"],
                    },
                    "container_count": container_count,
                }
            )

        resp.media = {
            "host": host,
            "allocated": allocated,
            "users": users,
            # stale=True means the host block is null because LXD was
            # unreachable, NOT that the allocation figures are suspect —
            # those come from SQLite and are always current.
            "stale": stale,
            "host_error": host_error,
        }
