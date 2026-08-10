"""
Container assignment grant and revoke endpoints.

Manages which users have access to which containers. All operations
are admin-only and write audit log entries per the project brief's
explicit requirement that access changes leave a trail.

Quota is checked at grant time using the container's DB-cached limits.
Revoke is always allowed — removing access never exceeds any quota.

Design decision (from brief): requesting an unassigned container by
ID must return 403, not 404, so an attacker cannot enumerate container
IDs by probing for 404 vs 403 responses.
"""

import falcon

from backend.auth.middleware import require_role
from backend.db import repo
from backend.lxd.quota import check_quota


class AssignmentResource:
    """Handles POST (grant) and DELETE (revoke) for user-container assignments.

    URL pattern: /api/users/{user_id}/containers/{container_id}

    Both operations are admin-only. Quota is enforced at grant time.
    Revocation never requires a quota check — reducing allocation is
    always safe.
    """

    def on_post(
        self,
        req: falcon.Request,
        resp: falcon.Response,
        user_id: str,
        container_id: str,
    ) -> None:
        """Grant a user access to a container (admin only).

        Runs a quota check against the target user's limits before
        granting access, because granting access means the container's
        cached limits count toward that user's allocation from this
        point forward.
        """
        require_role(req, "admin")

        # Validate the target user
        user = repo.get_user_by_id(user_id)
        if not user:
            raise falcon.HTTPNotFound(
                title="User not found",
                description=f"No user with id '{user_id}'.",
            )

        # Validate the target container
        container = repo.get_container_by_id(container_id)
        if not container:
            raise falcon.HTTPNotFound(
                title="Container not found",
                description=f"No container with id '{container_id}'.",
            )
        if container["deleted_at"] is not None:
            raise falcon.HTTPGone(
                title="Container deleted",
                description="This container has been deleted and cannot be assigned.",
            )

        # Check if already assigned (active assignment)
        if repo.user_has_access(user_id, container_id):
            raise falcon.HTTPConflict(
                title="Already assigned",
                description=f"User '{user_id}' already has access to "
                f"container '{container_id}'.",
            )

        # Quota check: adding this container's cached limits to the
        # user's current allocation must not exceed their quota.
        allowed, reason = check_quota(
            user_id,
            additional_ram_mb=container["limit_ram_mb"],
            additional_cpu=container["limit_cpu"],
            additional_disk_gb=container["limit_disk_gb"],
        )
        if not allowed:
            raise falcon.HTTPBadRequest(
                title="Quota exceeded",
                description=reason,
            )

        assignment_id = repo.assign_container(user_id, container_id)

        # Audit trail: access grants are security-sensitive operations
        # that must leave a trail so admins can trace who was given
        # access to what and when.
        repo.write_audit_log(
            user_id=req.context.user["id"],
            action="assignment.grant",
            target=assignment_id,
            detail=(
                f"Granted user '{user['email']}' access to "
                f"container '{container['lxd_name']}'"
            ),
        )

        resp.status = falcon.HTTP_201
        resp.media = {
            "assignment_id": assignment_id,
            "user_id": user_id,
            "container_id": container_id,
        }

    def on_delete(
        self,
        req: falcon.Request,
        resp: falcon.Response,
        user_id: str,
        container_id: str,
    ) -> None:
        """Revoke a user's access to a container (admin only).

        Sets the assignment row to active=0 (soft-delete) so the access
        history is preserved. Does NOT hard-delete the row — matching
        Step 4.4's decision to keep assignment history for audit trails.
        """
        require_role(req, "admin")

        # Validate both exist
        user = repo.get_user_by_id(user_id)
        if not user:
            raise falcon.HTTPNotFound(
                title="User not found",
                description=f"No user with id '{user_id}'.",
            )

        container = repo.get_container_by_id(container_id)
        if not container:
            raise falcon.HTTPNotFound(
                title="Container not found",
                description=f"No container with id '{container_id}'.",
            )

        # Check there is an active assignment to revoke
        if not repo.user_has_access(user_id, container_id):
            raise falcon.HTTPNotFound(
                title="Assignment not found",
                description=f"User '{user_id}' does not have active access to "
                f"container '{container_id}'.",
            )

        repo.revoke_assignment(user_id, container_id)

        # Audit trail: access revocations are as security-sensitive as
        # grants and must be traceable.
        repo.write_audit_log(
            user_id=req.context.user["id"],
            action="assignment.revoke",
            target=container_id,
            detail=(
                f"Revoked user '{user['email']}' access to "
                f"container '{container['lxd_name']}'"
            ),
        )

        resp.media = {
            "user_id": user_id,
            "container_id": container_id,
            "message": "Access revoked",
        }


class ContainerDetailByUserResource:
    """GET a single container by ID, enforcing assignment-based access.

    Design decision (from brief): requesting an unassigned container by
    ID must return 403, NOT 404. Returning 404 would allow an attacker
    to enumerate valid container IDs by probing for the different
    response codes. 403 reveals nothing about whether the container
    exists at all.
    """

    def on_get(
        self,
        req: falcon.Request,
        resp: falcon.Response,
        container_id: str,
    ) -> None:
        """Return a container's details if the caller has access.

        Admins always have access. Regular users must have an active
        assignment. Unassigned containers return 403, not 404 — this
        matches the brief's explicit security requirement.
        """
        if req.context.user is None:
            raise falcon.HTTPUnauthorized(
                title="Not authenticated",
                description="Please sign in.",
            )

        container = repo.get_container_by_id(container_id)
        role = req.context.user["role"]

        # Admins always see all containers
        if role == "admin":
            if not container:
                raise falcon.HTTPNotFound(
                    title="Container not found",
                    description=f"No container with id '{container_id}'.",
                )
        else:
            # For non-admins: always return 403 for containers they
            # cannot access — even if the container doesn't exist.
            # This is the explicit security requirement from the brief:
            # "requesting an unassigned container by ID must return 403,
            # not 404" to prevent container ID enumeration.
            if not container or not repo.user_has_access(
                req.context.user["id"], container_id
            ):
                raise falcon.HTTPForbidden(
                    title="Access denied",
                    description="You do not have access to this container.",
                )

        if container["deleted_at"] is not None and role != "admin":
            raise falcon.HTTPForbidden(
                title="Access denied",
                description="You do not have access to this container.",
            )

        resp.media = dict(container)

    # Admin PATCH and DELETE are delegated to ContainerDetailResource
    # which already contains all the LXD state/limit logic and audit
    # logging. Delegation keeps the logic in one place and avoids
    # duplicating the ~100 lines of admin mutation code.

    def on_patch(
        self,
        req: falcon.Request,
        resp: falcon.Response,
        container_id: str,
    ) -> None:
        """Delegate to ContainerDetailResource (admin: state/limit update)."""
        from backend.resources.containers import ContainerDetailResource
        return ContainerDetailResource().on_patch(req, resp, container_id)

    def on_delete(
        self,
        req: falcon.Request,
        resp: falcon.Response,
        container_id: str,
    ) -> None:
        """Delegate to ContainerDetailResource (admin: delete container)."""
        from backend.resources.containers import ContainerDetailResource
        return ContainerDetailResource().on_delete(req, resp, container_id)
