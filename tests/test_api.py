"""End-to-end route tests. LinkedIn itself is stubbed out - these check our
own contract: validation, error shape, caching and the response envelope."""

import pytest
from fastapi.testclient import TestClient

from app import main
from app.errors import ProfileNotFound


@pytest.fixture
def client(monkeypatch):
    async def fake_fetch(_client, public_id, *, include_contact=True, include_raw=False):
        if public_id == "ghost":
            raise ProfileNotFound(public_id)
        return {
            "public_id": public_id,
            "source": "voyager",
            "basics": {"full_name": "Ada Lovelace"},
            "contact_requested": include_contact,
        }

    monkeypatch.setattr(main, "fetch_profile", fake_fetch)
    main.cache.clear()
    with TestClient(main.app) as test_client:
        yield test_client


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["linkedin_session"] in {"configured", "missing"}


def test_docs_and_home_are_served(client):
    assert client.get("/").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_get_profile(client):
    res = client.get("/api/v1/profile", params={"url": "https://www.linkedin.com/in/adalovelace/"})
    assert res.status_code == 200
    body = res.json()
    assert body["public_id"] == "adalovelace"
    assert body["cached"] is False


def test_second_call_is_served_from_cache(client):
    params = {"url": "https://www.linkedin.com/in/adalovelace/"}
    client.get("/api/v1/profile", params=params)
    body = client.get("/api/v1/profile", params=params).json()
    assert body["cached"] is True


def test_refresh_bypasses_the_cache(client):
    params = {"url": "https://www.linkedin.com/in/adalovelace/"}
    client.get("/api/v1/profile", params=params)
    body = client.get("/api/v1/profile", params={**params, "refresh": "true"}).json()
    assert body["cached"] is False


def test_post_profile(client):
    res = client.post(
        "/api/v1/profile",
        json={"url": "linkedin.com/in/adalovelace", "include_contact": False},
    )
    assert res.status_code == 200
    assert res.json()["contact_requested"] is False


def test_bad_url_returns_400_with_error_shape(client):
    res = client.get("/api/v1/profile", params={"url": "https://example.com/in/x"})
    assert res.status_code == 400
    body = res.json()
    assert body["code"] == "invalid_url"
    assert body["status"] == 400
    assert "error" in body


def test_missing_profile_returns_404(client):
    res = client.get("/api/v1/profile", params={"url": "https://www.linkedin.com/in/ghost"})
    assert res.status_code == 404
    assert res.json()["code"] == "profile_not_found"


def test_api_key_is_enforced_when_configured(client, monkeypatch):
    monkeypatch.setattr(main.settings, "api_key", "s3cret")
    params = {"url": "https://www.linkedin.com/in/adalovelace/"}

    assert client.get("/api/v1/profile", params=params).status_code == 401
    ok = client.get("/api/v1/profile", params=params, headers={"X-API-Key": "s3cret"})
    assert ok.status_code == 200


def test_rate_limit(client, monkeypatch):
    monkeypatch.setattr(main.limiter, "per_minute", 2)
    main.limiter._hits.clear()
    params = {"url": "https://www.linkedin.com/in/adalovelace/", "refresh": "true"}

    assert client.get("/api/v1/profile", params=params).status_code == 200
    assert client.get("/api/v1/profile", params=params).status_code == 200
    blocked = client.get("/api/v1/profile", params=params)
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "rate_limited"


async def test_public_fallback_can_be_disabled(monkeypatch):
    """The brief asks for a purely reverse-engineered solution, so it must be
    possible to run with the secondary HTML path switched off entirely - and
    when it is off, the page must not even be requested."""
    from app.service import _try_public_page

    async def must_not_be_called(_public_id):
        raise AssertionError("public page was fetched despite being disabled")

    monkeypatch.setattr(main.settings, "enable_public_fallback", False)
    monkeypatch.setattr(main.client, "public_html", must_not_be_called)

    assert await _try_public_page(main.client, "anyone") is None


async def test_public_fallback_is_used_when_enabled(monkeypatch):
    from app.service import _try_public_page

    called = []

    async def fake_html(public_id):
        called.append(public_id)
        return "<html><head></head><body>no json-ld here</body></html>"

    monkeypatch.setattr(main.settings, "enable_public_fallback", True)
    monkeypatch.setattr(main.client, "public_html", fake_html)

    # No JSON-LD in that page, so it parses to None - but it was fetched.
    assert await _try_public_page(main.client, "anyone") is None
    assert called == ["anyone"]
