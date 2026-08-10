"""
User management endpoints (admin-only).

Provides invite (POST), list (GET), and update (PATCH) operations for
user accounts. All operations are restricted to admins and write audit
log entries for accountability.
"""

import falcon

from backend.auth.middleware import require_role
from backend.db import repo
from backend.lxd.quota import compute_user_allocation


class UserListResource:
    """Handles GET (list all users) and POST (invite a new user).

    Both operations are admin-only. GET enriches each user record with
    their current resource allocation so the admin dashboard can show
    usage at a glance.
    """

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        """Return all users with their current resource allocations."""
        require_role(req, "admin")

        users = repo.list_users()
        enriched = []
        for user in users:
            user_dict = dict(user)
            # Append current allocation so the admin can see usage at a glance
            user_dict["allocation"] = compute_user_allocation(user["id"])
            enriched.append(user_dict)

        resp.media = {"users": enriched}

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        """Invite a new user by creating them with status='invited'.

        Expected JSON body:
        {
            "email": "newuser@example.com",
            "role": "user",
            "quota_ram_mb": 2048,
            "quota_cpu": 2.0,
            "quota_disk_gb": 20
        }

        The invited user does NOT receive an email 
        (only Google OAuth is used for authentication — the invited user simply
        needs to sign in with their Google account using the same email
        address, and the OAuth callback will recognize them and upgrade
        their status from 'invited' to 'active' on first login).
        """
        require_role(req, "admin")

        body = req.get_media()

        email = body.get("email", "").strip().lower()
        if not email or "@" not in email:
            raise falcon.HTTPBadRequest(
                title="Invalid email",
                description="A valid email address is required.",
            )

        # Check if user already exists
        if repo.get_user_by_email(email):
            raise falcon.HTTPConflict(
                title="User already exists",
                description=f"A user with email '{email}' already exists.",
            )

        role = body.get("role", "user")
        if role not in ("admin", "user"):
            raise falcon.HTTPBadRequest(
                title="Invalid role",
                description="Role must be 'admin' or 'user'.",
            )

        quota_ram_mb = int(body.get("quota_ram_mb", 0))
        quota_cpu = float(body.get("quota_cpu", 0.0))
        quota_disk_gb = int(body.get("quota_disk_gb", 0))

        user_id = repo.create_user(
            email=email,
            role=role,
            status="invited",
            quota_ram_mb=quota_ram_mb,
            quota_cpu=quota_cpu,
            quota_disk_gb=quota_disk_gb,
        )

        # Audit trail for user invitation
        repo.write_audit_log(
            user_id=req.context.user["id"],
            action="user.invite",
            target=user_id,
            detail=f"Invited {email} as {role}",
        )

        resp.status = falcon.HTTP_201
        resp.media = {
            "id": user_id,
            "email": email,
            "role": role,
            "status": "invited",
        }


class UserDetailResource:
    """Handles PATCH for a single user (admin-only).

    Allows updating role, status, and quota fields. Validates that
    demoting the last admin is not allowed — there must always be at
    least one admin in the system.
    """

    def on_patch(
        self,
        req: falcon.Request,
        resp: falcon.Response,
        user_id: str,
    ) -> None:
        """Update a user's role, status, or quotas (admin only)."""
        require_role(req, "admin")

        user = repo.get_user_by_id(user_id)
        if not user:
            raise falcon.HTTPNotFound(
                title="User not found",
                description=f"No user with id '{user_id}'.",
            )

        body = req.get_media()

        # Extract only the fields that are allowed to be updated
        updates = {}
        if "role" in body:
            if body["role"] not in ("admin", "user"):
                raise falcon.HTTPBadRequest(
                    title="Invalid role",
                    description="Role must be 'admin' or 'user'.",
                )
            updates["role"] = body["role"]

        if "status" in body:
            if body["status"] not in ("invited", "active", "revoked"):
                raise falcon.HTTPBadRequest(
                    title="Invalid status",
                    description="Status must be 'invited', 'active', or 'revoked'.",
                )
            updates["status"] = body["status"]

        for field in ("quota_ram_mb", "quota_cpu", "quota_disk_gb"):
            if field in body:
                updates[field] = body[field]

        if not updates:
            raise falcon.HTTPBadRequest(
                title="Nothing to update",
                description="Provide at least one field to update.",
            )

        # Safety check: prevent demoting the last admin, which would
        # lock everyone out of admin operations permanently.
        # We count OTHER active admins (excluding the one being demoted).
        # If there are none, this demotion would remove the last admin.
        if updates.get("role") == "user" and user["role"] == "admin":
            other_active_admins = sum(
                1 for u in repo.list_users()
                if u["id"] != user_id
                and u["role"] == "admin"
                and u["status"] == "active"
            )
            if other_active_admins == 0:
                raise falcon.HTTPBadRequest(
                    title="Cannot remove last admin",
                    description="At least one active admin must remain. "
                    "Promote another user to admin first.",
                )

        repo.update_user(user_id, **updates)

        # Audit trail for user updates
        repo.write_audit_log(
            user_id=req.context.user["id"],
            action="user.update",
            target=user_id,
            detail=f"Updated user {user['email']}: {updates}",
        )

        updated_user = repo.get_user_by_id(user_id)
        resp.media = {
            "id": updated_user["id"],
            "email": updated_user["email"],
            "role": updated_user["role"],
            "status": updated_user["status"],
            "quota_ram_mb": updated_user["quota_ram_mb"],
            "quota_cpu": updated_user["quota_cpu"],
            "quota_disk_gb": updated_user["quota_disk_gb"],
        }
