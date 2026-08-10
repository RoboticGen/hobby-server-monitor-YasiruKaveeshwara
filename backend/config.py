"""
Central configuration module for the Hobby Server Monitor backend.

This is the ONLY module in the entire backend that reads os.environ.
Every other module imports the `config` singleton from here instead of
reading environment variables directly.

Fail-fast design: if a required variable is missing, the application
raises a RuntimeError immediately at import time rather than starting
in a silently misconfigured state. This matters for security-critical
settings like JWT_SECRET and ADMIN_BOOTSTRAP_EMAIL — if these silently
defaulted to empty strings or dummy values, the app would appear to work
but would be trivially exploitable (e.g., an empty JWT secret means any
client can forge valid tokens; a missing bootstrap email means no admin
account can ever be created).
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    """Immutable holder for all application configuration.

    Fields map 1:1 to the environment variables documented in .env.example
    and in the build guide section 0.11. Types are converted from their
    string env-var representation to the correct Python type here.
    """

    # Google OAuth 2.0
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str

    # JWT / session signing
    jwt_secret: str

    # Admin bootstrap — the one email that becomes Admin on first sign-in
    # (decision 7.11: explicit env var prevents "first visitor wins" race)
    admin_bootstrap_email: str

    # Storage paths
    database_path: Path
    tinyflux_path: Path

    # LXD connection
    lxd_endpoint: str
    lxd_cert_path: str   # Empty string when using a unix socket
    lxd_key_path: str    # Empty string when using a unix socket

    # Background collector interval in seconds
    collector_interval_seconds: int

    # Frontend origin — used for CORS and cookie domain settings
    frontend_origin: str

    # Whether session cookies require HTTPS (true in production, false for
    # local HTTP development)
    session_cookie_secure: bool

    # Port the Falcon API server listens on
    port: int


def _require(name: str) -> str:
    """Read a required environment variable, raising immediately if missing.

    This is the enforcement point for the fail-fast rule: a missing
    security-critical variable (like JWT_SECRET or ADMIN_BOOTSTRAP_EMAIL)
    must never silently default, because that would be a security bug,
    not just an inconvenience — an empty JWT_SECRET lets anyone forge
    valid tokens, and a missing ADMIN_BOOTSTRAP_EMAIL means no admin
    account can ever be created through the intended bootstrap flow.
    """
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable '{name}' is missing or empty. "
            f"Set it in your .env file or system environment before starting "
            f"the application. See .env.example for documentation."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    """Read an optional environment variable, returning a fallback default
    if it is not set in the environment."""
    return os.environ.get(name, default)


def _parse_bool(value: str) -> bool:
    """Convert a string environment variable value to a Python bool.

    Accepts 'true', '1', or 'yes' (case-insensitive) as True;
    everything else is treated as False.
    """
    return value.strip().lower() in ("true", "1", "yes")


def load_config() -> Config:
    """Load and validate all application configuration from the environment.

    Reads a .env file if present (for local development), then falls back
    to real environment variables (for production under systemd). Validates
    that every required variable is set and converts types appropriately.

    Returns a frozen Config dataclass instance.
    Raises RuntimeError if any required variable is missing or empty.
    """
    # Load .env file from the same directory as this config module (backend/).
    # Does NOT override already-set env vars, so real environment
    # (e.g., systemd EnvironmentFile=) always takes precedence.
    _env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(dotenv_path=_env_path)

    return Config(
        # --- Required variables: fail loudly if missing ---
        google_client_id=_require("GOOGLE_CLIENT_ID"),
        google_client_secret=_require("GOOGLE_CLIENT_SECRET"),
        google_redirect_uri=_require("GOOGLE_REDIRECT_URI"),
        jwt_secret=_require("JWT_SECRET"),
        admin_bootstrap_email=_require("ADMIN_BOOTSTRAP_EMAIL"),
        database_path=Path(_require("DATABASE_PATH")),
        tinyflux_path=Path(_require("TINYFLUX_PATH")),
        lxd_endpoint=_require("LXD_ENDPOINT"),
        frontend_origin=_require("FRONTEND_ORIGIN"),

        # --- Optional variables: sensible defaults for development ---
        lxd_cert_path=_optional("LXD_CERT_PATH", ""),
        lxd_key_path=_optional("LXD_KEY_PATH", ""),
        collector_interval_seconds=int(
            _optional("COLLECTOR_INTERVAL_SECONDS", "10")
        ),
        session_cookie_secure=_parse_bool(
            _optional("SESSION_COOKIE_SECURE", "false")
        ),
        port=int(_optional("PORT", "8000")),
    )


# Module-level singleton: every other backend module imports this instead
# of calling load_config() themselves. This ensures configuration is
# loaded and validated exactly once, at startup, and any missing variable
# is caught before the app handles its first request.
config = load_config()
