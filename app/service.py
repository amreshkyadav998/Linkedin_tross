"""Orchestration: fetch the raw payloads, normalise them, fall back if needed.

The route handlers stay thin; all the "what do we call and in what order"
logic lives here.
"""

from __future__ import annotations

import logging

from .dash import build_profile_from_dash
from .errors import LinkedInError, ProfileNotFound, SessionExpired
from .fallback import build_from_public_page
from .linkedin import VoyagerClient

log = logging.getLogger(__name__)


async def fetch_profile(
    client: VoyagerClient,
    public_id: str,
    *,
    include_contact: bool = True,
    include_raw: bool = False,
) -> dict:
    """Return the normalised profile document.

    Tries the authenticated Voyager API first. If that is unavailable for any
    reason, falls back to parsing the logged-out public page so the caller
    still gets something useful.
    """
    try:
        payload = await _dash_with_relogin(client, public_id)
    except LinkedInError as exc:
        log.warning("voyager failed for %s: %s", public_id, exc)
        fallback = await _try_public_page(client, public_id)
        if fallback:
            fallback["source"] = "public_page"
            fallback["warning"] = (
                "Returned from the public profile page because the LinkedIn API "
                f"call failed ({exc.code}). Some sections are unavailable."
            )
            return fallback
        raise

    # The contact card is the one thing dash does not carry. It is a
    # nice-to-have, so it never fails the request.
    contact = await client.contact_info(public_id) if include_contact else None

    profile = build_profile_from_dash(public_id, payload, contact_info=contact)
    profile["source"] = "voyager"
    if include_raw:
        profile["raw"] = {"dash_profile": payload, "contact_info": contact}
    return profile


async def _dash_with_relogin(client: VoyagerClient, public_id: str) -> dict:
    """Fetch the profile, re-authenticating once if the session has ended.

    Any failure here still raises LinkedInError, so the caller's public-page
    fallback applies whether the session was recoverable or not.
    """
    try:
        return await client.dash_profile(public_id)
    except SessionExpired:
        if not await client.try_relogin():
            raise
        return await client.dash_profile(public_id)


async def _try_public_page(client: VoyagerClient, public_id: str) -> dict | None:
    html = await client.public_html(public_id)
    if not html:
        return None
    try:
        return build_from_public_page(public_id, html)
    except Exception:  # a parser hiccup must not mask the original error
        log.exception("public page parsing failed for %s", public_id)
        return None


__all__ = ["fetch_profile", "ProfileNotFound"]
