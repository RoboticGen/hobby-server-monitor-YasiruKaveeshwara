"""
Container management endpoints.

Provides list and create operations for LXD containers. Every mutation
writes an audit log entry. Names are re-validated server-side because
the frontend form being correct is a UX nicety only — an attacker can
craft requests that bypass client-side validation entirely.
"""

import re

import falcon
from pylxd.exceptions import LXDAPIException

from backend.auth.middleware import require_role
from backend.db import repo
from backend.lxd import client as lxd_client
from backend.lxd.client import LXDUnavailableError
from backend.lxd.quota import check_quota
from backend.resources.validation import (
    require_object,
    validate_bool,
    validate_limits,
    validate_string,
)

# LXD container naming rules: lowercase alphanumeric + hyphens,
# must start with a letter, must not end with a hyphen, 1-63 chars.
_CONTAINER_NAME_RE = re.compile(r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")


def _extract_ip_addresses(network: dict) -> list[str]:
    """Extract a list of non-loopback IPv4 addresses from LXD network state."""
    addresses: list[str] = []
    if not isinstance(network, dict):
        return addresses

    for iface in network.values():
        if not isinstance(iface, dict):
            continue
        for addr in iface.get("addresses", []) or []:
            if (
                isinstance(addr, dict)
                and addr.get("family") == "inet"
                and addr.get("scope") == "global"
                and addr.get("address")
            ):
                addresses.append(addr["address"])

    return addresses


def _enrich_with_lxd_state(db_record: dict) -> dict:
    """Merge a DB container record with live LXD state if available.

    If LXD is unreachable or the container doesn't exist in LXD, the
    DB record is returned as-is with a 'lxd_status' of 'Unknown'.
    """
    return _enrich_with_lxd_detail(db_record)


def _enrich_with_lxd_detail(db_record: dict) -> dict:
    """Merge a DB container record with the live details needed for detail view."""
    result = dict(db_record)
    lxd_info = lxd_client.get_container_details(db_record["lxd_name"])
    if lxd_info:
        config = lxd_info.get("config", {}) or {}
        state = lxd_info.get("state", {}) or {}
        result["lxd_status"] = lxd_info.get("status", "Unknown")
        result["lxd_architecture"] = lxd_info.get("architecture", "")
        result["lxd_created_at"] = lxd_info.get("created_at", "")
        result["lxd_image"] = (
            config.get("image.description")
            or config.get("image.os")
            or lxd_info.get("description")
            or result.get("image", "")
        )
        result["lxd_ip_addresses"] = _extract_ip_addresses(
            lxd_info.get("state", {}).get("network", {}),
        )
        result["lxd_process_count"] = int(state.get("processes", 0) or 0)
    else:
        result["lxd_status"] = "Unknown"
        result["lxd_architecture"] = ""
        result["lxd_created_at"] = result.get("created_at", "")
        result["lxd_image"] = result.get("image", "")
        result["lxd_ip_addresses"] = []
        result["lxd_process_count"] = 0
    return result


def _format_lxd_api_error(exc: LXDAPIException) -> str:
    """Return a stable, human-readable error message from an LXD API exception."""
    try:
        return str(exc)
    except Exception:
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                data = response.json()
                if isinstance(data, dict):
                    return (
                        data.get("error")
                        or data.get("metadata", {}).get("err")
                        or response.content.decode("utf-8", errors="ignore")
                    )
            except Exception:
                try:
                    return response.content.decode("utf-8", errors="ignore")
                except Exception:
                    pass
    return "Unknown LXD error"


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
                container = repo.get_container_by_id(assignment["container_id"])
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

        body = require_object(req.get_media())

        # --- Validate container name ---
        # required=False so a missing or null name falls through to the
        # "Invalid container name" message below, which says more than
        # "must be a string". A non-string name is a different mistake and
        # gets its own 400.
        name = validate_string(body.get("name"), "name", required=False)
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

        # The empty default keeps the existing behaviour of letting a
        # missing image fall through to the "Missing image" message; a
        # non-string image gets its own 400.
        image = validate_string(body.get("image"), "image", required=False)
        if not image:
            raise falcon.HTTPBadRequest(
                title="Missing image",
                description="An image alias is required (e.g., 'ubuntu:22.04').",
            )

        # Parse resource limits from the request body
        # 0 means unlimited, so a missing key and an explicit 0 agree.
        ram_mb, cpu, disk_gb = validate_limits(
            body.get("limits", {}),
            defaults={"ram_mb": 0, "cpu": 0.0, "disk_gb": 0},
        )

        # --- Optional placement and lifecycle fields ---
        # All five are optional and all default to "leave it alone": blank
        # network/storage_pool inherit the default profile, and both toggles
        # default to off. That keeps a body written before these existed
        # producing exactly the container it produced then.
        #
        # None of them are validated against what LXD actually offers. The
        # options endpoint below reports the real names, but a stale form
        # could still send a pool that has since been removed — LXD rejects
        # that at create time with a precise message, and re-checking here
        # would only duplicate a rule LXD already owns while opening a
        # window where the two disagree.
        network = validate_string(body.get("network"), "network", required=False)
        storage_pool = validate_string(
            body.get("storage_pool"), "storage_pool", required=False
        )
        description = validate_string(
            body.get("description"), "description", required=False
        )
        ephemeral = validate_bool(body.get("ephemeral"), "ephemeral")
        autostart = validate_bool(body.get("autostart"), "autostart")

        # If pre-assigning to a user, check their quota first
        assign_to = body.get("assign_to")
        if assign_to is not None and not isinstance(assign_to, str):
            raise falcon.HTTPBadRequest(
                title="Invalid assign_to",
                description="'assign_to' must be a user id string.",
            )
        if assign_to:
            target_user = repo.get_user_by_id(assign_to)
            if not target_user:
                raise falcon.HTTPBadRequest(
                    title="User not found",
                    description=f"No user found with id '{assign_to}'.",
                )
            allowed, reason = check_quota(assign_to, ram_mb, cpu, disk_gb)
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
            # Format CPU limits for LXD: use an integer string for whole
            # numbers ("1" not "1.0") and a compact decimal for floats.
            try:
                if isinstance(cpu, float) and cpu.is_integer():
                    cpu_str = str(int(cpu))
                elif isinstance(cpu, float):
                    cpu_str = ("%.3f" % cpu).rstrip("0").rstrip(".")
                else:
                    cpu_str = str(cpu)
            except Exception:
                cpu_str = str(cpu)
            lxd_limits["limits.cpu"] = cpu_str
        if autostart:
            # LXD models autostart as ordinary container config, not as a
            # top-level property, so it travels with the limits dict.
            lxd_limits["boot.autostart"] = "true"

        # Create the container in LXD
        try:
            lxd_client.create_container(
                name,
                image,
                lxd_limits,
                network=network,
                storage_pool=storage_pool,
                ephemeral=ephemeral,
                description=description,
            )
        except LXDAPIException as e:
            # A user-supplied create request failed validation at the LXD API
            # layer. This is a client error, not an outage. Report it as 400
            # so the frontend can show the actual LXD rejection reason.
            lxd_error = _format_lxd_api_error(e)
            raise falcon.HTTPBadRequest(
                title="Invalid container request",
                description=f"LXD rejected the create request: {lxd_error}",
            )
        except Exception as e:
            # LXD being unreachable is a service availability issue, not a code bug.
            # Return 503 with a clear message so the frontend can show a retry prompt
            # rather than a generic 500 that looks like a crash.
            raise falcon.HTTPServiceUnavailable(
                title="LXD unreachable",
                description=f"Cannot create container — LXD error: {e}",
            )

        # Record the container in our database with cached limits
        try:
            container_id = repo.create_container_record(
                lxd_name=name,
                image=image,
                created_by=req.context.user["id"],
                limit_ram_mb=ram_mb,
                limit_cpu=cpu,
                limit_disk_gb=disk_gb,
            )
        except repo.DuplicateKeyError:
            # Same window as the invite race: the name check above cannot
            # close it, so the UNIQUE constraint is what actually decides.
            raise falcon.HTTPConflict(
                title="Container name already exists",
                description=f"A container named '{name}' already exists.",
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


# Valid state actions accepted by the PATCH endpoint
_VALID_STATE_ACTIONS = {"start", "stop", "restart", "freeze", "unfreeze"}


class ContainerDetailResource:
    """Handles PATCH (state/limits update) and DELETE for a single container.

    Both operations are admin-only and both write an audit log entry —
    the project brief explicitly requires that "destructive and
    limit-changing actions leave a trail" for admin accountability.
    """

    def on_patch(
        self,
        req: falcon.Request,
        resp: falcon.Response,
        container_id: str,
    ) -> None:
        """Update a container's state or resource limits (admin only).

        Expected JSON body — one of:
        State change:  {"action": "start"|"stop"|"restart"|"freeze"|"unfreeze"}
        Limit change:  {"limits": {"ram_mb": 2048, "cpu": 2.0, "disk_gb": 20}}
        """
        require_role(req, "admin")

        container = repo.get_container_by_id(container_id)
        if not container:
            raise falcon.HTTPNotFound(
                title="Container not found",
                description=f"No container with id '{container_id}'.",
            )
        if container["deleted_at"] is not None:
            raise falcon.HTTPGone(
                title="Container deleted",
                description="This container has been deleted.",
            )

        body = require_object(req.get_media())
        action = body.get("action")
        limits = body.get("limits")

        if action and limits:
            raise falcon.HTTPBadRequest(
                title="Ambiguous request",
                description="Provide either 'action' or 'limits', not both.",
            )
        if not action and not limits:
            raise falcon.HTTPBadRequest(
                title="Nothing to do",
                description="Provide 'action' (state change) or 'limits' "
                "(resource update).",
            )

        # --- State change ---
        if action:
            if action not in _VALID_STATE_ACTIONS:
                raise falcon.HTTPBadRequest(
                    title="Invalid action",
                    description=f"Action must be one of: "
                    f"{sorted(_VALID_STATE_ACTIONS)}.",
                )
            try:
                lxd_client.change_container_state(container["lxd_name"], action)
            except Exception as e:
                # 503, not 500, when LXD is unreachable — a clean, documented failure rather
                # than a raw stack trace.
                raise falcon.HTTPServiceUnavailable(
                    title="LXD unreachable",
                    description=f"Cannot change state — LXD error: {e}",
                )

            # Audit log: every state change leaves a trail so admins can
            # trace who stopped/started what and when.
            repo.write_audit_log(
                user_id=req.context.user["id"],
                action=f"container.{action}",
                target=container_id,
                detail=f"{action} container '{container['lxd_name']}'",
            )

            resp.media = {
                "id": container_id,
                "action": action,
                "message": f"Container {action} successful",
            }
            return

        # --- Limit change ---
        # Missing keys default to the container's current limits, so a
        # partial body changes only what it names.
        new_ram, new_cpu, new_disk = validate_limits(
            limits,
            defaults={
                "ram_mb": container["limit_ram_mb"],
                "cpu": container["limit_cpu"],
                "disk_gb": container["limit_disk_gb"],
            },
        )

        # Quota check uses the DELTA (new - old), not the absolute new
        # value, because the user's quota is against their total allocation
        # across all containers.
        delta_ram = new_ram - container["limit_ram_mb"]
        delta_cpu = new_cpu - container["limit_cpu"]
        delta_disk = new_disk - container["limit_disk_gb"]

        # Only check quota if limits are increasing (decreasing always OK).
        #
        # The check runs against every user the container is assigned to,
        # not against container["created_by"]. The creator is the admin who
        # made it, and admins are typically unlimited, so checking them
        # meant the check never fired: grant a small container, then grow
        # it, and the assignee's quota was bypassed entirely. Grant-time
        # enforcement in assignments.py checks the grantee; this has to
        # match, or the two paths disagree about who the limits belong to.
        if delta_ram > 0 or delta_cpu > 0 or delta_disk > 0:
            for assignee in repo.list_assignees(container_id):
                allowed, reason = check_quota(
                    assignee["id"],
                    max(delta_ram, 0),
                    max(delta_cpu, 0.0),
                    max(delta_disk, 0),
                )
                if not allowed:
                    raise falcon.HTTPBadRequest(
                        title="Quota exceeded",
                        description=(f"{assignee['email']}: {reason}"),
                    )

        # Build LXD config from new limits
        lxd_limits = {}
        if new_ram > 0:
            lxd_limits["limits.memory"] = f"{new_ram}MB"
        if new_cpu > 0:
            try:
                if isinstance(new_cpu, float) and new_cpu.is_integer():
                    cpu_str = str(int(new_cpu))
                elif isinstance(new_cpu, float):
                    cpu_str = ("%.3f" % new_cpu).rstrip("0").rstrip(".")
                else:
                    cpu_str = str(new_cpu)
            except Exception:
                cpu_str = str(new_cpu)
            lxd_limits["limits.cpu"] = cpu_str

        # Update limits in LXD
        try:
            lxd_client.update_container_limits(container["lxd_name"], lxd_limits)
        except Exception as e:
            # Decision 7.10 / Phase 14.2: 503 so the frontend gets a
            # clean, retryable failure — not a generic 500.
            raise falcon.HTTPServiceUnavailable(
                title="LXD unreachable",
                description=f"Cannot update limits — LXD error: {e}",
            )

        # Update the DB cache to keep it in sync with LXD
        repo.update_container_limits(container_id, new_ram, new_cpu, new_disk)

        # Audit log: limit changes leave a trail so admins can trace
        # resource allocation changes over time.
        repo.write_audit_log(
            user_id=req.context.user["id"],
            action="container.limits",
            target=container_id,
            detail=(
                f"Updated limits on '{container['lxd_name']}': "
                f"ram={new_ram}MB, cpu={new_cpu}, disk={new_disk}GB"
            ),
        )

        resp.media = {
            "id": container_id,
            "limits": {"ram_mb": new_ram, "cpu": new_cpu, "disk_gb": new_disk},
            "message": "Limits updated successfully",
        }

    def on_delete(
        self,
        req: falcon.Request,
        resp: falcon.Response,
        container_id: str,
    ) -> None:
        """Delete a container (admin only): removes from LXD, soft-deletes in DB."""
        require_role(req, "admin")

        container = repo.get_container_by_id(container_id)
        if not container:
            raise falcon.HTTPNotFound(
                title="Container not found",
                description=f"No container with id '{container_id}'.",
            )
        if container["deleted_at"] is not None:
            raise falcon.HTTPGone(
                title="Already deleted",
                description="This container has already been deleted.",
            )

        # Delete from LXD first (stops if running, then removes)
        try:
            lxd_client.delete_container(container["lxd_name"])
        except Exception as e:
            # Clear 503 so the admin sees "LXD is down, try again later" rather than a confusing 500.
            raise falcon.HTTPServiceUnavailable(
                title="LXD unreachable",
                description=f"Cannot delete container — LXD error: {e}",
            )

        # Soft-delete in DB — the row stays so audit log entries and
        # TinyFlux metric data referencing this container remain valid.
        repo.soft_delete_container(container_id)

        # Audit log: every deletion is recorded per the project brief's
        # explicit requirement that destructive actions leave a trail.
        repo.write_audit_log(
            user_id=req.context.user["id"],
            action="container.delete",
            target=container_id,
            detail=f"Deleted container '{container['lxd_name']}'",
        )

        resp.media = {
            "id": container_id,
            "message": f"Container '{container['lxd_name']}' deleted",
        }


class LxdOptionsResource:
    """GET /api/lxd/options — the images, networks, and pools this host has.

    Exists so the creation form can offer what LXD actually reports instead
    of a hardcoded list that silently drifts from the host (PROJECT-PLAN
    5.2). Admin-only, matching container creation itself: a regular user
    cannot create a container, so the host's storage topology is not
    theirs to enumerate.
    """

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        """Return the host's available images, networks, and storage pools."""
        require_role(req, "admin")

        # Degrades rather than fails, same as the accounting endpoint: with
        # LXD down the form can still be rendered against free-text fields
        # and the server will reject anything invalid at create time. A 503
        # here would block creation entirely on a lookup that is only ever
        # advisory.
        stale = False
        lxd_error = None
        images: list[dict] = []
        networks: list[dict] = []
        storage_pools: list[dict] = []

        try:
            images = lxd_client.list_images()
            networks = lxd_client.list_networks()
            storage_pools = lxd_client.list_storage_pools()
        except LXDUnavailableError as exc:
            stale = True
            lxd_error = str(exc)

        resp.media = {
            "images": images,
            "networks": networks,
            "storage_pools": storage_pools,
            # stale=True means these lists are empty because LXD could not
            # be reached, NOT because the host has none. The form has to
            # tell those apart to avoid showing an empty dropdown as if it
            # were the authoritative set.
            "stale": stale,
            "lxd_error": lxd_error,
        }
