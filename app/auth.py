"""Log in with a username + password and keep the whole cookie jar.

This is the alternative to copying a cookie header out of a browser, and for a
deployed instance it is the better one: the server can re-authenticate by
itself when a session ends, instead of someone editing environment variables.

The important detail is that we keep **every** cookie LinkedIn sets, not just
`li_at`. LinkedIn issues `bcookie`/`bscookie` (browser identity) to any
visitor, before you authenticate. Doing the whole flow in one session means
those identity cookies were issued to *this* client, so the cookie set, the IP
and the TLS fingerprint all agree with each other. A cookie header copied out
of Chrome does not have that property - it was minted for a different browser
on a different connection.

`session_key` accepts an email address or a phone number, whichever the
account uses to sign in.
"""

from __future__ import annotations

import logging
import re

from .linkedin import HAS_CURL_CFFI, IMPERSONATE, USER_AGENT

log = logging.getLogger(__name__)

HOME_URL = "https://www.linkedin.com/"
AUTH_URL = "https://www.linkedin.com/uas/authenticate"

_HEADERS = {
    "user-agent": USER_AGENT,
    "accept-language": "en-US,en;q=0.9",
    "x-li-user-agent": "LIAuthLibrary:0.0.3 com.linkedin.android:4.1.881 Pixel:5",
    "accept": "application/json",
    "content-type": "application/x-www-form-urlencoded",
}

# login_result values LinkedIn returns for a blocked-but-valid attempt.
_CHALLENGE_RESULTS = {
    "CHALLENGE": "a security checkpoint (2FA, an emailed PIN, or a captcha)",
    "BAD_PASSWORD": "the password was rejected",
    "BAD_EMAIL": "the email/phone was not recognised",
    "ACCOUNT_LOCKED": "the account is locked",
}


class LoginFailed(Exception):
    """Login did not produce a usable session."""


def _session(timeout: int):
    """A TLS-impersonating session if available, else plain httpx."""
    if HAS_CURL_CFFI:
        from curl_cffi import requests as curl_requests

        return curl_requests.AsyncSession(impersonate=IMPERSONATE, timeout=timeout)

    import httpx

    return httpx.AsyncClient(timeout=timeout, follow_redirects=True)


async def _close(session) -> None:
    closer = getattr(session, "aclose", None) or session.close
    result = closer()
    if hasattr(result, "__await__"):
        await result


async def fetch_session_cookies(
    username: str, password: str, timeout: int = 25
) -> dict[str, str]:
    """Sign in and return the full cookie jar as {name: value}.

    Raises LoginFailed with a specific reason - a checkpoint is by far the most
    common outcome for a new account or an unfamiliar IP.
    """
    session = _session(timeout)
    try:
        # 1. Touch the homepage so LinkedIn issues the browser-identity cookies
        #    (bcookie/bscookie) and a session-bound JSESSIONID.
        await session.get(HOME_URL, headers={"user-agent": USER_AGENT})

        jsessionid = (session.cookies.get("JSESSIONID") or "").strip('"')
        if not jsessionid:
            raise LoginFailed(
                "LinkedIn did not issue a JSESSIONID cookie - it may be blocking "
                "this IP address."
            )

        # 2. Post the credentials, echoing the CSRF token back.
        resp = await session.post(
            AUTH_URL,
            headers=_HEADERS,
            data={
                "session_key": username,
                "session_password": password,
                "JSESSIONID": jsessionid,
            },
        )

        if resp.status_code == 401:
            raise LoginFailed(
                "LinkedIn rejected the credentials (HTTP 401). Common causes, "
                "in rough order: the password is wrong; the account signs in "
                "with Google/SSO rather than a LinkedIn password; two-factor "
                "authentication is enabled; or LinkedIn has flagged the account "
                "and is refusing this login path. Do NOT retry repeatedly - "
                "failed logins can lock the account. Use LINKEDIN_COOKIE instead."
            )
        if resp.status_code != 200:
            raise LoginFailed(f"Login endpoint returned HTTP {resp.status_code}.")

        try:
            result = resp.json().get("login_result")
        except ValueError:
            raise LoginFailed("Login endpoint returned a non-JSON response.")

        if result != "PASS":
            reason = _CHALLENGE_RESULTS.get(result, f"login_result={result!r}")
            raise LoginFailed(
                f"LinkedIn refused the login - {reason}. Complete the challenge "
                "by signing in through a browser, then either retry or fall back "
                "to copying LINKEDIN_COOKIE."
            )

        # 3. Harvest everything, including the pre-auth identity cookies.
        jar = {name: value for name, value in session.cookies.items() if value}
        if not jar.get("li_at"):
            raise LoginFailed("Login reported success but no li_at cookie was set.")

        _requote_jsessionid(jar)
        log.info("Signed in to LinkedIn; captured %d cookies.", len(jar))
        return jar
    finally:
        await _close(session)


def _requote_jsessionid(jar: dict[str, str]) -> None:
    """LinkedIn stores JSESSIONID quoted; the client expects it that way."""
    value = jar.get("JSESSIONID")
    if value and not value.startswith('"'):
        jar["JSESSIONID"] = f'"{value}"'


def looks_like_phone(value: str) -> bool:
    """Phone numbers are accepted as session_key; used only for logging."""
    return bool(re.fullmatch(r"[+\d][\d\s\-()]{6,}", (value or "").strip()))
