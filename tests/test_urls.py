import pytest

from app.urls import InvalidProfileURL, extract_public_id


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://www.linkedin.com/in/williamhgates/", "williamhgates"),
        ("https://linkedin.com/in/williamhgates", "williamhgates"),
        ("http://www.linkedin.com/in/williamhgates", "williamhgates"),
        ("https://in.linkedin.com/in/ada-lovelace-123", "ada-lovelace-123"),
        ("www.linkedin.com/in/williamhgates", "williamhgates"),
        ("linkedin.com/in/williamhgates/", "williamhgates"),
        ("https://www.linkedin.com/in/williamhgates/?originalSubdomain=us", "williamhgates"),
        ("https://www.linkedin.com/in/williamhgates/detail/recent-activity/", "williamhgates"),
        ("https://www.linkedin.com/in/%E5%BC%A0%E4%BC%9F-1234", "张伟-1234"),
        ("  https://www.linkedin.com/in/williamhgates  ", "williamhgates"),
        ("williamhgates", "williamhgates"),
    ],
)
def test_accepts_profile_urls(value, expected):
    assert extract_public_id(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "https://twitter.com/in/someone",
        "https://www.linkedin.com/company/microsoft",
        "https://www.linkedin.com/school/mit",
        "https://www.linkedin.com/feed/",
        "https://www.linkedin.com/pub/someone/1/2/3",
        "not a url at all",
    ],
)
def test_rejects_everything_else(value):
    with pytest.raises(InvalidProfileURL):
        extract_public_id(value)
