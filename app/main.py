"""FastAPI application: routes, auth, rate limiting, error handling."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .cache import RateLimiter, TTLCache
from .config import get_settings
from .errors import LinkedInError
from .linkedin import VoyagerClient
from .service import fetch_profile
from .urls import InvalidProfileURL, extract_public_id

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
log = logging.getLogger("linkedin-api")

settings = get_settings()
cache = TTLCache(settings.cache_ttl_seconds)
limiter = RateLimiter(settings.rate_limit_per_minute)
client = VoyagerClient(settings)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await client.start()

    # No cookie header configured? Sign in with credentials instead. This
    # captures the whole jar, including the browser-identity cookies, so it
    # lands in "full" mode rather than the short-lived partial one.
    # try_relogin() carries the cooldown that stops a --reload loop from
    # hammering LinkedIn's login endpoint and locking the account.
    if not client.authenticated and settings.can_login:
        await client.try_relogin()

    if client.authenticated and client.has_full_cookies:
        log.info("LinkedIn session is configured (full browser cookies).")
    elif client.authenticated:
        log.warning(
            "LinkedIn session is configured, but only partial cookies were "
            "supplied. LinkedIn ties li_at to the browser identity in "
            "bcookie/bscookie and will invalidate this session after a few "
            "requests. Set LINKEDIN_COOKIE to the full cookie header."
        )
    else:
        log.warning(
            "No LinkedIn session - the API will only serve public-page fallback data. "
            "Set LINKEDIN_COOKIE to enable the full response."
        )

    yield
    await client.close()


app = FastAPI(
    title="LinkedIn Profile API",
    version=__version__,
    description=(
        "Give it a LinkedIn profile URL, get back structured JSON: name, "
        "headline, location, about, experience, education, skills, "
        "certifications, languages and images.\n\n"
        "Interactive docs are on this page; a minimal demo UI is at `/`."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# request/response models (these drive the OpenAPI docs)
# ---------------------------------------------------------------------------

class ProfileRequest(BaseModel):
    url: str = Field(
        ...,
        description="A LinkedIn profile URL, or just the slug.",
        examples=["https://www.linkedin.com/in/williamhgates/"],
    )
    include_contact: bool = Field(
        False,
        description=(
            "Also try the legacy contact-info card. LinkedIn currently "
            "answers that endpoint with HTTP 410, so this usually adds a "
            "wasted request and no data."
        ),
    )
    raw: bool = Field(
        False, description="Attach LinkedIn's untouched payload under `raw`."
    )
    refresh: bool = Field(False, description="Bypass the server-side cache.")


class ErrorBody(BaseModel):
    error: str
    code: str
    status: int


# ---------------------------------------------------------------------------
# dependencies
# ---------------------------------------------------------------------------

def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """No-op unless API_KEY is configured on the server."""
    if settings.api_key and x_api_key != settings.api_key:
        raise LinkedInError(
            "Missing or invalid X-API-Key header.", status=401, code="unauthorized"
        )


def enforce_rate_limit(request: Request) -> None:
    client_ip = _client_ip(request)
    if not limiter.check(client_ip):
        raise LinkedInError(
            f"Rate limit exceeded ({settings.rate_limit_per_minute} requests/minute). "
            f"Retry in {limiter.retry_after(client_ip)}s.",
            status=429,
            code="rate_limited",
        )


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# error handling - every failure comes back as the same JSON shape
# ---------------------------------------------------------------------------

@app.exception_handler(LinkedInError)
async def _linkedin_error(_request: Request, exc: LinkedInError):
    return JSONResponse(
        status_code=exc.status,
        content={"error": exc.message, "code": exc.code, "status": exc.status},
    )


@app.exception_handler(InvalidProfileURL)
async def _invalid_url(_request: Request, exc: InvalidProfileURL):
    return JSONResponse(
        status_code=400,
        content={"error": str(exc), "code": "invalid_url", "status": 400},
    )


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", tags=["meta"], summary="Liveness and session status")
async def health():
    return {
        "status": "ok",
        "version": __version__,
        "linkedin_session": (
            "configured" if client.authenticated else "missing"
        ),
        "cookie_mode": (
            "full" if client.has_full_cookies
            else "partial" if client.authenticated
            else "none"
        ),
        "transport": "curl_cffi-chrome" if client.impersonating else "httpx",
        "cached_profiles": cache.size,
    }


@app.get(
    "/api/v1/profile",
    tags=["profile"],
    summary="Scrape a LinkedIn profile",
    responses={
        400: {"model": ErrorBody, "description": "The URL is not a profile URL"},
        401: {"model": ErrorBody, "description": "Bad or missing API key"},
        404: {"model": ErrorBody, "description": "Profile not found or not visible"},
        429: {"model": ErrorBody, "description": "Rate limited by us or by LinkedIn"},
        502: {"model": ErrorBody, "description": "LinkedIn session expired or blocked"},
    },
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
async def get_profile(
    url: str = Query(
        ...,
        description="LinkedIn profile URL (or slug).",
        examples=["https://www.linkedin.com/in/williamhgates/"],
    ),
    include_contact: bool = Query(
        False,
        description=(
            "Also try the legacy contact-info card (LinkedIn currently "
            "returns HTTP 410 for it)."
        ),
    ),
    raw: bool = Query(False, description="Attach LinkedIn's untouched payload."),
    refresh: bool = Query(False, description="Bypass the server-side cache."),
):
    return await _resolve(url, include_contact, raw, refresh)


@app.post(
    "/api/v1/profile",
    tags=["profile"],
    summary="Scrape a LinkedIn profile (JSON body)",
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
async def post_profile(body: ProfileRequest):
    return await _resolve(body.url, body.include_contact, body.raw, body.refresh)


async def _resolve(url: str, include_contact: bool, raw: bool, refresh: bool) -> dict:
    public_id = extract_public_id(url)  # raises InvalidProfileURL -> 400
    cache_key = f"{public_id}:{int(include_contact)}"

    if not refresh and not raw:
        hit = cache.get(cache_key)
        if hit:
            return {**hit, "cached": True}

    profile = await fetch_profile(
        client, public_id, include_contact=include_contact, include_raw=raw
    )
    profile["cached"] = False

    if not raw:
        cache.set(cache_key, profile)
    return profile
