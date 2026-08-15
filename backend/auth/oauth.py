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


class TokenExchangeError(RuntimeError):
    """Raised when the authorization-code exchange does not succeed.

    Exists so the caller can report *why* the exchange failed. Google's token
    endpoint answers a rejection with a JSON body naming the cause
    ('invalid_client' for a bad secret, 'invalid_grant' for a code that is
    expired or already redeemed, 'redirect_uri_mismatch'), and those names are
    the only thing that distinguishes a misconfigured deployment from a user
    who simply took too long or reloaded the callback. Collapsing them all
    into one opaque failure leaves nothing to act on.

    Attributes:
        error: Google's machine-readable code, or 'network_error' when the
            request never reached Google at all.
        description: Google's human-readable explanation, or the exception
            text for a transport failure.
        status: The HTTP status Google returned, or 0 if there was no reply.
    """

    def __init__(self, error: str, description: str, status: int) -> None:
        super().__init__(f"{error}: {description}" if description else error)
        self.error = error
        self.description = description
        self.status = status


def build_google_auth_url(state: str) -> str:
    """Construct the Google OAuth2 authorization URL.

    Returns the full URL the user's browser should be redirected to.
    After the user authenticates with Google, Google redirects back to
    our configured callback URL with an authorization code.

    `state` is an opaque random value that Google echoes back unmodified on
    the callback. The caller stores the same value in a cookie and compares
    the two, which is what ties a callback to a sign-in this browser actually
    started — without it, an attacker can feed a victim a callback URL
    carrying their own authorization code and silently log the victim into
    the attacker's account (login CSRF). It is required, not optional,
    because a default would make the protection easy to omit by accident.
    """
    params = {
        "client_id": config.google_client_id,
        "redirect_uri": config.google_redirect_uri,
        "response_type": "code",
        "scope": _SCOPES,
        "access_type": "offline",  # requests a refresh token from Google
        "prompt": "consent",  # ensures we always get a fresh consent
        "state": state,
    }
    return f"{_GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_tokens(code: str) -> dict:
    """Exchange an authorization code for Google OAuth tokens.

    POSTs to Google's token endpoint with the authorization code we
    received from the callback. Returns the parsed JSON response, which
    contains at minimum 'id_token' and 'access_token'.

    Raises TokenExchangeError if Google rejects the exchange or is
    unreachable, carrying Google's own error code so the caller can log
    something specific instead of "it failed".

    Note that `redirect_uri` is sent again here even though no redirect
    happens on this call: Google requires it to match the value used in the
    authorization request, as proof the same client is completing the flow.
    """
    payload = {
        "client_id": config.google_client_id,
        "client_secret": config.google_client_secret,
        "code": code,
        "redirect_uri": config.google_redirect_uri,
        "grant_type": "authorization_code",
    }

    # A transport failure and a rejection are different problems — DNS or
    # egress trouble versus bad credentials — so they are separated here
    # rather than both surfacing as a generic exception.
    try:
        resp = requests.post(_GOOGLE_TOKEN_URL, data=payload, timeout=10)
    except requests.RequestException as exc:
        raise TokenExchangeError("network_error", str(exc), 0) from exc

    if resp.status_code != 200:
        # The error body is the whole point of this branch, so a body that is
        # not JSON falls back to the raw text rather than masking the failure
        # with a second one. Truncated because it reaches a log line.
        try:
            body = resp.json()
        except ValueError:
            body = {}
        raise TokenExchangeError(
            body.get("error", "unknown_error"),
            body.get("error_description", resp.text[:200]),
            resp.status_code,
        )

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
# Tolerance for disagreement between this host's clock and Google's when
# checking the ID token's `iat` and `exp` claims.
#
# google-auth defaults this to 0, which rejects a token whose `iat` is even a
# fraction of a second in the future — and that happens routinely: Google
# stamps `iat` at issuance, the token then travels through the browser
# redirect and our token exchange, and any local clock running slightly slow
# makes a perfectly valid token look like it was "used too early". Under WSL2
# the effect is worse, because the VM clock drifts from the Windows host
# across sleep.
#
# 10 seconds absorbs that without meaningfully weakening the check: ID tokens
# are valid for about an hour, so this cannot resurrect an expired one. It is
# not a substitute for a correct clock — a host minutes out of sync still
# fails, and should.
_CLOCK_SKEW_TOLERANCE_SECONDS = 10


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
        clock_skew_in_seconds=_CLOCK_SKEW_TOLERANCE_SECONDS,
    )
    return claims
