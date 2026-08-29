"""Cookie handling - the part that decides whether a session survives.

Sending only li_at + JSESSIONID gets the session invalidated by LinkedIn after
a few requests, and pinning JSESSIONID breaks as soon as LinkedIn rotates it.
These tests pin down both behaviours.
"""

import httpx
import pytest

from app.config import Settings
from app.linkedin import VoyagerClient, parse_cookie_header

FULL_COOKIE = (
    'bcookie="v=2&abc"; bscookie="v=1&xyz"; li_at=AQEDAtoken; '
    'JSESSIONID="ajax:1111111111111111111"; lidc="b=OGST00:s=O"'
)


def _client(**overrides) -> VoyagerClient:
    # _env_file=None keeps a developer's real .env out of the test run -
    # otherwise a local LINKEDIN_COOKIE leaks in and these assertions flip.
    return VoyagerClient(Settings(_env_file=None, **overrides))


def _response(set_cookies: dict) -> httpx.Response:
    headers = [("set-cookie", f"{k}={v}; Path=/") for k, v in set_cookies.items()]
    return httpx.Response(200, headers=headers, request=httpx.Request("GET", "https://x"))


# --- parsing --------------------------------------------------------------

def test_parse_cookie_header():
    jar = parse_cookie_header(FULL_COOKIE)
    assert jar["li_at"] == "AQEDAtoken"
    assert jar["JSESSIONID"] == '"ajax:1111111111111111111"'
    assert jar["bcookie"] == '"v=2&abc"'


@pytest.mark.parametrize("raw", ["", "   ", "novalue", "=orphan;;"])
def test_parse_cookie_header_tolerates_junk(raw):
    assert isinstance(parse_cookie_header(raw), dict)


# --- what the client derives ---------------------------------------------

def test_full_cookie_header_is_used_verbatim():
    client = _client(linkedin_cookie=FULL_COOKIE)
    assert client.authenticated
    assert client.has_full_cookies
    assert client.jsessionid == "ajax:1111111111111111111"   # quotes stripped
    header = client._cookie_header()
    assert "bcookie=" in header and "lidc=" in header
    assert 'JSESSIONID="ajax:1111111111111111111"' in header  # quotes kept


def test_csrf_token_always_matches_the_cookie():
    client = _client(linkedin_cookie=FULL_COOKIE)
    headers = client._headers()
    assert f'JSESSIONID="{headers["csrf-token"]}"' in headers["cookie"]


def test_two_cookie_mode_authenticates_but_is_flagged_partial():
    client = _client(linkedin_li_at="AQEDAtoken", linkedin_jsessionid="ajax:222")
    assert client.authenticated
    assert client.has_full_cookies is False   # no bcookie/bscookie
    assert client.jsessionid == "ajax:222"


def test_missing_jsessionid_is_not_authenticated():
    assert _client(linkedin_li_at="AQEDAtoken").authenticated is False


# --- rotation -------------------------------------------------------------

def test_rotated_jsessionid_is_adopted_and_csrf_follows():
    client = _client(linkedin_cookie=FULL_COOKIE)
    client._adopt_rotations(_response({"JSESSIONID": '"ajax:9999999999"'}))

    assert client.jsessionid == "ajax:9999999999"
    headers = client._headers()
    assert headers["csrf-token"] == "ajax:9999999999"
    assert 'JSESSIONID="ajax:9999999999"' in headers["cookie"]


def test_rotation_quotes_a_bare_jsessionid():
    client = _client(linkedin_cookie=FULL_COOKIE)
    client._adopt_rotations(_response({"JSESSIONID": "ajax:8888"}))
    assert 'JSESSIONID="ajax:8888"' in client._cookie_header()


def test_logout_instruction_is_ignored():
    """LinkedIn sends li_at="delete me" to end a session - adopting that would
    wipe our own credential and turn a transient failure into a dead client."""
    client = _client(linkedin_cookie=FULL_COOKIE)
    client._adopt_rotations(_response({"li_at": "delete me"}))
    assert client.li_at == "AQEDAtoken"


def test_all_rotated_cookies_are_adopted():
    """Cloudflare's __cf_bm and LinkedIn's lidc rotate frequently; sending a
    stale one is itself a signal that this is not the original browser."""
    client = _client(linkedin_cookie=FULL_COOKIE)
    client._adopt_rotations(_response({"__cf_bm": "fresh-token", "lidc": '"b=NEW"'}))
    header = client._cookie_header()
    assert "__cf_bm=fresh-token" in header
    assert 'lidc="b=NEW"' in header


class _StubTransport:
    """Minimal stand-in for the HTTP client inside VoyagerClient."""

    def __init__(self, response):
        self._response = response
        self.cookies = httpx.Cookies()
        self.kwargs = None

    async def get(self, url, **kwargs):
        self.kwargs = kwargs
        return self._response


async def test_redirect_is_reported_as_a_session_problem():
    """A 3xx from a JSON API is the login wall. Following it yields an opaque
    "maximum redirects followed" error that hides the real cause, so the
    client must not follow it and must say what actually happened."""
    from app.errors import SessionExpired

    client = _client(linkedin_cookie=FULL_COOKIE)
    client._client = _StubTransport(
        httpx.Response(
            302,
            headers={"location": "https://www.linkedin.com/login?session_redirect=x"},
            request=httpx.Request("GET", "https://www.linkedin.com/voyager/api/me"),
        )
    )

    with pytest.raises(SessionExpired) as err:
        await client._get("/me")

    message = str(err.value)
    assert "redirected" in message
    assert "/login" in message
    # and it must have asked the transport not to follow the redirect
    assert client._client.kwargs.get("follow_redirects") is False or            client._client.kwargs.get("allow_redirects") is False
