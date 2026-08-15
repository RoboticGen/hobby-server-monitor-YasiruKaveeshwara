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
import logging

import falcon

from backend.auth.jwt_utils import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_refresh_token,
)
from backend.auth.oauth import (
    TokenExchangeError,
    build_google_auth_url,
    exchange_code_for_tokens,
    verify_and_decode_id_token,
)
from backend.config import config
from backend.db import repo
from backend.lxd.quota import compute_user_allocation

# Module-level logger, matching the collector's convention. The OAuth failure
# paths below are the only place in this app where the cause of a failure is
# known solely to the server: the browser is mid-redirect from Google and
# cannot be shown provider detail, so without a log line the reason is lost.
log = logging.getLogger(__name__)

# Refresh tokens last 7 days before the user must re-authenticate.
_REFRESH_TOKEN_LIFETIME_DAYS = 7

# Cookie names used for the access token (short-lived JWT) and refresh
# token (opaque, hashed server-side).
_ACCESS_COOKIE = "access_token"
_REFRESH_COOKIE = "refresh_token"


def _session_is_expired(expires_at: str) -> bool:
    """Return True if an ISO-8601 session expiry has passed.

    Fails closed: an unparseable or empty value is treated as expired rather
    than as valid-forever. A corrupt timestamp is the one case where guessing
    wrong grants indefinite access, so the safe direction is to refuse.

    Naive timestamps are read as UTC. Every writer in this codebase stores an
    aware UTC value, but comparing an aware `now` against a naive stored value
    raises TypeError, which would surface as a 500 on the refresh path instead
    of a clean 401.
    """
    try:
        expiry = datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        log.warning(
            "Session expires_at is unparseable (%r); treating as expired", expires_at
        )
        return True

    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)

    return expiry <= datetime.now(timezone.utc)


def _set_access_cookie(resp: falcon.Response, access_token: str) -> None:
    """Set only the access-token cookie.

    Split out from _set_session_cookies because the refresh endpoint reissues
    the access token without touching the refresh token. Keeping the attribute
    list in one function means the cookie written at refresh time cannot drift
    from the one written at login — a mismatch in `secure` or `same_site`
    would silently produce a cookie the browser stops sending, which presents
    as a random logout rather than as a bug.
    """
    resp.set_cookie(
        _ACCESS_COOKIE,
        access_token,
        max_age=config.access_token_lifetime_minutes * 60,
        secure=config.session_cookie_secure,
        http_only=True,
        same_site="Lax",
        path="/",
    )


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
    _set_access_cookie(resp, access_token)
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
    resp.unset_cookie(_ACCESS_COOKIE, path="/", same_site="Lax")
    resp.unset_cookie(_REFRESH_COOKIE, path="/", same_site="Lax")


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

        # Exchange the authorization code for tokens from Google.
        #
        # The failure is logged with Google's own error code because that code
        # is the diagnosis: 'invalid_client' means the client secret is wrong,
        # 'invalid_grant' means the code was already redeemed or has expired
        # (reloading this callback URL does exactly that), 'network_error'
        # means the request never left the host. The browser only gets the
        # code, not Google's full description, which keeps provider detail in
        # the log where it belongs.
        try:
            tokens = exchange_code_for_tokens(code)
        except TokenExchangeError as exc:
            log.error(
                "Google token exchange failed: error=%s http_status=%s detail=%s",
                exc.error,
                exc.status,
                exc.description,
            )
            raise falcon.HTTPBadRequest(
                title="Token exchange failed",
                description=f"Google rejected the authorization code "
                f"({exc.error}). Check the backend log for details, then "
                "try signing in again.",
            )

        # Google only returns an ID token when the 'openid' scope was granted.
        # Checked explicitly rather than letting the subscript below raise a
        # KeyError, so a missing token is reported as the distinct problem it
        # is instead of being blamed on verification.
        if "id_token" not in tokens:
            log.error(
                "Google token response contained no id_token: keys=%s",
                sorted(tokens),
            )
            raise falcon.HTTPBadRequest(
                title="Invalid token response",
                description="Google's response did not include an ID token. "
                "Check that the 'openid' scope is being requested.",
            )

        # Verify the ID token and extract the user's verified email.
        #
        # google-auth's message is echoed to the browser as well as logged,
        # because it names the cause precisely ("Token used too early …",
        # "Token has wrong audience …") and none of those messages carry a
        # secret — the client ID they may mention is already visible in the
        # browser's own address bar during this flow.
        try:
            claims = verify_and_decode_id_token(tokens["id_token"])
        except ValueError as exc:
            log.error("Google ID token verification failed: %s", exc)
            raise falcon.HTTPBadRequest(
                title="Invalid ID token",
                description=f"The token from Google could not be verified: "
                f"{str(exc)[:300]}",
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
            datetime.now(timezone.utc) + timedelta(days=_REFRESH_TOKEN_LIFETIME_DAYS)
        ).isoformat()

        repo.create_session(
            user_id=user["id"],
            refresh_token_hash=refresh_hash,
            expires_at=expires_at,
        )

        # Set session cookies and redirect to the frontend
        _set_session_cookies(resp, access_token, refresh_token)
        raise falcon.HTTPFound(config.frontend_origin)


class RefreshResource:
    """Exchanges a valid refresh token for a new access token.

    Without this endpoint the refresh token created at login was stored,
    hashed, and cleared on logout but never redeemed, so every session died
    hard at the access token's 30-minute expiry regardless of the 7-day
    refresh lifetime — mid-task, with no warning.

    The refresh token itself is deliberately *not* rotated on use. Rotation
    is the stronger design (a stolen token is single-use), but it needs
    replay detection to be safe: two tabs refreshing at once would each
    present the same valid token, and whichever lost the race would be
    logged out by its own sibling. Detecting that properly means a used-token
    grace window and a family-wide revocation rule, which is more machinery
    than a hobby monitor warrants. The token stays server-side revocable via
    the sessions row, which is what makes logout definitive, and that is the
    property being traded for.
    """

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        """Issue a fresh access token if the refresh cookie names a live session."""
        refresh_token = req.cookies.get(_REFRESH_COOKIE)
        if not refresh_token:
            raise falcon.HTTPUnauthorized(
                title="Not authenticated",
                description="No refresh token. Please sign in.",
            )

        # Only the hash is ever stored, so the lookup hashes the presented
        # token rather than comparing raw values.
        session = repo.get_session_by_hash(hash_refresh_token(refresh_token))
        if session is None:
            # Covers both a forged token and one whose session was deleted by
            # logout. Cookies are cleared so a stale token stops being resent
            # on every subsequent request.
            _clear_session_cookies(resp)
            raise falcon.HTTPUnauthorized(
                title="Invalid session",
                description="Your session is no longer valid. Please sign in " "again.",
            )

        # expires_at is stored as ISO text and no SQL predicate filters on it,
        # so expiry is enforced here. Without this check the 7-day lifetime
        # would be decorative: the row outlives its own expiry and would keep
        # minting access tokens indefinitely.
        if _session_is_expired(session["expires_at"]):
            repo.delete_session(session["id"])
            _clear_session_cookies(resp)
            raise falcon.HTTPUnauthorized(
                title="Session expired",
                description="Your session has expired. Please sign in again.",
            )

        # Re-read the user on every refresh instead of trusting the role
        # embedded in the expiring token. This is the point at which an admin's
        # demotion or revocation actually takes effect: a stale role would
        # otherwise survive for the full refresh lifetime, so revoking someone
        # would not log them out for up to 7 days.
        user = repo.get_user_by_id(session["user_id"])
        if user is None or user["status"] != "active":
            repo.delete_session(session["id"])
            _clear_session_cookies(resp)
            raise falcon.HTTPUnauthorized(
                title="Account unavailable",
                description="Your account is no longer active. Please contact "
                "an admin.",
            )

        _set_access_cookie(resp, create_access_token(user["id"], user["role"]))
        resp.media = {
            "id": user["id"],
            "email": user["email"],
            "role": user["role"],
            "status": user["status"],
        }


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

        allocation = compute_user_allocation(user["id"])

        resp.media = {
            "id": user["id"],
            "email": user["email"],
            "role": user["role"],
            "status": user["status"],
            "quota_ram_mb": user["quota_ram_mb"],
            "quota_cpu": user["quota_cpu"],
            "quota_disk_gb": user["quota_disk_gb"],
            "allocation": allocation,
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

        user_ctx = getattr(req.context, "user", None)
        if user_ctx and user_ctx.get("id"):
            repo.delete_sessions_for_user(user_ctx["id"])

        _clear_session_cookies(resp)
        resp.media = {"message": "Logged out successfully"}
