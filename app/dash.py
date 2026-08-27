"""Parser for LinkedIn's current profile API (the "dash" model).

LinkedIn retired the old `profileView` endpoint (it now answers HTTP 410) in
favour of `/identity/dash/profiles`. The response format changed shape
completely, so it gets its own module.

Where `profileView` returned one pre-nested tree, dash returns a *normalised
graph*:

    {
      "data":     { "*elements": ["urn:li:fsd_profile:ACoAA..."] },
      "included": [ {entityUrn: "urn:li:fsd_profile:...", ...},
                    {entityUrn: "urn:li:fsd_profileEducation:...", ...}, ... ]
    }

Every entity is flat in `included`, and references between them are string
URNs held in keys prefixed with `*`. A profile's education list is not nested
under the profile - the profile has `"*profileEducations": "urn:li:collection
Response:xyz"`, that collection is itself in `included`, and *its* `*elements`
are the URNs of the Education entities.

So the job is: index `included` by URN, then walk the pointers. `Resolver`
does that; the rest of the module maps entities onto the same output schema
that `normalize.py` produces, so the API contract does not change.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .normalize import (
    LANGUAGE_PROFICIENCY,
    _clean,
    _contact,
    _date,
    _image,
    _months_between,
    _pretty_enum,
    _range_text,
    _urn_id,
)


class Resolver:
    """Index `included` by entityUrn and follow `*`-prefixed pointers."""

    def __init__(self, payload: dict):
        self.payload = payload or {}
        self.included = self.payload.get("included") or []
        self.by_urn: dict[str, dict] = {
            e["entityUrn"]: e
            for e in self.included
            if isinstance(e, dict) and e.get("entityUrn")
        }

    def get(self, urn) -> dict | None:
        if not isinstance(urn, str):
            return None
        return self.by_urn.get(urn)

    def follow(self, entity: dict | None, key: str) -> dict | None:
        """entity["*key"] is a URN -> return the entity it points at."""
        if not isinstance(entity, dict):
            return None
        return self.get(entity.get(f"*{key}"))

    def collection(self, entity: dict | None, key: str) -> list[dict]:
        """entity["*key"] points at a CollectionResponse; return its members.

        Returns [] for absent or empty sections, which is the common case -
        most profiles do not fill in patents or test scores.
        """
        holder = self.follow(entity, key)
        if not holder:
            return []
        out = []
        for urn in holder.get("*elements") or []:
            item = self.get(urn)
            if item:
                out.append(item)
        return out

    def of_type(self, suffix: str) -> list[dict]:
        """All included entities whose $type ends with `.suffix`."""
        return [e for e in self.included if e.get("$type", "").endswith("." + suffix)]

    def root_profile(self) -> dict | None:
        """The Profile the query was about."""
        for urn in (self.payload.get("data") or {}).get("*elements") or []:
            entity = self.get(urn)
            if entity:
                return entity
        profiles = self.of_type("Profile")
        return profiles[0] if profiles else None


# ---------------------------------------------------------------------------
# helpers specific to the dash shape
# ---------------------------------------------------------------------------

def _range(node) -> dict:
    """dash uses dateRange:{start,end}; the old model used timePeriod."""
    node = node or {}
    start_raw, end_raw = node.get("start"), node.get("end")
    start, end = _date(start_raw), _date(end_raw)
    is_current = start is not None and end is None
    return {
        "start_date": start,
        "end_date": end,
        "is_current": is_current,
        "duration_months": _months_between(start_raw, end_raw),
        "date_range": _range_text(start, end, is_current),
    }


def _picture(node) -> dict | None:
    """Profile/company images hide the VectorImage one or two levels down."""
    if not isinstance(node, dict):
        return None
    for key in ("displayImageWithFrameReferenceUnion", "displayImageReferenceUnion",
                "originalImageReference", "logo", "vectorImage"):
        inner = node.get(key)
        if isinstance(inner, dict):
            found = _image(inner.get("vectorImage") or inner)
            if found:
                return found
    return _image(node)


def _company_of(res: Resolver, item: dict) -> dict:
    """Shared company block for positions / certifications / volunteering."""
    company = res.follow(item, "company")
    if not company:
        return {
            "company_id": _urn_id(item.get("companyUrn")),
            "company_linkedin_url": None,
            "company_logo": None,
            "company_industries": None,
        }
    industries = [
        name
        for name in (
            (res.get(urn) or {}).get("name") for urn in company.get("industryUrns") or []
        )
        if name
    ]
    return {
        "company_id": _urn_id(company.get("entityUrn")),
        "company_linkedin_url": _clean(company.get("url")),
        "company_logo": _picture(company.get("logo")),
        "company_industries": industries or None,
    }


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------

def _basics(res: Resolver, profile: dict) -> dict:
    first = _clean(profile.get("firstName"))
    last = _clean(profile.get("lastName"))
    full = " ".join(p for p in (first, last) if p) or None

    geo = res.follow(profile.get("geoLocation") or {}, "geo")
    country_code = _clean((profile.get("location") or {}).get("countryCode"))
    industry = res.get(profile.get("industryUrn"))

    # Geo gives the city (or country) without the country appended; the country
    # itself is only exposed as an ISO code on `location`.
    geo_name = None
    if geo:
        geo_name = _clean(
            geo.get("defaultLocalizedNameWithoutCountryName")
            or geo.get("defaultLocalizedName")
        )

    pronoun = (profile.get("pronounUnion") or {}).get("standardizedPronoun")

    return {
        "first_name": first,
        "last_name": last,
        "full_name": full,
        "headline": _clean(profile.get("headline")),
        "about": _clean(profile.get("summary")),
        "industry": _clean((industry or {}).get("name")),
        "location": {
            "text": geo_name or _clean(profile.get("locationName")),
            "country": None,
            "country_code": country_code.upper() if country_code else None,
            "postal_code": _clean((profile.get("location") or {}).get("postalCode")),
        },
        "pronoun": _pretty_enum(pronoun),
        "is_premium": profile.get("premium"),
        "is_influencer": profile.get("influencer"),
        "is_creator": profile.get("creator"),
        "birth_date": _date(profile.get("birthDateOn")),
        "profile_picture": _picture(profile.get("profilePicture")),
        "background_image": _picture(profile.get("backgroundPicture")),
    }


def _experience(res: Resolver, profile: dict) -> list[dict]:
    """Positions live under position groups (one group per employer), which is
    how LinkedIn renders promotions within the same company."""
    out = []
    for group in res.collection(profile, "profilePositionGroups"):
        positions = res.collection(group, "profilePositionInPositionGroup")
        for position in positions or [group]:
            employment = res.follow(position, "employmentType")
            out.append(
                {
                    "title": _clean(position.get("title")),
                    "company": _clean(
                        position.get("companyName") or group.get("companyName")
                    ),
                    "employment_type": _clean((employment or {}).get("name")),
                    "location": _clean(position.get("locationName")),
                    "description": _clean(position.get("description")),
                    **_range(position.get("dateRange")),
                    **_company_of(res, position if position.get("companyUrn") else group),
                }
            )
    return out


def _education(res: Resolver, profile: dict) -> list[dict]:
    out = []
    for item in res.collection(profile, "profileEducations"):
        school = res.follow(item, "school")
        out.append(
            {
                "school": _clean(item.get("schoolName") or (school or {}).get("name")),
                "degree": _clean(item.get("degreeName")),
                "field_of_study": _clean(item.get("fieldOfStudy")),
                "grade": _clean(item.get("grade")),
                "activities": _clean(item.get("activities")),
                "description": _clean(item.get("description")),
                "school_id": _urn_id(item.get("schoolUrn")),
                "school_url": _clean((school or {}).get("url")),
                "school_logo": _picture((school or {}).get("logo")),
                **_range(item.get("dateRange")),
            }
        )
    return out


def _certifications(res: Resolver, profile: dict) -> list[dict]:
    out = []
    for item in res.collection(profile, "profileCertifications"):
        period = _range(item.get("dateRange"))
        out.append(
            {
                "name": _clean(item.get("name")),
                "authority": _clean(item.get("authority")),
                "license_number": _clean(item.get("licenseNumber")),
                "url": _clean(item.get("url")),
                "issued_on": period["start_date"],
                "expires_on": period["end_date"],
                "company_logo": _company_of(res, item)["company_logo"],
            }
        )
    return out


def _skills(res: Resolver, profile: dict) -> list[str]:
    names, seen = [], set()
    for item in res.collection(profile, "profileSkills"):
        name = _clean(item.get("name"))
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names


def _languages(res: Resolver, profile: dict) -> list[dict]:
    return [
        {
            "name": _clean(item.get("name")),
            "proficiency": LANGUAGE_PROFICIENCY.get(
                item.get("proficiency"), _pretty_enum(item.get("proficiency"))
            ),
        }
        for item in res.collection(profile, "profileLanguages")
    ]


def _projects(res: Resolver, profile: dict) -> list[dict]:
    return [
        {
            "name": _clean(item.get("title")),
            "description": _clean(item.get("description")),
            "url": _clean(item.get("url")),
            **_range(item.get("dateRange")),
        }
        for item in res.collection(profile, "profileProjects")
    ]


def _publications(res: Resolver, profile: dict) -> list[dict]:
    return [
        {
            "name": _clean(item.get("name")),
            "publisher": _clean(item.get("publisher")),
            "description": _clean(item.get("description")),
            "url": _clean(item.get("url")),
            "published_on": _date((item.get("publishedOn") or {})),
        }
        for item in res.collection(profile, "profilePublications")
    ]


def _honors(res: Resolver, profile: dict) -> list[dict]:
    return [
        {
            "title": _clean(item.get("title")),
            "issuer": _clean(item.get("issuer")),
            "description": _clean(item.get("description")),
            "issued_on": _date(item.get("issuedOn")),
        }
        for item in res.collection(profile, "profileHonors")
    ]


def _volunteering(res: Resolver, profile: dict) -> list[dict]:
    return [
        {
            "role": _clean(item.get("role")),
            "organization": _clean(item.get("companyName")),
            "cause": _pretty_enum(item.get("cause")),
            "description": _clean(item.get("description")),
            **_range(item.get("dateRange")),
            **_company_of(res, item),
        }
        for item in res.collection(profile, "profileVolunteerExperiences")
    ]


def _courses(res: Resolver, profile: dict) -> list[dict]:
    return [
        {"name": _clean(item.get("name")), "number": _clean(item.get("number"))}
        for item in res.collection(profile, "profileCourses")
    ]


def _organizations(res: Resolver, profile: dict) -> list[dict]:
    return [
        {
            "name": _clean(item.get("name")),
            "position": _clean(item.get("position")),
            "description": _clean(item.get("description")),
            **_range(item.get("dateRange")),
        }
        for item in res.collection(profile, "profileOrganizations")
    ]


def _patents(res: Resolver, profile: dict) -> list[dict]:
    return [
        {
            "title": _clean(item.get("title")),
            "number": _clean(item.get("number")),
            "description": _clean(item.get("description")),
            "url": _clean(item.get("url")),
            "is_pending": item.get("pending"),
            "filed_on": _date(item.get("filingDate")),
            "issued_on": _date(item.get("issuedOn")),
        }
        for item in res.collection(profile, "profilePatents")
    ]


def _test_scores(res: Resolver, profile: dict) -> list[dict]:
    return [
        {
            "name": _clean(item.get("name")),
            "score": _clean(item.get("score")),
            "description": _clean(item.get("description")),
            "taken_on": _date(item.get("dateOn")),
        }
        for item in res.collection(profile, "profileTestScores")
    ]


def _network(res: Resolver, profile: dict) -> dict:
    relationship = res.follow(profile, "memberRelationship") or {}
    union = relationship.get("memberRelationshipUnion") or {}
    degree = None
    if "connection" in union:
        degree = "1st"
    elif "noConnection" in union:
        degree = "Out of network"
    elif "self" in union:
        degree = "Self"
    return {"followers": None, "connections": None, "degree": degree}


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def build_profile_from_dash(
    public_id: str,
    payload: dict,
    *,
    contact_info: dict | None = None,
) -> dict:
    """Assemble the API response from a dash `/identity/dash/profiles` payload."""
    res = Resolver(payload)
    profile = res.root_profile()
    if not profile:
        raise ValueError("no Profile entity in the dash payload")

    experience = _experience(res, profile)
    education = _education(res, profile)
    skills = _skills(res, profile)

    return {
        "public_id": _clean(profile.get("publicIdentifier")) or public_id,
        "profile_url": f"https://www.linkedin.com/in/{public_id}",
        "member_id": _urn_id(profile.get("objectUrn")),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "basics": _basics(res, profile),
        "network": _network(res, profile),
        "contact": _contact(contact_info),
        "experience": experience,
        "education": education,
        "skills": skills,
        "certifications": _certifications(res, profile),
        "languages": _languages(res, profile),
        "projects": _projects(res, profile),
        "publications": _publications(res, profile),
        "honors": _honors(res, profile),
        "volunteering": _volunteering(res, profile),
        "courses": _courses(res, profile),
        "organizations": _organizations(res, profile),
        "patents": _patents(res, profile),
        "test_scores": _test_scores(res, profile),
        "counts": {
            "experience": len(experience),
            "education": len(education),
            "skills": len(skills),
        },
    }
