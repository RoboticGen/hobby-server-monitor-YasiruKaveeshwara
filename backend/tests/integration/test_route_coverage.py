"""
Route coverage: every registered route is reachable and guarded.

The unit suite's test_auth_required.py checks a hand-maintained list of
routes. That list is the documented defense (decision 7.2), but it only
protects routes someone remembered to add to it — a new endpoint registered
without a matching list entry is exactly the failure it is meant to catch,
and it would pass.

This test walks Falcon's router instead, so the route set is discovered
rather than declared. A new endpoint added in a later phase is picked up
automatically; if it is unguarded, this fails without anyone updating a list.
"""

import pytest
from falcon import testing

from backend.app import create_app

# Routes that are public by design. Anything not listed here must reject an
# unauthenticated caller. Keeping this list (rather than a list of protected
# routes) inverts the default: forgetting to update it makes a new route
# *stricter* in the test's eyes, not laxer, so the failure mode is a visible
# test failure rather than a silent gap.
PUBLIC = {
    ("GET", "/health"),
    ("GET", "/api/auth/google/login"),
    ("GET", "/api/auth/google/callback"),
    ("POST", "/api/auth/logout"),
}

# Placeholder values substituted for URI template fields when probing.
_FIELD_STUB = "00000000-0000-0000-0000-000000000000"


def _walk(node, prefix=""):
    """Yield (methods, path_template) for every responder in the router tree."""
    path = f"{prefix}/{node.raw_segment}"
    if node.resource is not None:
        methods = sorted(
            m
            for m, fn in (node.method_map or {}).items()
            if getattr(fn, "__name__", "") != "method_not_allowed"
            and m != "OPTIONS"
        )
        if methods:
            yield methods, path
    for child in node.children:
        yield from _walk(child, path)


def discovered_routes() -> list[tuple[str, str]]:
    """Return every (method, concrete_path) pair registered on the app."""
    app = create_app()
    found: list[tuple[str, str]] = []
    for root in app._router._roots:
        for methods, template in _walk(root):
            concrete = template
            # Replace {field} placeholders with a syntactically valid id that
            # will not exist, so the probe exercises auth rather than lookup.
            while "{" in concrete:
                start = concrete.index("{")
                end = concrete.index("}", start)
                concrete = concrete[:start] + _FIELD_STUB + concrete[end + 1:]
            for method in methods:
                found.append((method, concrete, template))
    return found


ROUTES = discovered_routes()


def test_router_is_not_empty():
    """Guard against the walk silently finding nothing and vacuously passing."""
    assert len(ROUTES) >= 14, f"only discovered {len(ROUTES)} routes"


@pytest.mark.parametrize(
    "method,path,template",
    ROUTES,
    ids=[f"{m} {t}" for m, _, t in ROUTES],
)
def test_every_discovered_route_requires_auth(method, path, template, client):
    """Every route not explicitly marked public must 401 without a cookie.

    Uses the concrete path with a non-existent id: a route that checked
    existence *before* authentication would answer 404 here and fail, which
    is the intended signal — leaking "this id does not exist" to an
    unauthenticated caller is itself a finding.
    """
    if _is_public(method, template):
        pytest.skip("route is public by design")

    simulate = getattr(client, f"simulate_{method.lower()}")
    result = simulate(path, json={})

    assert result.status_code == 401, (
        f"{method} {template} returned {result.status_code} to an "
        f"unauthenticated caller; expected 401"
    )


def _is_public(method: str, template: str) -> bool:
    """Match a discovered route against the PUBLIC set."""
    return (method, template) in PUBLIC


@pytest.mark.parametrize(
    "method,path,template",
    [r for r in ROUTES if _is_public(r[0], r[2])],
    ids=[f"{m} {t}" for m, _, t in ROUTES if _is_public(m, t)],
)
def test_public_routes_are_reachable_without_auth(method, path, template, client):
    """Public routes must not 401 — otherwise sign-in itself would be gated."""
    simulate = getattr(client, f"simulate_{method.lower()}")
    result = simulate(path)
    assert result.status_code != 401, f"{method} {template} unexpectedly 401s"


def test_expired_token_is_treated_as_unauthenticated(client):
    """An expired JWT must be rejected, not accepted as a stale identity."""
    from datetime import datetime, timedelta, timezone

    import jwt as pyjwt

    from backend.config import config

    now = datetime.now(timezone.utc)
    expired = pyjwt.encode(
        {
            "user_id": "someone",
            "role": "admin",
            "exp": now - timedelta(hours=1),
            "iat": now - timedelta(hours=2),
        },
        config.jwt_secret,
        algorithm="HS256",
    )
    result = client.simulate_get(
        "/api/users", headers={"Cookie": f"access_token={expired}"}
    )
    assert result.status_code == 401


def test_token_signed_with_wrong_secret_is_rejected(client):
    """A forged token signed with the wrong key must not grant admin."""
    from datetime import datetime, timedelta, timezone

    import jwt as pyjwt

    now = datetime.now(timezone.utc)
    forged = pyjwt.encode(
        {
            "user_id": "attacker",
            "role": "admin",
            "exp": now + timedelta(hours=1),
            "iat": now,
        },
        "not-the-real-secret",
        algorithm="HS256",
    )
    result = client.simulate_get(
        "/api/users", headers={"Cookie": f"access_token={forged}"}
    )
    assert result.status_code == 401


def test_alg_none_token_is_rejected(client):
    """The classic 'alg: none' JWT bypass must not be accepted.

    PyJWT refuses unsigned tokens unless 'none' is in the allowed algorithms
    list; decode_access_token pins HS256. This asserts that pinning holds.
    """
    import jwt as pyjwt

    unsigned = pyjwt.encode(
        {"user_id": "attacker", "role": "admin"}, key="", algorithm="none"
    )
    result = client.simulate_get(
        "/api/users", headers={"Cookie": f"access_token={unsigned}"}
    )
    assert result.status_code == 401
