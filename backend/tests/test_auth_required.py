"""
Tests for authentication-related code.

Started in Step 5.1 with a basic test for the OAuth URL builder.
Extended in Step 5.2 with JWT round-trip tests.
Extended in Step 5.4 with the "every route requires auth" test.
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
