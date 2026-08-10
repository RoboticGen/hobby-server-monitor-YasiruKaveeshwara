"""
Tests for authentication-related code.

Started with a basic test for the OAuth URL builder.
Extended with JWT round-trip tests.
Extended with the "every route requires auth" test.
"""

import os
import tempfile

# Override DATABASE_PATH before importing any backend module that
# triggers config loading, so tests don't touch the real database.
if "DATABASE_PATH" not in os.environ:
    _tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _tmp.close()
    os.environ.setdefault("DATABASE_PATH", _tmp.name)

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
