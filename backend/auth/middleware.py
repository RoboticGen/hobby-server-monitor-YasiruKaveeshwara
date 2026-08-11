"""
Central authorization middleware and access-checking helpers.

The AuthMiddleware runs on every incoming request, decoding the access
token cookie and attaching the user's identity to the request context.
It does NOT reject unauthenticated requests itself — individual resources
call the helper functions below to enforce their own auth requirements.
This separation keeps public routes (health, login) working without
special exemption logic in the middleware.

The helper functions (require_role, require_container_access) are the
actual enforcement points that resource methods call at the top of their
handlers. The "every route requires auth" integration test (Step 5.4)
is what ensures no new endpoint silently skips these calls.
"""

import falcon

from backend.auth.jwt_utils import decode_access_token
from backend.db import repo

# Cookie name matching the one set in resources/auth.py
_ACCESS_COOKIE = "access_token"


class AuthMiddleware:
    """Falcon middleware that attaches user identity to every request.

    Reads the access_token cookie, decodes the JWT, and sets
    req.context.user to {"id": ..., "role": ...} if valid, or None
    if the cookie is missing, expired, or tampered with.

    Does NOT raise 401 here — that's the job of require_role() and
    require_container_access() in each resource method that needs auth.
    Public endpoints (health, login, callback) simply never call those
    helpers and work fine with req.context.user = None.
    """

    def process_resource(
        self,
        req: falcon.Request,
        resp: falcon.Response,
        resource: object,
        params: dict,
    ) -> None:
        """Decode the access token cookie and attach user context."""
        req.context.user = None

        access_token = req.cookies.get(_ACCESS_COOKIE)
        if not access_token:
            return

        payload = decode_access_token(access_token)
        if payload is None:
            return

        # Attach minimal identity info — full user record lookups happen
        # in the resource methods that need them (e.g., MeResource).
        req.context.user = {
            "id": payload["user_id"],
            "role": payload["role"],
        }


def require_role(req: falcon.Request, role: str) -> None:
    """Enforce that the current request is from an authenticated user
    with the specified role.

    Raises HTTPUnauthorized (401) if no user is authenticated.
    Raises HTTPForbidden (403) if the user's role doesn't match.

    Call this at the very top of any resource method that requires
    a specific role (typically "admin").
    """
    if req.context.user is None:
        raise falcon.HTTPUnauthorized(
            title="Not authenticated",
            description="This endpoint requires authentication. "
            "Please sign in.",
        )
    if req.context.user["role"] != role:
        raise falcon.HTTPForbidden(
            title="Insufficient permissions",
            description=f"This action requires the '{role}' role.",
        )


# require_container_access re-checks the caller's access to the specific
# container on EVERY call — the result is never cached. This is critical
# for the terminal exec endpoint (Phase 12): if an admin revokes a user's
# access mid-session, the very next exec call must be rejected, not served
# from a stale cached "yes they had access a minute ago".
def require_container_access(
    req: falcon.Request, container_id: str
) -> None:
    """Enforce that the current user may access a specific container.

    Admins can access any container. Regular users must have an active
    assignment for the specific container_id.

    Raises HTTPUnauthorized (401) if no user is authenticated.
    Raises HTTPForbidden (403) if the user is not an admin and does
    not have an active assignment to this container.
    """
    if req.context.user is None:
        raise falcon.HTTPUnauthorized(
            title="Not authenticated",
            description="This endpoint requires authentication. "
            "Please sign in.",
        )

    # Admins bypass per-container access checks
    if req.context.user["role"] == "admin":
        return

    # For regular users, check the assignments table on every call
    if not repo.user_has_access(req.context.user["id"], container_id):
        raise falcon.HTTPForbidden(
            title="Access denied",
            description="You do not have access to this container. "
            "Ask an admin to grant you access.",
        )
