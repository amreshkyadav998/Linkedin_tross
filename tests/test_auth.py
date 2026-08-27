"""Credential login: what it captures, and how it reports refusals.

LinkedIn is not contacted here - the session object is stubbed. What matters
is that we keep the *whole* jar (a partial one gets the session killed) and
that each refusal turns into a readable message rather than a bare exception.
"""

import pytest

from app import auth
from app.auth import LoginFailed, fetch_session_cookies, looks_like_phone


class FakeCookies(dict):
    def get(self, name, default=None):  # matches the transports' Cookies API
        return dict.get(self, name, default)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Stands in for curl_cffi's AsyncSession / httpx's AsyncClient."""

    def __init__(self, cookies, response):
        self.cookies = FakeCookies(cookies)
        self._response = response
        self.posted = None
        self.closed = False

    async def get(self, url, **kwargs):
        return FakeResponse({})

    async def post(self, url, **kwargs):
        self.posted = kwargs.get("data")
        return self._response

    def close(self):
        self.closed = True


@pytest.fixture
def install(monkeypatch):
    def _install(cookies, response):
        session = FakeSession(cookies, response)
        monkeypatch.setattr(auth, "_session", lambda timeout: session)
        return session

    return _install


PRE_AUTH = {"JSESSIONID": '"ajax:555"', "bcookie": '"v=2&abc"', "bscookie": '"v=1&xyz"'}
SIGNED_IN = {**PRE_AUTH, "li_at": "AQEDAtoken", "lidc": '"b=OGST"'}


@pytest.mark.asyncio
async def test_returns_the_whole_jar(install):
    session = install(SIGNED_IN, FakeResponse({"login_result": "PASS"}))

    jar = await fetch_session_cookies("ada@example.org", "hunter2")

    # The browser-identity cookies are the point - li_at alone is not enough.
    assert jar["li_at"] == "AQEDAtoken"
    assert jar["bcookie"] == '"v=2&abc"'
    assert jar["bscookie"] == '"v=1&xyz"'
    assert jar["JSESSIONID"] == '"ajax:555"'
    assert session.closed


@pytest.mark.asyncio
async def test_csrf_token_is_echoed_back_unquoted(install):
    session = install(SIGNED_IN, FakeResponse({"login_result": "PASS"}))
    await fetch_session_cookies("ada@example.org", "hunter2")
    assert session.posted["JSESSIONID"] == "ajax:555"
    assert session.posted["session_key"] == "ada@example.org"


@pytest.mark.asyncio
async def test_bare_jsessionid_is_quoted(install):
    install({**SIGNED_IN, "JSESSIONID": "ajax:555"},
            FakeResponse({"login_result": "PASS"}))
    jar = await fetch_session_cookies("ada@example.org", "hunter2")
    assert jar["JSESSIONID"] == '"ajax:555"'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result,expected",
    [
        ("CHALLENGE", "checkpoint"),
        ("BAD_PASSWORD", "password"),
        ("BAD_EMAIL", "not recognised"),
        ("ACCOUNT_LOCKED", "locked"),
    ],
)
async def test_refusals_explain_themselves(install, result, expected):
    install(PRE_AUTH, FakeResponse({"login_result": result}))
    with pytest.raises(LoginFailed, match=expected):
        await fetch_session_cookies("ada@example.org", "hunter2")


@pytest.mark.asyncio
async def test_missing_jsessionid_fails_early(install):
    install({}, FakeResponse({"login_result": "PASS"}))
    with pytest.raises(LoginFailed, match="JSESSIONID"):
        await fetch_session_cookies("ada@example.org", "hunter2")


@pytest.mark.asyncio
async def test_pass_without_li_at_is_a_failure(install):
    install(PRE_AUTH, FakeResponse({"login_result": "PASS"}))
    with pytest.raises(LoginFailed, match="li_at"):
        await fetch_session_cookies("ada@example.org", "hunter2")


@pytest.mark.asyncio
async def test_non_json_and_http_errors(install):
    install(PRE_AUTH, FakeResponse(None))
    with pytest.raises(LoginFailed, match="non-JSON"):
        await fetch_session_cookies("ada@example.org", "hunter2")

    install(PRE_AUTH, FakeResponse({}, status_code=503))
    with pytest.raises(LoginFailed, match="HTTP 503"):
        await fetch_session_cookies("ada@example.org", "hunter2")


@pytest.mark.parametrize("value,expected", [
    ("+919876543210", True),
    ("9876543210", True),
    ("+1 (555) 123-4567", True),
    ("ada@example.org", False),
    ("", False),
])
def test_looks_like_phone(value, expected):
    assert looks_like_phone(value) is expected


@pytest.mark.asyncio
async def test_401_explains_causes_and_warns_against_retrying(install):
    """A bare "HTTP 401" tells the operator nothing, and the wrong reaction -
    retrying - is the one that gets the account locked."""
    install(PRE_AUTH, FakeResponse({}, status_code=401))
    with pytest.raises(LoginFailed) as err:
        await fetch_session_cookies("ada@example.org", "hunter2")

    message = str(err.value)
    assert "lock the account" in message
    assert "LINKEDIN_COOKIE" in message
