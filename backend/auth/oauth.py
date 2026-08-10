"""
Google OAuth 2.0 Authorization Code flow helpers.

These functions handle the three stages of the OAuth exchange:
1. Building the authorization URL that redirects the user to Google.
2. Exchanging the authorization code for tokens after Google redirects back.
3. Verifying the returned ID token so we can trust the email it contains.

No other module in this project should interact with Google's auth
endpoints directly — all OAuth logic is concentrated here.
"""

from urllib.parse import urlencode

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token as google_id_token

from backend.config import config

# Google's OAuth 2.0 endpoints (stable, well-documented)
_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Scopes we request: openid for the ID token, email and profile for
# the user's verified email address and display name.
_SCOPES = "openid email profile"


def build_google_auth_url() -> str:
    """Construct the Google OAuth2 authorization URL.

    Returns the full URL the user's browser should be redirected to.
    After the user authenticates with Google, Google redirects back to
    our configured callback URL with an authorization code.
    """
    params = {
        "client_id": config.google_client_id,
        "redirect_uri": config.google_redirect_uri,
        "response_type": "code",
        "scope": _SCOPES,
        "access_type": "offline",  # requests a refresh token from Google
        "prompt": "consent",       # ensures we always get a fresh consent
    }
    return f"{_GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_tokens(code: str) -> dict:
    """Exchange an authorization code for Google OAuth tokens.

    POSTs to Google's token endpoint with the authorization code we
    received from the callback. Returns the parsed JSON response, which
    contains at minimum 'id_token' and 'access_token'.

    Raises requests.HTTPError if Google rejects the exchange (e.g.,
    expired or already-used code).
    """
    payload = {
        "client_id": config.google_client_id,
        "client_secret": config.google_client_secret,
        "code": code,
        "redirect_uri": config.google_redirect_uri,
        "grant_type": "authorization_code",
    }
    resp = requests.post(_GOOGLE_TOKEN_URL, data=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


# -------------------------------------------------------------------------
# verify_and_decode_id_token is the ONLY place in the entire application
# where we trust that an email address genuinely belongs to a real person.
# Google's ID token is cryptographically signed; this function verifies
# that signature and checks the audience claim matches our client ID.
#
# Everything after this point — looking the email up in our users table,
# checking roles, enforcing quotas — is our own authorization logic that
# has nothing to do with Google. Google only proves identity ("this
# person owns this email"); we decide what they're allowed to do.
# -------------------------------------------------------------------------
def verify_and_decode_id_token(token: str) -> dict:
    """Verify a Google ID token's signature and return the decoded claims.

    Uses the google-auth library to validate the token against Google's
    public keys and confirm the audience matches our client ID.

    Returns the decoded claims dict, which includes 'email',
    'email_verified', 'name', 'picture', and other profile fields.

    Raises ValueError if the token is invalid, expired, or has the
    wrong audience.
    """
    claims = google_id_token.verify_oauth2_token(
        token,
        GoogleAuthRequest(),
        audience=config.google_client_id,
    )
    return claims
