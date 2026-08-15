"""
Refresh-token redemption, tested from the seat of an expired access token.

The refresh token was created, hashed, and stored at login long before any
endpoint could redeem it, so the session died at the access token's 30-minute
expiry no matter what the sessions row said. These tests pin the redemption
path and — more importantly — the four ways it must refuse, because a refresh
endpoint that is lax about *when* it mints a token is a 7-day bypass of both
logout and revocation.

The access token deliberately never appears in these requests: refresh must
work from the refresh cookie alone, which is the whole point of it existing
once the access token is gone.
"""

from datetime import datetime, timedelta, timezone
import re

import pytest
from falcon import testing

from backend.app import create_app
from backend.auth.jwt_utils import (
    decode_access_token,
    generate_refresh_token,
    hash_refresh_token,
)
from backend.db import repo

from .conftest import make_user

pytestmark = pytest.mark.usefixtures("isolated_db")


def _issue_session(
    email: str, role: str = "user", status: str = "active", lifetime_days: int = 7
) -> tuple[str, str, str]:
    """Create a user plus a live session, returning (user_id, raw_token, sid).

    Built through the repo rather than by driving the OAuth callback, because
    the callback needs a live Google to reach. The rows this produces are the
    same ones the callback writes.
    """
    user_id = repo.create_user(
        email=email,
        role=role,
        status=status,
        quota_ram_mb=4096,
        quota_cpu=4.0,
        quota_disk_gb=40,
    )
    raw = generate_refresh_token()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=lifetime_days)
    ).isoformat()
    sid = repo.create_session(
        user_id=user_id,
        refresh_token_hash=hash_refresh_token(raw),
        expires_at=expires_at,
    )
    return user_id, raw, sid


def _refresh_cookie(raw_token: str) -> dict:
    """Headers carrying only the refresh cookie — no access token."""
    return {"Cookie": f"refresh_token={raw_token}"}


def _access_token_from(result) -> str | None:
    """Pull the access_token value out of the response's Set-Cookie headers."""
    for name, value in result.headers.items():
        if name.lower() == "set-cookie" and "access_token=" in value:
            match = re.search(r"access_token=([^;,\s]+)", value)
            if match and match.group(1) not in ("", '""'):
                return match.group(1)
    # Falcon's test client collapses repeated headers; fall back to the cookie
    # jar it parses for us.
    cookie = result.cookies.get("access_token")
    return cookie.value if cookie and cookie.value else None


class TestSuccessfulRefresh:
    """A live session must yield a usable access token."""

    def test_refresh_issues_a_new_access_cookie(self, client):
        _, raw, _ = _issue_session("refresh-ok@example.com")

        result = client.simulate_post("/api/auth/refresh", headers=_refresh_cookie(raw))

        assert result.status_code == 200, result.text
        assert (
            _access_token_from(result) is not None
        ), "refresh returned 200 but set no access_token cookie"

    def test_the_new_token_actually_authenticates(self, client):
        """The real test of a refresh: the token it mints must work.

        A 200 with a malformed or wrongly-signed token would look identical to
        success at the HTTP layer, so the token is spent on a protected route.
        """
        _, raw, _ = _issue_session("refresh-usable@example.com", role="admin")

        refreshed = client.simulate_post(
            "/api/auth/refresh", headers=_refresh_cookie(raw)
        )
        token = _access_token_from(refreshed)
        assert token is not None

        me = client.simulate_get(
            "/api/auth/me", headers={"Cookie": f"access_token={token}"}
        )
        assert (
            me.status_code == 200
        ), f"the freshly minted token was rejected: {me.text}"
        assert me.json["email"] == "refresh-usable@example.com"

    def test_minted_token_carries_the_current_role_not_the_stale_one(self, client):
        """Role is re-read at refresh, so a demotion takes effect immediately.

        This is the security-relevant half: if the role were copied from the
        old token, demoting an admin would leave them admin for up to the full
        refresh lifetime.
        """
        user_id, raw, _ = _issue_session("refresh-demoted@example.com", role="admin")
        repo.update_user(user_id, role="user")

        result = client.simulate_post("/api/auth/refresh", headers=_refresh_cookie(raw))
        assert result.status_code == 200

        payload = decode_access_token(_access_token_from(result))
        assert payload is not None
        assert (
            payload["role"] == "user"
        ), "refresh reissued the stale admin role after a demotion"

    def test_refresh_does_not_rotate_the_refresh_token(self, client):
        """Documented behaviour: the refresh cookie is reusable across calls.

        Pinned deliberately. If rotation is added later this test should fail
        and be replaced, rather than the change landing unnoticed and breaking
        concurrent tabs.
        """
        _, raw, _ = _issue_session("refresh-norotate@example.com")

        first = client.simulate_post("/api/auth/refresh", headers=_refresh_cookie(raw))
        second = client.simulate_post("/api/auth/refresh", headers=_refresh_cookie(raw))

        assert first.status_code == 200
        assert second.status_code == 200, "the same refresh token stopped working"

    def test_repeated_refresh_does_not_accumulate_sessions(self, client):
        """Reissuing an access token must not create session rows.

        Counted with a direct query rather than through a repo helper, because
        no repo function lists a user's sessions — guarding on hasattr would
        make this test silently vacuous, which is worse than not having it.
        """
        user_id, raw, _ = _issue_session("refresh-nogrow@example.com")

        def session_count() -> int:
            conn = repo.get_connection()
            try:
                return conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,)
                ).fetchone()[0]
            finally:
                conn.close()

        before = session_count()
        assert before == 1, f"fixture should leave exactly one session, got {before}"

        for _ in range(3):
            assert (
                client.simulate_post(
                    "/api/auth/refresh", headers=_refresh_cookie(raw)
                ).status_code
                == 200
            )

        after = session_count()
        assert after == before, f"session rows grew {before} -> {after}"


class TestRefreshRefusals:
    """Every way the endpoint must say no."""

    def test_no_cookie_is_401(self, client):
        result = client.simulate_post("/api/auth/refresh")
        assert result.status_code == 401

    def test_forged_token_is_401(self, client):
        """A token that hashes to nothing on file must not mint anything."""
        result = client.simulate_post(
            "/api/auth/refresh",
            headers=_refresh_cookie("not-a-real-token-" + "a" * 40),
        )
        assert result.status_code == 401
        assert _access_token_from(result) is None

    def test_expired_session_is_401_and_is_deleted(self, client):
        """Expiry is enforced in Python — no SQL predicate filters on it.

        Without that check the sessions row outlives its own expires_at and
        keeps minting access tokens, making the 7-day lifetime decorative.
        """
        _, raw, sid = _issue_session("refresh-expired@example.com", lifetime_days=-1)

        result = client.simulate_post("/api/auth/refresh", headers=_refresh_cookie(raw))

        assert result.status_code == 401
        assert _access_token_from(result) is None
        assert (
            repo.get_session_by_hash(hash_refresh_token(raw)) is None
        ), "the expired session row survived the refusal"

    def test_logged_out_session_cannot_be_refreshed(self, client):
        """Logout's whole promise: the refresh token dies with the row.

        If this failed, logout would be cosmetic — the cleared cookie would be
        gone from the browser but the token would still be redeemable for a
        week by anyone who had captured it.
        """
        _, raw, sid = _issue_session("refresh-loggedout@example.com")
        repo.delete_session(sid)

        result = client.simulate_post("/api/auth/refresh", headers=_refresh_cookie(raw))
        assert result.status_code == 401
        assert _access_token_from(result) is None

    def test_revoked_user_cannot_refresh(self, client):
        """Status is re-read too, so revocation is not deferred 7 days."""
        user_id, raw, _ = _issue_session("refresh-revoked@example.com")
        repo.update_user(user_id, status="revoked")

        result = client.simulate_post("/api/auth/refresh", headers=_refresh_cookie(raw))

        assert result.status_code == 401
        assert _access_token_from(result) is None
        assert (
            repo.get_session_by_hash(hash_refresh_token(raw)) is None
        ), "a revoked user's session was left redeemable"

    def test_unparseable_expiry_fails_closed(self, client):
        """A corrupt timestamp must read as expired, not as valid-forever.

        This is the one case where guessing wrong grants indefinite access.
        """
        user_id = repo.create_user(
            email="refresh-corrupt@example.com",
            role="user",
            status="active",
            quota_ram_mb=0,
            quota_cpu=0.0,
            quota_disk_gb=0,
        )
        raw = generate_refresh_token()
        repo.create_session(
            user_id=user_id,
            refresh_token_hash=hash_refresh_token(raw),
            expires_at="not-a-timestamp",
        )

        result = client.simulate_post("/api/auth/refresh", headers=_refresh_cookie(raw))
        assert result.status_code == 401
        assert _access_token_from(result) is None

    def test_naive_expiry_timestamp_does_not_500(self, client):
        """A naive stored timestamp must compare cleanly, not raise TypeError.

        Comparing an aware `now` against a naive value raises, which would
        surface as a 500 on the auth path instead of a clean decision.
        """
        user_id = repo.create_user(
            email="refresh-naive@example.com",
            role="user",
            status="active",
            quota_ram_mb=0,
            quota_cpu=0.0,
            quota_disk_gb=0,
        )
        raw = generate_refresh_token()
        naive_future = (
            (datetime.now(timezone.utc) + timedelta(days=3))
            .replace(tzinfo=None)
            .isoformat()
        )
        repo.create_session(
            user_id=user_id,
            refresh_token_hash=hash_refresh_token(raw),
            expires_at=naive_future,
        )

        result = client.simulate_post("/api/auth/refresh", headers=_refresh_cookie(raw))
        assert (
            result.status_code == 200
        ), f"a naive future expiry was not handled: {result.status_code}"

    def test_refresh_token_is_not_accepted_as_an_access_token(self, client):
        """The two cookies are not interchangeable.

        The refresh token is opaque and unsigned, so if any code path treated
        it as a JWT it would have to fail — this asserts the boundary holds.
        """
        _, raw, _ = _issue_session("refresh-notaccess@example.com")

        result = client.simulate_get(
            "/api/auth/me", headers={"Cookie": f"access_token={raw}"}
        )
        assert result.status_code == 401


class TestRefreshIsNotABypass:
    """The endpoint must not become a way around normal authorization."""

    def test_refresh_grants_no_privileges_by_itself(self, client):
        """A refreshed user token still cannot reach an admin-only route."""
        _, raw, _ = _issue_session("refresh-noescalate@example.com", role="user")

        refreshed = client.simulate_post(
            "/api/auth/refresh", headers=_refresh_cookie(raw)
        )
        token = _access_token_from(refreshed)
        assert token is not None

        result = client.simulate_get(
            "/api/users", headers={"Cookie": f"access_token={token}"}
        )
        assert (
            result.status_code == 403
        ), f"a refreshed user token reached an admin route ({result.status_code})"

    def test_get_is_not_allowed(self, client):
        """Refresh is a POST-only state change; a GET must not perform it.

        A GET-able refresh is reachable by top-level navigation and image tags,
        which is exactly the shape SameSite=Lax does not protect against.
        """
        _, raw, _ = _issue_session("refresh-getonly@example.com")
        result = client.simulate_get("/api/auth/refresh", headers=_refresh_cookie(raw))
        assert result.status_code == 405
