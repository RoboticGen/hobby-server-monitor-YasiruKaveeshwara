"""
Authentication route resources.

Handles the Google OAuth2 login flow, session management, and current-user
queries. These four endpoints are the only auth-related entry points:

  GET  /api/auth/google/login    → redirect to Google
  GET  /api/auth/google/callback → exchange code, set session cookies
  GET  /api/auth/me              → return current user info or 401
  POST /api/auth/logout          → revoke session, clear cookies
"""

from datetime import datetime, timedelta, timezone

import falcon

from backend.auth.jwt_utils import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_refresh_token,
)
from backend.auth.oauth import (
    build_google_auth_url,
    exchange_code_for_tokens,
    verify_and_decode_id_token,
)
from backend.config import config
from backend.db import repo

# Refresh tokens last 7 days before the user must re-authenticate.
_REFRESH_TOKEN_LIFETIME_DAYS = 7

# Cookie names used for the access token (short-lived JWT) and refresh
# token (opaque, hashed server-side).
_ACCESS_COOKIE = "access_token"
_REFRESH_COOKIE = "refresh_token"


def _set_session_cookies(
    resp: falcon.Response,
    access_token: str,
    refresh_token: str,
) -> None:
    """Set both session cookies on the response with consistent security settings.

    Both cookies are httpOnly (inaccessible to JavaScript, preventing XSS
    from stealing them), SameSite=Lax (sent on top-level navigations but
    not on cross-origin subrequests), and Secure only when configured
    (production requires HTTPS, local dev runs over HTTP).
    """
    resp.set_cookie(
        _ACCESS_COOKIE,
        access_token,
        max_age=900,  # 15 minutes, matching the JWT's own expiry
        secure=config.session_cookie_secure,
        http_only=True,
        same_site="Lax",
        path="/",
    )
    resp.set_cookie(
        _REFRESH_COOKIE,
        refresh_token,
        max_age=_REFRESH_TOKEN_LIFETIME_DAYS * 86400,  # 7 days in seconds
        secure=config.session_cookie_secure,
        http_only=True,
        same_site="Lax",
        path="/",
    )


def _clear_session_cookies(resp: falcon.Response) -> None:
    """Remove both session cookies by setting them to expire immediately."""
    resp.unset_cookie(_ACCESS_COOKIE, samesite="Lax")
    resp.unset_cookie(_REFRESH_COOKIE, samesite="Lax")


class GoogleLoginResource:
    """Redirects the browser to Google's OAuth2 consent screen.

    This is a plain navigation (not a fetch/XHR), because the browser
    must leave our domain entirely to visit Google's login page.
    """

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        """Issue a 302 redirect to Google's authorization URL."""
        raise falcon.HTTPFound(build_google_auth_url())


class GoogleCallbackResource:
    """Handles the OAuth2 callback after Google redirects back to us.

    Exchanges the authorization code for tokens, verifies the ID token
    to extract the user's email, then either creates or looks up the
    user in our database, issues session cookies, and redirects to
    the frontend.
    """

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        """Process the Google OAuth callback and establish a session."""
        code = req.get_param("code")
        if not code:
            raise falcon.HTTPBadRequest(
                title="Missing authorization code",
                description="Google did not return an authorization code. "
                "Please try signing in again.",
            )

        # Exchange the authorization code for tokens from Google
        try:
            tokens = exchange_code_for_tokens(code)
        except Exception:
            raise falcon.HTTPBadRequest(
                title="Token exchange failed",
                description="Could not exchange the authorization code. "
                "It may have expired — please try signing in again.",
            )

        # Verify the ID token and extract the user's verified email
        try:
            claims = verify_and_decode_id_token(tokens["id_token"])
        except (ValueError, KeyError):
            raise falcon.HTTPBadRequest(
                title="Invalid ID token",
                description="The token from Google could not be verified.",
            )

        email = claims.get("email", "").lower().strip()
        if not email:
            raise falcon.HTTPBadRequest(
                title="No email in token",
                description="Google did not provide an email address.",
            )

        # Look the email up in our users table
        user = repo.get_user_by_email(email)

        if user is None:
            # -----------------------------------------------------------------
            # Admin bootstrap: only the email configured in
            # ADMIN_BOOTSTRAP_EMAIL is auto-created as an admin on first
            # sign-in. Every other unknown email is rejected with a clear
            # "not invited" message. This prevents the "first visitor wins"
            # race condition that a "first-sign-in-becomes-admin" strategy
            # would have.
            # -----------------------------------------------------------------
            if email == config.admin_bootstrap_email.lower().strip():
                user_id = repo.create_user(
                    email=email,
                    role="admin",
                    status="active",
                    quota_ram_mb=0,
                    quota_cpu=0.0,
                    quota_disk_gb=0,
                )
                user = repo.get_user_by_id(user_id)
                repo.write_audit_log(
                    user_id=user_id,
                    action="auth.bootstrap_admin",
                    target=None,
                    detail=f"Bootstrap admin created for {email}",
                )
            else:
                raise falcon.HTTPForbidden(
                    title="Not invited",
                    description=f"The email '{email}' has not been invited "
                    "to this server. Ask an admin to invite you first.",
                )

        # Reject revoked users with a clear message
        if user["status"] == "revoked":
            raise falcon.HTTPForbidden(
                title="Access revoked",
                description="Your access has been revoked by an admin.",
            )

        # If user status is "invited", upgrade to "active" on first sign-in
        if user["status"] == "invited":
            repo.update_user(user["id"], status="active")
            user["status"] = "active"

        # Issue tokens and create a server-side session
        access_token = create_access_token(user["id"], user["role"])
        refresh_token = generate_refresh_token()
        refresh_hash = hash_refresh_token(refresh_token)
        expires_at = (
            datetime.now(timezone.utc)
            + timedelta(days=_REFRESH_TOKEN_LIFETIME_DAYS)
        ).isoformat()

        repo.create_session(
            user_id=user["id"],
            refresh_token_hash=refresh_hash,
            expires_at=expires_at,
        )

        # Set session cookies and redirect to the frontend
        _set_session_cookies(resp, access_token, refresh_token)
        raise falcon.HTTPFound(config.frontend_origin)


class MeResource:
    """Returns the current authenticated user's profile information.

    Reads user identity from req.context.user. If the middleware hasn't run or the user isn't authenticated, falls back to decoding the access_token cookie
    directly so this endpoint works before the middleware is wired in.
    """

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        """Return the current user's id, email, role, and status as JSON."""
        # Prefer req.context.user if set by middleware (Phase 5.4+)
        user_ctx = getattr(req.context, "user", None)

        if user_ctx is None:
            # Fallback: decode the access token cookie directly
            access_token = req.cookies.get(_ACCESS_COOKIE)
            if not access_token:
                raise falcon.HTTPUnauthorized(
                    title="Not authenticated",
                    description="No valid session. Please sign in.",
                )
            payload = decode_access_token(access_token)
            if payload is None:
                raise falcon.HTTPUnauthorized(
                    title="Invalid or expired token",
                    description="Your session has expired. Please sign in again.",
                )
            user_ctx = {"id": payload["user_id"], "role": payload["role"]}

        # Fetch the full user record from the database
        user = repo.get_user_by_id(user_ctx["id"])
        if user is None:
            raise falcon.HTTPUnauthorized(
                title="User not found",
                description="Your account no longer exists.",
            )

        resp.media = {
            "id": user["id"],
            "email": user["email"],
            "role": user["role"],
            "status": user["status"],
        }


class LogoutResource:
    """Revokes the current session server-side and clears session cookies.

    Logout means the session row in the database is deleted — not just
    the client-side cookie. This ensures that even if someone captured
    the cookie value before logout, it's no longer valid server-side.
    """

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        """Delete the server-side session and clear both cookies."""
        refresh_token = req.cookies.get(_REFRESH_COOKIE)
        if refresh_token:
            # Look up and delete the session by the refresh token's hash
            refresh_hash = hash_refresh_token(refresh_token)
            session = repo.get_session_by_hash(refresh_hash)
            if session:
                repo.delete_session(session["id"])

        _clear_session_cookies(resp)
        resp.media = {"message": "Logged out successfully"}
