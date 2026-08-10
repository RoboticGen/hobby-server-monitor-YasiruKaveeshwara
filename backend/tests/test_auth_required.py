"""
Tests for authentication-related code.

Started with a basic test for the OAuth URL builder.
Extended with JWT round-trip tests.
Extended with the "every route requires auth" test.
"""

from backend.auth.oauth import build_google_auth_url
from backend.auth.jwt_utils import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_refresh_token,
)
from backend.config import config


class TestBuildGoogleAuthUrl:
    """Tests for the Google OAuth authorization URL builder."""

    def test_url_contains_client_id(self):
        """The generated URL must include our configured client ID."""
        url = build_google_auth_url()
        assert config.google_client_id in url

    def test_url_contains_scopes(self):
        """The URL must request the openid, email, and profile scopes."""
        url = build_google_auth_url()
        # Scopes are URL-encoded as "openid+email+profile" or "openid%20email%20profile"
        assert "openid" in url
        assert "email" in url
        assert "profile" in url

    def test_url_contains_redirect_uri(self):
        """The URL must include our configured redirect URI."""
        url = build_google_auth_url()
        # The redirect URI is URL-encoded, but the host portion should appear
        assert "callback" in url

    def test_url_starts_with_google_endpoint(self):
        """The URL should point to Google's OAuth2 authorization endpoint."""
        url = build_google_auth_url()
        assert url.startswith("https://accounts.google.com/")


class TestJWTRoundTrip:
    """Tests for JWT access token creation and decoding."""

    def test_create_and_decode_round_trip(self):
        """Creating a token and immediately decoding it returns the
        original user_id and role."""
        token = create_access_token(user_id="test-uuid-123", role="admin")
        payload = decode_access_token(token)

        assert payload is not None
        assert payload["user_id"] == "test-uuid-123"
        assert payload["role"] == "admin"

    def test_expired_token_returns_none(self):
        """A token with an expired timestamp must return None, not raise."""
        import jwt as pyjwt
        from datetime import datetime, timedelta, timezone

        # Manually create a token that expired 1 hour ago
        now = datetime.now(timezone.utc)
        payload = {
            "user_id": "test-uuid-123",
            "role": "admin",
            "exp": now - timedelta(hours=1),
            "iat": now - timedelta(hours=2),
        }
        expired_token = pyjwt.encode(
            payload, config.jwt_secret, algorithm="HS256"
        )

        result = decode_access_token(expired_token)
        assert result is None

    def test_tampered_token_returns_none(self):
        """A token signed with the wrong secret must return None."""
        import jwt as pyjwt
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        payload = {
            "user_id": "test-uuid-123",
            "role": "admin",
            "exp": now + timedelta(hours=1),
            "iat": now,
        }
        bad_token = pyjwt.encode(payload, "wrong-secret", algorithm="HS256")

        result = decode_access_token(bad_token)
        assert result is None


class TestRefreshToken:
    """Tests for refresh token generation and hashing."""

    def test_generate_produces_nonempty_string(self):
        """generate_refresh_token returns a non-empty string."""
        token = generate_refresh_token()
        assert isinstance(token, str)
        assert len(token) > 0

    def test_generate_produces_unique_tokens(self):
        """Two calls should produce different tokens (with overwhelming
        probability given 32 bytes of entropy)."""
        t1 = generate_refresh_token()
        t2 = generate_refresh_token()
        assert t1 != t2

    def test_hash_is_deterministic(self):
        """Hashing the same token twice produces the same hex digest."""
        token = "test-token-value"
        h1 = hash_refresh_token(token)
        h2 = hash_refresh_token(token)
        assert h1 == h2

    def test_hash_is_hex_string(self):
        """The hash output should be a 64-character hex string (SHA-256)."""
        h = hash_refresh_token("test-token-value")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# =========================================================================
# "Every route requires auth" integration test
#
# This test maintains an explicit list of every protected route in the
# application and asserts that each one returns 401 when called without
# an authentication cookie. The list must be updated every time a new
# protected route is added in a later phase — that upkeep IS the defense
# against accidentally shipping an unprotected endpoint.
# =========================================================================

from falcon import testing

from backend.app import create_app

# -------------------------------------------------------------------
# PROTECTED ROUTES: every auth-required route in the app.
# Add new entries here as they are registered in later phases.
# Format: (HTTP_METHOD, path)
# -------------------------------------------------------------------
PROTECTED_ROUTES: list[tuple[str, str]] = [
    ("GET", "/api/containers"),
    ("POST", "/api/containers"),
    ("PATCH", "/api/containers/nonexistent-id"),
    ("DELETE", "/api/containers/nonexistent-id"),
    # Phase 8.1:  ("GET", "/api/users"),
    # Phase 8.1:  ("POST", "/api/users"),
    # Phase 11.1: ("GET", "/api/metrics/latest"),
    # Phase 12.1: ("POST", "/api/containers/{id}/exec"),
    # Phase 13.1: ("GET", "/api/accounting"),
]

# -------------------------------------------------------------------
# PUBLIC ROUTES: routes that intentionally work without auth.
# These should NOT return 401 when called without a cookie.
# Format: (HTTP_METHOD, path)
# -------------------------------------------------------------------
PUBLIC_ROUTES: list[tuple[str, str]] = [
    ("GET", "/health"),
    ("GET", "/api/auth/google/login"),
    # callback and me are special cases tested separately
]


class TestEveryRouteRequiresAuth:
    """Integration test ensuring no protected route is left open.

    Uses Falcon's TestClient to simulate requests without any auth
    cookies and asserts that every protected route returns 401.
    """

    def _get_client(self) -> testing.TestClient:
        """Create a fresh Falcon test client for each test."""
        return testing.TestClient(create_app())

    def test_protected_routes_return_401_without_auth(self):
        """Every route in PROTECTED_ROUTES must return 401 without a cookie."""
        client = self._get_client()

        for method, path in PROTECTED_ROUTES:
            simulate = getattr(client, f"simulate_{method.lower()}")
            result = simulate(path)
            assert result.status_code == 401, (
                f"{method} {path} returned {result.status_code}, "
                f"expected 401 (unauthenticated)"
            )

    def test_public_routes_do_not_return_401(self):
        """Public routes must NOT return 401 without a cookie."""
        client = self._get_client()

        for method, path in PUBLIC_ROUTES:
            simulate = getattr(client, f"simulate_{method.lower()}")
            result = simulate(path)
            assert result.status_code != 401, (
                f"{method} {path} returned 401 but is supposed to be public"
            )

    def test_me_returns_401_without_auth(self):
        """GET /api/auth/me specifically must return 401 without a cookie."""
        client = self._get_client()
        result = client.simulate_get("/api/auth/me")
        assert result.status_code == 401

    def test_logout_succeeds_without_auth(self):
        """POST /api/auth/logout should succeed even without a cookie
        (idempotent — logging out when not logged in is not an error)."""
        client = self._get_client()
        result = client.simulate_post("/api/auth/logout")
        assert result.status_code == 200
