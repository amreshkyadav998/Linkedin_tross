"""Turn whatever the caller sent us into a LinkedIn 'public identifier'.

The public identifier is the slug in a profile URL:

    https://www.linkedin.com/in/williamhgates/  ->  "williamhgates"

Everything downstream works off that slug.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

# LinkedIn runs country subdomains (in.linkedin.com, uk.linkedin.com, ...) and
# a couple of alternate hosts. All of them serve the same profiles.
_ALLOWED_HOST = re.compile(r"^([a-z]{2,3}\.)?(www\.)?linkedin\.com$", re.I)

# A slug is letters/digits/dashes/underscores/percent-escapes. Unicode slugs
# exist (e.g. /in/张伟-1234) so we allow non-ASCII too, but no slashes.
_SLUG_OK = re.compile(r"^[^/\s?#]{2,150}$")


class InvalidProfileURL(ValueError):
    """Raised when the input is not a usable LinkedIn profile URL."""


def extract_public_id(value: str) -> str:
    """Return the public identifier for a profile URL (or a bare slug).

    Raises InvalidProfileURL with a human-readable reason on bad input.
    """
    if not value or not value.strip():
        raise InvalidProfileURL("No profile URL was provided.")

    raw = value.strip()

    # Allow a bare slug ("williamhgates") as a convenience.
    if "/" not in raw and "." not in raw and " " not in raw:
        return _clean_slug(raw)

    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw

    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()

    if not _ALLOWED_HOST.match(host):
        raise InvalidProfileURL(
            f"'{host or value}' is not a linkedin.com URL."
        )

    parts = [p for p in parsed.path.split("/") if p]

    # /in/<slug> is the profile route. Anything else is a different entity.
    if len(parts) >= 2 and parts[0].lower() == "in":
        return _clean_slug(parts[1])

    if parts and parts[0].lower() in {"company", "school", "showcase"}:
        raise InvalidProfileURL(
            "That is a company/school page. This API only handles member "
            "profiles (linkedin.com/in/...)."
        )

    if parts and parts[0].lower() == "pub":
        raise InvalidProfileURL(
            "Legacy /pub/ URLs are not supported. Open the profile in a "
            "browser and copy the /in/... URL instead."
        )

    raise InvalidProfileURL(
        "Expected a profile URL of the form https://www.linkedin.com/in/<name>."
    )


def _clean_slug(slug: str) -> str:
    slug = unquote(slug).strip().strip("/")
    if not _SLUG_OK.match(slug):
        raise InvalidProfileURL(f"'{slug}' is not a valid profile identifier.")
    return slug


def canonical_url(public_id: str) -> str:
    return f"https://www.linkedin.com/in/{public_id}"
