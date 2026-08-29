"""A tiny client for LinkedIn's internal "Voyager" API.

Voyager is the private JSON API that linkedin.com itself calls from the
browser. It is not documented and needs a logged-in session, but it returns
the profile page's data already structured - which is exactly what we want.

Auth is cookie based, and the important lesson is that **partial cookies do
not work**. The obvious approach - send `li_at` (the session) plus
`JSESSIONID` (the CSRF token) - succeeds for a handful of requests and then
LinkedIn invalidates the session outright, answering later calls with
"403 CSRF check failed" and finally `li_at=delete me`.

The reason is that a browser sends roughly ten cookies, not two. `bcookie` and
`bscookie` identify the browser, `lidc` routes to a datacenter, and LinkedIn
binds `li_at` to that browser identity. An `li_at` arriving without them looks
like a replayed/stolen cookie, so LinkedIn kills the session defensively.

And correct cookies are still not sufficient on their own - LinkedIn also
fingerprints the TLS handshake, so the connection has to look like Chrome too.

So this client:
  1. takes the WHOLE cookie header copied from a real browser session,
  2. follows LinkedIn's `Set-Cookie` rotations - JSESSIONID, `lidc` and
     Cloudflare's `__cf_bm` all rotate, and pinning them goes stale, and
  3. speaks TLS as Chrome does, via curl_cffi (see the import below).

`csrf-token` must always echo the *current* JSESSIONID, so it is derived from
the cookie jar on every request rather than stored separately.
"""

from __future__ import annotations

import logging
import time

import httpx

from .config import Settings
from .errors import LinkedInError, ProfileNotFound, SessionExpired, Throttled

# Correct cookies are not enough. Python's TLS handshake has a different
# signature (JA3/JA4 - cipher order, extensions, HTTP/2 settings) from
# Chrome's, and LinkedIn/Cloudflare fingerprint it. A session whose cookies say
# "Chrome" but whose handshake says "Python" reads as a replayed cookie, and
# LinkedIn kills it. curl_cffi replays Chrome's actual handshake, so the
# connection matches the cookies.
#
# It is an optional import: without it the client still works, but expect
# LinkedIn to invalidate sessions after a handful of requests.
try:
    from curl_cffi import requests as curl_requests
    from curl_cffi.requests import exceptions as curl_exceptions

    HAS_CURL_CFFI = True
    _TIMEOUTS: tuple[type[Exception], ...] = (
        httpx.TimeoutException,
        curl_exceptions.Timeout,
    )
    _TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
        httpx.HTTPError,
        curl_exceptions.RequestException,
    )
except ImportError:  # pragma: no cover - exercised only where the wheel is absent
    HAS_CURL_CFFI = False
    _TIMEOUTS = (httpx.TimeoutException,)
    _TRANSPORT_ERRORS = (httpx.HTTPError,)

log = logging.getLogger(__name__)

API_BASE = "https://www.linkedin.com/voyager/api"

# Which browser curl_cffi should impersonate. Keep this in step with USER_AGENT
# below - a Chrome handshake paired with a Firefox user-agent is its own tell.
IMPERSONATE = "chrome"

# Seconds to wait before retrying a failed login. LinkedIn locks accounts that
# see repeated failed attempts, so back off hard rather than hammering.
LOGIN_RETRY_COOLDOWN = 900

# The projection asking dash for every profile section. Bump the version
# suffix if LinkedIn retires this one (symptom: 400/404 from dash_profile).
DASH_DECORATION = (
    "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-63"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def parse_cookie_header(raw: str) -> dict[str, str]:
    """'a=1; b=2' -> {'a': '1', 'b': '2'} - the browser's own cookie format."""
    jar: dict[str, str] = {}
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if name:
            jar[name] = value
    return jar


# LinkedIn and Cloudflare rotate several cookies mid-session - JSESSIONID, the
# `lidc` routing hint, and Cloudflare's short-lived `__cf_bm` bot-management
# token among them. A browser accepts every one of them, so we do too rather
# than keeping a whitelist: sending a stale __cf_bm or lidc is itself a signal
# that this is not the browser the session belongs to.


class VoyagerClient:
    """Fetches the raw JSON blobs that make up a profile page."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: httpx.AsyncClient | None = None
        self._login_failed_at = 0.0

        # Preferred: the whole cookie header, copied from a browser session.
        self._cookies = parse_cookie_header(settings.linkedin_cookie)

        # Fall back to the two-cookie form. It authenticates, but expect the
        # session to be short-lived - see the module docstring.
        if settings.linkedin_li_at:
            self._cookies.setdefault("li_at", settings.linkedin_li_at)
        if settings.linkedin_jsessionid:
            self._cookies["JSESSIONID"] = f'"{settings.linkedin_jsessionid.strip(chr(34))}"'

    # -- cookie state ------------------------------------------------------

    @property
    def jsessionid(self) -> str:
        """The CSRF token, always read from the current cookie state."""
        return (self._cookies.get("JSESSIONID") or "").strip('"')

    @property
    def li_at(self) -> str:
        return self._cookies.get("li_at", "")

    @property
    def has_full_cookies(self) -> bool:
        """Whether we have the browser-identity cookies that keep a session alive."""
        return bool(self._cookies.get("bcookie") or self._cookies.get("bscookie"))

    def _cookie_header(self) -> str:
        return "; ".join(f"{name}={value}" for name, value in self._cookies.items())

    def _adopt_rotations(self, resp) -> None:
        """Follow every Set-Cookie update instead of pinning stale values."""
        for name, value in resp.cookies.items():
            if not value or "delete me" in value:
                # "delete me" is LinkedIn ending the session, not a rotation.
                # Adopting it would wipe our own credential.
                continue
            if name == "JSESSIONID" and not value.startswith('"'):
                value = f'"{value}"'
            if self._cookies.get(name) != value:
                log.info("adopted rotated cookie: %s", name)
                self._cookies[name] = value

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        base_headers = {
            "user-agent": USER_AGENT,
            "accept-language": "en-US,en;q=0.9",
        }
        if HAS_CURL_CFFI:
            self._client = curl_requests.AsyncSession(
                impersonate=IMPERSONATE,
                timeout=self.settings.request_timeout,
                headers=base_headers,
            )
            log.info("HTTP transport: curl_cffi impersonating %s", IMPERSONATE)
        else:
            self._client = httpx.AsyncClient(
                timeout=self.settings.request_timeout,
                follow_redirects=True,
                headers=base_headers,
            )
            log.warning(
                "HTTP transport: httpx (curl_cffi not installed). LinkedIn "
                "fingerprints the TLS handshake and will invalidate sessions "
                "after a few requests. Install curl_cffi."
            )

    async def close(self) -> None:
        if self._client is None:
            return
        # curl_cffi's AsyncSession closes with close(), httpx's with aclose().
        closer = getattr(self._client, "aclose", None) or self._client.close
        result = closer()
        if hasattr(result, "__await__"):
            await result
        self._client = None

    @property
    def impersonating(self) -> bool:
        return HAS_CURL_CFFI

    def set_cookies(self, jar: dict[str, str]) -> None:
        """Replace the cookie state, e.g. after signing in with credentials."""
        self._cookies = dict(jar)

    async def try_relogin(self) -> bool:
        """Re-authenticate with configured credentials. True if it worked.

        This is what lets a deployed instance survive LinkedIn ending its
        session without anyone editing environment variables.

        Failures are rate-limited hard. Repeated failed logins get an account
        locked - a worse outcome than the expired session we are trying to
        recover from - and a dev server running with --reload would otherwise
        retry on every file save.
        """
        if not self.settings.can_login:
            return False

        since_failure = time.monotonic() - self._login_failed_at
        if since_failure < LOGIN_RETRY_COOLDOWN:
            log.info(
                "Skipping re-login: last attempt failed %.0fs ago (cooling down "
                "for %ds to avoid locking the account).",
                since_failure,
                LOGIN_RETRY_COOLDOWN,
            )
            return False

        # Imported here, not at module scope: app.auth imports this module.
        from .auth import LoginFailed, fetch_session_cookies

        try:
            jar = await fetch_session_cookies(
                self.settings.linkedin_email,
                self.settings.linkedin_password,
                self.settings.request_timeout,
            )
        except LoginFailed as exc:
            self._login_failed_at = time.monotonic()
            log.warning("Re-login failed: %s", exc)
            return False

        self.set_cookies(jar)
        self._login_failed_at = 0.0
        log.info("Authenticated with LinkedIn.")
        return True

    @property
    def authenticated(self) -> bool:
        """Both the session and the CSRF token are required."""
        return bool(self.li_at and self.jsessionid)

    # -- plumbing ----------------------------------------------------------

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        return {
            "csrf-token": self.jsessionid,
            "x-restli-protocol-version": "2.0.0",
            "x-li-lang": "en_US",
            "accept": accept,
            "referer": "https://www.linkedin.com/feed/",
            "cookie": self._cookie_header(),
        }

    def _clear_transport_jar(self) -> None:
        jar = getattr(self._client, "cookies", None)
        if jar is not None and hasattr(jar, "clear"):
            jar.clear()

    async def _get(
        self,
        path: str,
        params: dict | None = None,
        accept: str = "application/json",
    ) -> dict | None:
        """GET a Voyager endpoint. Returns None for endpoints that are simply
        unavailable for this profile (so callers can carry on without them)."""
        if not self._client:
            raise LinkedInError("HTTP client is not started.", status=503, code="not_ready")
        if not self.authenticated:
            raise LinkedInError(
                "No LinkedIn session configured on the server. Set LINKEDIN_COOKIE.",
                status=503,
                code="no_session",
            )

        url = f"{API_BASE}{path}"
        # We manage cookies by hand (see _adopt_rotations), so the transport's
        # own jar must stay empty - otherwise it appends a second, stale
        # JSESSIONID and the csrf-token header no longer matches it.
        self._clear_transport_jar()
        # Do NOT follow redirects on API calls. Voyager answers a valid
        # request with JSON; a redirect means it is sending us to the login
        # wall, i.e. the session is not accepted. Following that just bounces
        # between login pages until the client gives up with an opaque
        # "maximum redirects followed" error, hiding the real cause.
        no_redirects = (
            {"allow_redirects": False} if HAS_CURL_CFFI else {"follow_redirects": False}
        )
        try:
            resp = await self._client.get(
                url, params=params, headers=self._headers(accept), **no_redirects
            )
        except _TIMEOUTS as exc:
            raise LinkedInError(
                "LinkedIn did not respond in time.", status=504, code="upstream_timeout"
            ) from exc
        except _TRANSPORT_ERRORS as exc:
            raise LinkedInError(f"Could not reach LinkedIn: {exc}") from exc

        log.info("voyager GET %s -> %s", path, resp.status_code)
        self._adopt_rotations(resp)

        if 300 <= resp.status_code < 400:
            location = resp.headers.get("location", "")
            raise SessionExpired(
                "LinkedIn redirected the API call to the login page"
                + (f" ({location.split('?')[0]})" if location else "")
                + ". The session is not accepted from this machine. A common "
                "cause is deploying with cookies minted elsewhere: LinkedIn "
                "binds a session to the browser and network it was created on, "
                "so a cookie copied from a laptop is often rejected when "
                "replayed from a datacenter. Signing in from the server itself "
                "(LINKEDIN_EMAIL/LINKEDIN_PASSWORD) avoids the mismatch"
            )

        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                # A 200 that isn't JSON means we were served the login wall.
                raise SessionExpired("received HTML instead of JSON")

        if resp.status_code == 403 and "csrf" in resp.text.lower():
            hint = (
                "the session was invalidated - most likely because only li_at "
                "and JSESSIONID were supplied. Set LINKEDIN_COOKIE to the full "
                "cookie header from your browser"
                if not self.has_full_cookies
                else "the session was invalidated by LinkedIn; copy a fresh "
                "LINKEDIN_COOKIE from your browser"
            )
            raise SessionExpired(f"CSRF check failed - {hint}")
        if resp.status_code in (401, 403):
            raise SessionExpired(f"HTTP {resp.status_code}")
        if resp.status_code == 410:
            # The endpoint itself has been retired by LinkedIn - nothing to do
            # with this profile. Callers fall through to the newer endpoint.
            raise LinkedInError(
                f"LinkedIn has retired the endpoint {path} (HTTP 410).",
                status=502,
                code="endpoint_retired",
            )
        if resp.status_code == 404:
            return None
        if resp.status_code in (429, 999):
            raise Throttled()
        if resp.status_code >= 500:
            raise LinkedInError(
                f"LinkedIn returned HTTP {resp.status_code}.",
                status=502,
                code="upstream_error",
            )
        return None

    # -- the actual endpoints ---------------------------------------------

    async def dash_profile(self, public_id: str) -> dict:
        """The big one: identity, positions, education, skills, certifications,
        languages, projects, publications, honours, volunteering and courses,
        all in a single response.

        `decorationId` is LinkedIn's way of asking for a projection - it names
        how much of the object graph to expand. FullProfileWithEntities pulls
        in every profile section; without it you get the bare Profile entity
        and nothing else. The trailing number is the projection's version and
        is the part most likely to need bumping if LinkedIn moves on.
        """
        data = await self._get(
            "/identity/dash/profiles",
            params={
                "q": "memberIdentity",
                "memberIdentity": public_id,
                "decorationId": DASH_DECORATION,
            },
            # The "normalized" content type is what returns the flat
            # `included` graph that dash.Resolver walks.
            accept="application/vnd.linkedin.normalized+json+2.1",
        )
        if not data or not (data.get("included") or []):
            raise ProfileNotFound(public_id)
        return data

    async def contact_info(self, public_id: str) -> dict | None:
        """Emails, phone numbers, websites, Twitter, birthday.

        NOTE: as of this writing LinkedIn answers this legacy endpoint with
        HTTP 410, the same way it retired profileView - so this returns None
        in practice. It is kept behind `include_contact` (default off) rather
        than deleted: the contact card is the one thing the dash payload does
        not carry, and the call is harmless if LinkedIn restores it.
        Everything visible on the profile page itself comes from dash.
        """
        try:
            return await self._get(f"/identity/profiles/{public_id}/profileContactInfo")
        except LinkedInError:
            return None  # never fail the whole request over contact info

    async def public_html(self, public_id: str) -> str | None:
        """The logged-out profile page, used by the fallback parser."""
        if not self._client:
            return None
        self._clear_transport_jar()
        try:
            resp = await self._client.get(
                f"https://www.linkedin.com/in/{public_id}",
                # Deliberately cookie-less: the logged-OUT page is the one that
            # carries the schema.org JSON-LD block the fallback parses.
            headers={"accept": "text/html,application/xhtml+xml"},
            )
        except _TRANSPORT_ERRORS:
            return None
        return resp.text if resp.status_code == 200 else None
