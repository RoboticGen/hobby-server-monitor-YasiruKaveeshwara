"""
Login CSRF: the OAuth callback must refuse a flow this browser did not start.

Without a `state` check, the callback accepts an authorization code from
anyone. The attack is not "steal the victim's session" — it is the inverse,
and it is easy to miss because nothing looks broken afterwards:

  1. The attacker begins a normal Google sign-in as themselves and stops at
     the callback URL, holding a valid, unredeemed authorization code.
  2. They get the victim's browser to load that URL — a link, an <img>, a
     redirect from any page.
  3. Our callback exchanges the attacker's code, verifies the attacker's
     identity, and sets *the victim's browser* up with a session belonging to
     the attacker's account.

The victim now sees a working dashboard and has no reason to doubt it. Every
container they create and every command they run lands in the attacker's
account, which the attacker can sign into and read at leisure.

The defense is a random value bound to the browser: `/login` mints one, sends
it to Google, and stores it in an httpOnly cookie on our origin. The callback
requires the two to match. An attacker controls the query string but cannot
write a cookie on our origin, so the halves cannot be made to agree.

These tests assert the refusal *and* that it is cheap: a rejected callback
must not reach Google's token endpoint, since a check that still burns an
authorization code and an outbound request is a rate-limiting problem wearing
a security fix's clothes.
"""

import pytest
from falcon import testing

from backend.app import create_app
from backend.db import repo
from backend.resources import auth as auth_module

pytestmark = pytest.mark.usefixtures("isolated_db")

VICTIM_EMAIL = "csrf-victim@example.com"


@pytest.fixture
def client(isolated_db):
    """A test client on the isolated database, with a real user to sign in as."""
    if repo.get_user_by_email(VICTIM_EMAIL) is None:
        repo.create_user(
            email=VICTIM_EMAIL,
            role="user",
            status="active",
            quota_ram_mb=4096,
            quota_cpu=4.0,
            quota_disk_gb=40,
        )
    return testing.TestClient(create_app())


@pytest.fixture
def google(monkeypatch):
    """Stub Google's two network calls and count how often they are reached.

    The counter is the point. Asserting only on the status code cannot tell a
    callback that was refused up front from one that was refused after a round
    trip to Google — and those differ in whether an attacker can make us spend
    an authorization code per forged request.
    """
    calls = {"exchange": 0, "verify": 0}

    def _exchange(code):
        calls["exchange"] += 1
        return {"id_token": "fake-id-token"}

    def _verify(token):
        calls["verify"] += 1
        return {"email": VICTIM_EMAIL, "email_verified": True}

    monkeypatch.setattr(auth_module, "exchange_code_for_tokens", _exchange)
    monkeypatch.setattr(auth_module, "verify_and_decode_id_token", _verify)
    return calls


def _begin_login(client) -> str:
    """Hit /login and return the state value it committed to the cookie jar."""
    result = client.simulate_get("/api/auth/google/login")
    assert result.status_code == 302
    return result.cookies["oauth_state"].value


def _session_count() -> int:
    """Rows in the sessions table — the thing a successful attack would add."""
    conn = repo.get_connection()
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
    finally:
        conn.close()


class TestLoginIssuesState:
    """What the /login redirect has to set up for the callback to verify."""

    def test_login_sets_a_state_cookie(self, client):
        state = _begin_login(client)
        assert state, "no oauth_state cookie was set"

    def test_state_cookie_is_httponly(self, client):
        """If JavaScript could read it, an XSS on our origin could also forge
        a matching callback, and the cookie would prove nothing."""
        result = client.simulate_get("/api/auth/google/login")
        assert result.cookies["oauth_state"].http_only is True

    def test_state_cookie_is_samesite_lax_not_strict(self, client):
        """Lax is required, not a weakening.

        The callback arrives as a top-level navigation from
        accounts.google.com, which is cross-site. Strict withholds cookies on
        exactly that navigation, so a Strict state cookie would be absent at
        the callback and every legitimate sign-in would fail verification.
        """
        result = client.simulate_get("/api/auth/google/login")
        assert result.cookies["oauth_state"].same_site.lower() == "lax"

    def test_state_cookie_is_short_lived(self, client):
        """It only has to survive one trip to Google's consent screen."""
        result = client.simulate_get("/api/auth/google/login")
        max_age = result.cookies["oauth_state"].max_age
        assert 0 < max_age <= 900, f"state cookie max_age={max_age}"

    def test_state_sent_to_google_matches_the_cookie(self, client):
        """The redirect Google receives must carry the same value we stored —
        otherwise the callback compares two unrelated strings and can never
        succeed."""
        result = client.simulate_get("/api/auth/google/login")
        state = result.cookies["oauth_state"].value
        assert f"state={state}" in result.headers["location"]

    def test_each_login_mints_a_fresh_state(self, client):
        """A state reused across sign-ins would make one captured callback URL
        replayable against later attempts."""
        assert _begin_login(client) != _begin_login(client)


class TestCallbackRefusesForgedState:
    """The attack itself, in its three shapes."""

    def test_callback_with_no_state_at_all_is_refused(self, client, google):
        """The pre-fix request: a bare callback URL with only a code.

        This is the shape an attacker actually sends, because they have no way
        to make the victim's browser present our state cookie *and* no way to
        guess its value.
        """
        before = _session_count()
        result = client.simulate_get(
            "/api/auth/google/callback", params={"code": "attacker-code"}
        )

        assert result.status_code == 400
        assert google["exchange"] == 0, "refused callback still called Google"
        assert _session_count() == before, "a refused callback created a session"

    def test_callback_with_a_state_param_but_no_cookie_is_refused(self, client, google):
        """An attacker can put anything in the query string.

        What they cannot do is set an httpOnly cookie on our origin, so the
        cookie side stays empty. Two empty-ish values must not be allowed to
        "match" — that is the bug this asserts against.
        """
        result = client.simulate_get(
            "/api/auth/google/callback",
            params={"code": "attacker-code", "state": "attacker-chosen-state"},
        )

        assert result.status_code == 400
        assert google["exchange"] == 0

    def test_callback_with_mismatched_state_is_refused(self, client, google):
        """A browser mid-sign-in holds a real cookie; the forged URL carries a
        different value. The comparison, not the presence, is what decides."""
        real_state = _begin_login(client)
        before = _session_count()

        result = client.simulate_get(
            "/api/auth/google/callback",
            params={"code": "attacker-code", "state": "not-the-real-state"},
            cookies={"oauth_state": real_state},
        )

        assert result.status_code == 400
        assert google["exchange"] == 0
        assert _session_count() == before

    def test_callback_with_empty_state_param_is_refused(self, client, google):
        """An empty string must not satisfy the check by being falsy on both
        sides or by comparing equal to a missing cookie."""
        result = client.simulate_get(
            "/api/auth/google/callback",
            params={"code": "attacker-code", "state": ""},
            cookies={"oauth_state": ""},
        )

        assert result.status_code == 400
        assert google["exchange"] == 0

    def test_refusal_does_not_name_the_expected_value(self, client, google):
        """The error reaches a browser the attacker may control, so it must not
        echo the state we were expecting."""
        real_state = _begin_login(client)
        result = client.simulate_get(
            "/api/auth/google/callback",
            params={"code": "c", "state": "wrong"},
            cookies={"oauth_state": real_state},
        )
        assert real_state not in result.text


class TestCallbackAcceptsGenuineState:
    """The defense must not break the flow it is protecting."""

    def test_matching_state_completes_the_sign_in(self, client, google):
        """A real browser round-trip: /login, then the callback with the state
        Google echoed back and the cookie the browser still holds."""
        state = _begin_login(client)
        before = _session_count()

        result = client.simulate_get(
            "/api/auth/google/callback",
            params={"code": "genuine-code", "state": state},
            cookies={"oauth_state": state},
        )

        assert result.status_code == 302, result.text
        assert google["exchange"] == 1
        assert _session_count() == before + 1
        assert "access_token" in result.cookies
        assert "refresh_token" in result.cookies

    def test_consumed_state_cookie_is_cleared(self, client, google):
        """Clearing it is what makes the value single-use: the same callback
        URL, resent from the same browser, no longer has a cookie to match."""
        state = _begin_login(client)
        result = client.simulate_get(
            "/api/auth/google/callback",
            params={"code": "genuine-code", "state": state},
            cookies={"oauth_state": state},
        )

        assert result.status_code == 302
        assert "oauth_state" in result.cookies, "state cookie was not unset"
        assert (
            result.cookies["oauth_state"].value == ""
        ), "state cookie survived the callback that consumed it"

    def test_replaying_a_completed_callback_is_refused(self, client, google):
        """Second use of the same URL, now that the browser's cookie is gone.

        Simulated by omitting the cookie, which is what the browser does after
        honoring the unset from the first response.
        """
        state = _begin_login(client)
        first = client.simulate_get(
            "/api/auth/google/callback",
            params={"code": "genuine-code", "state": state},
            cookies={"oauth_state": state},
        )
        assert first.status_code == 302

        replay = client.simulate_get(
            "/api/auth/google/callback",
            params={"code": "genuine-code", "state": state},
        )
        assert replay.status_code == 400
        assert google["exchange"] == 1, "the replay reached Google a second time"

    def test_state_check_runs_before_the_missing_code_check(self, client, google):
        """Order matters for what an unauthenticated caller can learn.

        A callback with a valid state but no code is a real user whose flow
        went wrong, and gets the specific "missing authorization code" message.
        A callback with neither must be refused on state alone — it should not
        be told anything about how far it got.
        """
        no_state = client.simulate_get("/api/auth/google/callback")
        assert no_state.status_code == 400
        assert "authorization code" not in no_state.text.lower()

        state = _begin_login(client)
        no_code = client.simulate_get(
            "/api/auth/google/callback",
            params={"state": state},
            cookies={"oauth_state": state},
        )
        assert no_code.status_code == 400
        assert "authorization code" in no_code.text.lower()
