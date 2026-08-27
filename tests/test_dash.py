"""Tests for the dash parser - the URN graph walk and the output schema.

The fixture is synthetic but mirrors the exact shape LinkedIn returns:
entities flat in `included`, cross-referenced by `*`-prefixed URN pointers.
"""

import json
from pathlib import Path

import pytest

from app.dash import Resolver, build_profile_from_dash

FIXTURE = Path(__file__).parent / "fixtures" / "dash_profile_sample.json"


@pytest.fixture(scope="module")
def payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def profile(payload):
    contact = {
        "emailAddress": "ada@example.org",
        "phoneNumbers": [{"type": "MOBILE", "number": "+44 20 7946 0000"}],
        "websites": [
            {
                "url": "https://example.org",
                "type": {
                    "com.linkedin.voyager.identity.profile.StandardWebsite": {
                        "category": "PERSONAL"
                    }
                },
            }
        ],
        "twitterHandles": [{"name": "adalovelace"}],
    }
    return build_profile_from_dash("adalovelace", payload, contact_info=contact)


# --- the resolver ---------------------------------------------------------

def test_resolver_indexes_and_follows_pointers(payload):
    res = Resolver(payload)
    root = res.root_profile()
    assert root["firstName"] == "Ada"

    # a "*key" pointer resolves to the entity it names
    industry = res.get(root["industryUrn"])
    assert industry["name"] == "Computer Software"

    # a collection pointer resolves through CollectionResponse to its members
    skills = res.collection(root, "profileSkills")
    assert len(skills) == 3


def test_resolver_is_forgiving_about_missing_data():
    res = Resolver({})
    assert res.root_profile() is None
    assert res.get("urn:li:nope") is None
    assert res.get(None) is None
    assert res.collection(None, "profileSkills") == []
    assert res.follow({"*x": "urn:li:missing"}, "x") is None


def test_empty_sections_resolve_to_empty_lists(profile):
    for section in ("honors", "patents", "courses", "test_scores",
                    "publications", "organizations", "volunteering"):
        assert profile[section] == [], section


# --- the mapped output ----------------------------------------------------

def test_basics(profile):
    basics = profile["basics"]
    assert basics["full_name"] == "Ada Lovelace"
    assert basics["about"] == "I write algorithms for machines that do not exist yet."
    assert basics["industry"] == "Computer Software"
    assert basics["location"]["text"] == "London"
    assert basics["location"]["country_code"] == "GB"
    assert basics["pronoun"] == "She her"
    assert basics["is_premium"] is True
    assert profile["member_id"] == "12345"
    assert profile["public_id"] == "adalovelace"


def test_profile_picture_unwraps_the_vector_image(profile):
    picture = profile["basics"]["profile_picture"]
    assert picture["url"] == "https://media.licdn.com/dms/image/ROOT/shrink_400_400/photo.jpg"
    assert len(picture["sizes"]) == 2


def test_experience_walks_position_groups(profile):
    first, second = profile["experience"]

    assert first["title"] == "Chief Algorithm Designer"
    assert first["company"] == "Analytical Engine Co"
    assert first["employment_type"] == "Full-time"
    assert first["is_current"] is True
    assert first["date_range"].startswith("Mar 1843 - Present")
    assert first["company_linkedin_url"] == (
        "https://www.linkedin.com/company/analytical-engine/"
    )
    assert first["company_industries"] == ["Computer Hardware"]
    assert first["company_logo"]["url"].endswith("logo_200_200/l.png")

    assert second["is_current"] is False
    assert second["date_range"] == "Jan 1842 - Dec 1843"
    assert second["duration_months"] == 24


def test_education(profile):
    education = profile["education"][0]
    assert education["school"] == "Private Tutoring"
    assert education["field_of_study"] == "Mathematics and Logic"
    assert education["grade"] == "Distinction"
    assert education["school_url"].endswith("/school/private-tutoring/")
    assert education["date_range"] == "1829 - 1835"


def test_skills_are_deduplicated_case_insensitively(profile):
    assert profile["skills"] == ["Mathematics", "Algorithms"]
    assert profile["counts"]["skills"] == 2


def test_certifications_and_languages(profile):
    cert = profile["certifications"][0]
    assert cert["name"] == "Certificate in Analytical Engines"
    assert cert["authority"] == "Royal Society"
    assert cert["issued_on"]["text"] == "Jun 1843"
    assert cert["expires_on"] is None

    assert profile["languages"][0] == {
        "name": "English",
        "proficiency": "Native or bilingual proficiency",
    }


def test_projects_and_network(profile):
    assert profile["projects"][0]["name"] == "Note G"
    assert profile["network"]["degree"] == "1st"


def test_contact_card(profile):
    contact = profile["contact"]
    assert contact["emails"] == ["ada@example.org"]
    assert contact["twitter"] == ["adalovelace"]
    assert contact["websites"][0]["label"] == "Personal"
    assert contact["phone_numbers"][0]["type"] == "Mobile"


def test_counts(profile):
    assert profile["counts"] == {"experience": 2, "education": 1, "skills": 2}


def test_payload_without_a_profile_is_rejected():
    with pytest.raises(ValueError):
        build_profile_from_dash("nobody", {"data": {}, "included": []})
