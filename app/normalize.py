"""Shared normalisation helpers.

`dash.py` maps LinkedIn's entity graph onto our response schema; this module
holds the small, format-agnostic pieces it leans on - date handling, image
URLs, URN parsing, enum prettifying - plus the contact-card mapping, which
still comes from a legacy endpoint with its own shape.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

MONTHS = [
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

LANGUAGE_PROFICIENCY = {
    "NATIVE_OR_BILINGUAL": "Native or bilingual proficiency",
    "FULL_PROFESSIONAL": "Full professional proficiency",
    "PROFESSIONAL_WORKING": "Professional working proficiency",
    "LIMITED_WORKING": "Limited working proficiency",
    "ELEMENTARY": "Elementary proficiency",
}


def _clean(value):
    """Drop empty strings/lists/dicts so the response stays readable."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, (list, dict)) and not value:
        return None
    return value


def _image(node) -> dict | None:
    """Read LinkedIn's VectorImage shape into {url, sizes}.

    The raw form is a root URL plus one path segment per rendered size:
        rootUrl + artifacts[i].fileIdentifyingUrlPathSegment
    We expose every size and pick the largest as the default `url`.
    """
    if not isinstance(node, dict):
        return None

    # Some payloads wrap the image in a union key like "com.linkedin.common.VectorImage".
    if "rootUrl" not in node:
        for key, value in node.items():
            if key.startswith("com.linkedin.") and isinstance(value, dict):
                node = value
                break

    root = node.get("rootUrl")
    artifacts = node.get("artifacts") or []
    if not root or not artifacts:
        return None

    sizes = []
    for art in artifacts:
        segment = art.get("fileIdentifyingUrlPathSegment")
        if not segment:
            continue
        sizes.append(
            {
                "width": art.get("width"),
                "height": art.get("height"),
                "url": root + segment,
            }
        )
    if not sizes:
        return None

    sizes.sort(key=lambda s: (s["width"] or 0) * (s["height"] or 0))
    return {"url": sizes[-1]["url"], "sizes": sizes}


def _date(node) -> dict | None:
    """{'month': 3, 'year': 2020} -> {'year':2020,'month':3,'text':'Mar 2020'}"""
    if not isinstance(node, dict):
        return None
    year, month, day = node.get("year"), node.get("month"), node.get("day")
    if not any([year, month, day]):
        return None

    parts = []
    if month and 1 <= month <= 12:
        parts.append(MONTHS[month] + (f" {day}" if day else ""))
    elif day:
        parts.append(str(day))
    if year:
        parts.append(str(year))

    return {"year": year, "month": month, "day": day, "text": " ".join(parts) or None}


def _months_between(start: dict | None, end: dict | None) -> int | None:
    """Inclusive month count; an absent end date means 'still going'."""
    if not start or not start.get("year"):
        return None
    end = end or {}
    now = datetime.now(timezone.utc)
    ongoing = not end.get("year")
    end_year = end.get("year") or now.year
    end_month = end.get("month") or (now.month if ongoing else 12)
    months = (end_year - start["year"]) * 12 + (end_month - (start.get("month") or 1)) + 1
    return months if months > 0 else None


def _range_text(start, end, is_current) -> str | None:
    if not start and not end:
        return None
    left = start["text"] if start else "?"
    right = "Present" if is_current else (end["text"] if end else "?")
    return f"{left} - {right}"


def _urn_id(urn) -> str | None:
    """urn:li:fsd_company:1441 -> '1441'  (also handles the (a,b) form)."""
    if not isinstance(urn, str) or not urn:
        return None
    tail = urn.rsplit(":", 1)[-1].strip("()")
    return tail.split(",")[0] or None


def _pretty_enum(value) -> str | None:
    """FULL_TIME -> 'Full time'."""
    if not isinstance(value, str) or not value:
        return None
    return re.sub(r"[_\s]+", " ", value).strip().capitalize()


def _contact(payload) -> dict:
    """Map the legacy contact-info card. Everything here is optional and is
    only ever visible for close connections."""
    payload = payload or {}
    websites = []
    for site in payload.get("websites") or []:
        label = None
        for _key, value in (site.get("type") or {}).items():
            if isinstance(value, dict):
                label = _pretty_enum(value.get("category") or value.get("label"))
        websites.append({"url": _clean(site.get("url")), "label": label})

    return {
        "emails": [e for e in [_clean(payload.get("emailAddress"))] if e],
        "phone_numbers": [
            {"number": _clean(p.get("number")), "type": _pretty_enum(p.get("type"))}
            for p in (payload.get("phoneNumbers") or [])
        ],
        "websites": websites,
        "twitter": [
            _clean(t.get("name"))
            for t in (payload.get("twitterHandles") or [])
            if t.get("name")
        ],
        "address": _clean(payload.get("address")),
        "birthday": _date(payload.get("birthDateOn")),
    }
