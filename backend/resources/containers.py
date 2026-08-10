"""
Container management endpoints.

Provides list and create operations for LXD containers. Every mutation
writes an audit log entry. Names are re-validated server-side because
the frontend form being correct is a UX nicety only — an attacker can
craft requests that bypass client-side validation entirely.
"""

import re

import falcon

from backend.auth.middleware import require_role
from backend.db import repo
from backend.lxd import client as lxd_client
from backend.lxd.quota import check_quota

# LXD container naming rules: lowercase alphanumeric + hyphens,
# must start with a letter, must not end with a hyphen, 1-63 chars.
_CONTAINER_NAME_RE = re.compile(r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")


def _enrich_with_lxd_state(db_record: dict) -> dict:
    """Merge a DB container record with live LXD state if available.

    If LXD is unreachable or the container doesn't exist in LXD, the
    DB record is returned as-is with a 'lxd_status' of 'Unknown'.
    """
    result = dict(db_record)
    lxd_info = lxd_client.get_container(db_record["lxd_name"])
    if lxd_info:
        result["lxd_status"] = lxd_info.get("status", "Unknown")
        result["lxd_config"] = lxd_info.get("config", {})
    else:
        result["lxd_status"] = "Unknown"
        result["lxd_config"] = {}
    return result


class ContainerListResource:
    """Handles GET (list) and POST (create) for containers.

    GET returns different results depending on the caller's role:
      - admin: all active containers
      - user: only containers assigned to them

    POST is admin-only and creates a new LXD container with server-side
    name validation and quota enforcement.
    """

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        """Return a list of containers visible to the current user."""
        if req.context.user is None:
            raise falcon.HTTPUnauthorized(
                title="Not authenticated",
                description="Please sign in to view containers.",
            )

        user_id = req.context.user["id"]
        role = req.context.user["role"]

        if role == "admin":
            # Admins see every active container
            containers = repo.list_active_containers()
        else:
            # Regular users see only their assigned containers
            assignments = repo.list_assignments_for_user(user_id)
            containers = []
            for assignment in assignments:
                container = repo.get_container_by_id(
                    assignment["container_id"]
                )
                # Only include active (not soft-deleted) containers
                if container and container["deleted_at"] is None:
                    containers.append(container)

        # Enrich each DB record with live LXD state (status, config)
        enriched = [_enrich_with_lxd_state(c) for c in containers]
        resp.media = {"containers": enriched}

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        """Create a new LXD container (admin only).

        Expected JSON body:
        {
            "name": "my-container",
            "image": "ubuntu:22.04",
            "limits": {
                "ram_mb": 1024,
                "cpu": 1.0,
                "disk_gb": 10
            },
            "assign_to": "user-uuid"  (optional, pre-assign to a user)
        }
        """
        require_role(req, "admin")

        body = req.get_media()

        # --- Validate container name ---
        name = body.get("name", "").strip()
        # Server-side re-validation of the container name against LXD
        # naming rules. The frontend form may also validate this, but
        # that is a UX nicety only — an attacker can craft requests
        # that bypass any client-side check, so we enforce it here.
        if not name or not _CONTAINER_NAME_RE.match(name):
            raise falcon.HTTPBadRequest(
                title="Invalid container name",
                description=(
                    "Container names must be 1-63 characters, start with "
                    "a lowercase letter, contain only lowercase letters, "
                    "digits, and hyphens, and must not end with a hyphen."
                ),
            )

        # Check if name already exists in our DB
        if repo.get_container_by_lxd_name(name):
            raise falcon.HTTPConflict(
                title="Container name already exists",
                description=f"A container named '{name}' already exists.",
            )

        image = body.get("image", "").strip()
        if not image:
            raise falcon.HTTPBadRequest(
                title="Missing image",
                description="An image alias is required (e.g., 'ubuntu:22.04').",
            )

        # Parse resource limits from the request body
        limits_body = body.get("limits", {})
        ram_mb = int(limits_body.get("ram_mb", 0))
        cpu = float(limits_body.get("cpu", 0))
        disk_gb = int(limits_body.get("disk_gb", 0))

        # If pre-assigning to a user, check their quota first
        assign_to = body.get("assign_to")
        if assign_to:
            target_user = repo.get_user_by_id(assign_to)
            if not target_user:
                raise falcon.HTTPBadRequest(
                    title="User not found",
                    description=f"No user found with id '{assign_to}'.",
                )
            allowed, reason = check_quota(
                assign_to, ram_mb, cpu, disk_gb
            )
            if not allowed:
                raise falcon.HTTPBadRequest(
                    title="Quota exceeded",
                    description=reason,
                )

        # Build LXD config from the requested limits
        lxd_limits = {}
        if ram_mb > 0:
            lxd_limits["limits.memory"] = f"{ram_mb}MB"
        if cpu > 0:
            lxd_limits["limits.cpu"] = str(cpu)

        # Create the container in LXD
        try:
            lxd_client.create_container(name, image, lxd_limits)
        except Exception as e:
            raise falcon.HTTPInternalServerError(
                title="Container creation failed",
                description=f"LXD error: {e}",
            )

        # Record the container in our database with cached limits
        container_id = repo.create_container_record(
            lxd_name=name,
            image=image,
            created_by=req.context.user["id"],
            limit_ram_mb=ram_mb,
            limit_cpu=cpu,
            limit_disk_gb=disk_gb,
        )

        # Pre-assign to user if requested
        if assign_to:
            repo.assign_container(assign_to, container_id)

        # Audit trail for container creation
        repo.write_audit_log(
            user_id=req.context.user["id"],
            action="container.create",
            target=container_id,
            detail=f"Created container '{name}' (image={image}, "
            f"ram={ram_mb}MB, cpu={cpu}, disk={disk_gb}GB)"
            + (f", assigned to {assign_to}" if assign_to else ""),
        )

        resp.status = falcon.HTTP_201
        resp.media = {
            "id": container_id,
            "name": name,
            "image": image,
            "limits": {"ram_mb": ram_mb, "cpu": cpu, "disk_gb": disk_gb},
        }
