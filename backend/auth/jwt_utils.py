"""
JWT access token and refresh token utilities.

Access tokens are short-lived JWTs (15 min) containing the user's ID and
role. They are signed with JWT_SECRET and verified on every API request
by the auth middleware.

Refresh tokens are opaque random strings stored as httpOnly cookies.
The database stores only the SHA-256 hash of each refresh token — never
the raw value — so that a leaked database dump does not hand out working
sessions. An attacker with a copy of the DB would have hashes, but could
not reverse them into valid cookies.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from backend.config import config

# Access token lifetime is configurable via env so session duration can be
# tuned without changing application code.
_ACCESS_TOKEN_LIFETIME = timedelta(minutes=config.access_token_lifetime_minutes)

# Algorithm used for JWT signing — HS256 is appropriate for a single-server
# setup where the same process both signs and verifies.
_JWT_ALGORITHM = "HS256"


def create_access_token(user_id: str, role: str) -> str:
    """Create a signed JWT access token containing user identity and role.

    The token includes an 'exp' claim set 15 minutes from now so that
    PyJWT automatically rejects it once expired.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "user_id": user_id,
        "role": role,
        "exp": now + _ACCESS_TOKEN_LIFETIME,
        "iat": now,
    }
    return jwt.encode(payload, config.jwt_secret, algorithm=_JWT_ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    """Verify and decode a JWT access token.

    Returns the payload dict (with 'user_id' and 'role') if the token
    is valid and not expired. Returns None if the token is invalid,
    expired, or tampered with — callers check for None rather than
    catching exceptions, keeping the auth middleware simple.
    """
    try:
        payload = jwt.decode(token, config.jwt_secret, algorithms=[_JWT_ALGORITHM])
        return payload
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def generate_refresh_token() -> str:
    """Generate a cryptographically random opaque refresh token.

    Uses secrets.token_urlsafe which is backed by the OS CSPRNG,
    producing a 43-character URL-safe base64 string (32 bytes of
    entropy) — suitable for a session token that will be stored in
    an httpOnly cookie.
    """
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    """Compute the SHA-256 hash of a refresh token for database storage.

    The database stores only this hash, never the raw token value.
    This means a leaked database dump (e.g., an unprotected backup,
    a SQL injection that reads the sessions table) does not give an
    attacker usable session tokens — they would need to reverse a
    SHA-256 hash, which is computationally infeasible.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
