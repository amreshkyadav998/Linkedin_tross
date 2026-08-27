"""Fallback parser for the logged-out public profile page.

When the Voyager session is missing or rejected we can still get a useful
subset of the profile from the public page, which embeds a schema.org
JSON-LD block. It is far thinner than Voyager - no skills, no about text,
no dates on most entries - but it keeps the API answering instead of 502-ing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from selectolax.parser import HTMLParser


def _first(value):
    """LinkedIn writes some JSON-LD fields as either a value or a list."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _text(value) -> str | None:
    value = _first(value)
    if isinstance(value, str):
        return value.strip() or None
    return None


def _person(html: str) -> dict | None:
    """Pull the schema.org Person node out of the page's JSON-LD."""
    tree = HTMLParser(html)
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except (ValueError, TypeError):
            continue
        graph = data.get("@graph") if isinstance(data, dict) else None
        candidates = graph if isinstance(graph, list) else [data]
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "Person":
                return item
    return None


def _year(value) -> dict | None:
    """JSON-LD dates arrive as '2020' or '2020-03'."""
    text = _text(value)
    if not text:
        return None
    bits = text.split("-")
    try:
        year = int(bits[0])
    except ValueError:
        return None
    month = int(bits[1]) if len(bits) > 1 and bits[1].isdigit() else None
    return {"year": year, "month": month, "day": None, "text": text}


def _org_entries(items, role_key: str) -> list[dict]:
    """alumniOf / worksFor entries share a shape: an org, optionally wrapped
    in an OrganizationRole that carries the dates and title."""
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"))
        start = _year(item.get("startDate"))
        end = _year(item.get("endDate"))
        role = _text(item.get(role_key))

        member = _first(item.get("member"))
        if isinstance(member, dict):
            start = start or _year(member.get("startDate"))
            end = end or _year(member.get("endDate"))

        out.append(
            {
                "name": name,
                "role": role,
                "url": _text(item.get("url")),
                "start_date": start,
                "end_date": end,
                "is_current": bool(start and not end),
                "location": _text(item.get("location")),
                "description": _text(item.get("description")),
            }
        )
    return out


def build_from_public_page(public_id: str, html: str) -> dict | None:
    """Return a partial profile in the same top-level shape, or None."""
    person = _person(html)
    if not person:
        return None

    address = _first(person.get("address")) or {}
    image = _first(person.get("image")) or {}
    locality = _text(address.get("addressLocality")) if isinstance(address, dict) else None
    country = _text(address.get("addressCountry")) if isinstance(address, dict) else None

    experience = [
        {
            "title": entry["role"],
            "company": entry["name"],
            "location": entry["location"],
            "description": entry["description"],
            "start_date": entry["start_date"],
            "end_date": entry["end_date"],
            "is_current": entry["is_current"],
            "company_linkedin_url": entry["url"],
        }
        for entry in _org_entries(person.get("worksFor"), "jobTitle")
    ]

    education = [
        {
            "school": entry["name"],
            "degree": entry["role"],
            "start_date": entry["start_date"],
            "end_date": entry["end_date"],
            "school_url": entry["url"],
        }
        for entry in _org_entries(person.get("alumniOf"), "degree")
    ]

    picture_url = _text(image.get("contentUrl")) if isinstance(image, dict) else None

    return {
        "public_id": public_id,
        "profile_url": f"https://www.linkedin.com/in/{public_id}",
        "member_id": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "basics": {
            "full_name": _text(person.get("name")),
            "headline": _text(person.get("jobTitle")) or _text(person.get("description")),
            "about": _text(person.get("description")),
            "location": {
                "text": ", ".join(p for p in (locality, country) if p) or None,
                "country": country,
            },
            "profile_picture": {"url": picture_url} if picture_url else None,
        },
        "experience": experience,
        "education": education,
        "languages": [
            {"name": _text(lang.get("name")) if isinstance(lang, dict) else _text(lang)}
            for lang in (person.get("knowsLanguage") or [])
        ],
        "skills": [],
        "awards": [a for a in (person.get("awards") or []) if isinstance(a, str)],
        "counts": {
            "experience": len(experience),
            "education": len(education),
            "skills": 0,
        },
    }
