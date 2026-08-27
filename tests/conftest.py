"""Test isolation.

`app.config` calls `load_dotenv()` at import time, so a developer's real
`.env` ends up in `os.environ` and leaks into every `Settings()` built during
a test run. That makes results depend on whether the machine happens to have
credentials configured. This strips those variables for the duration of each
test, so the suite behaves the same on a laptop and in CI.
"""

import os

import pytest

_LEAKY_VARS = [name for name in ("API_KEY", "CACHE_TTL_SECONDS",
                                 "RATE_LIMIT_PER_MINUTE", "REQUEST_TIMEOUT")]


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith("LINKEDIN_") or name in _LEAKY_VARS:
            monkeypatch.delenv(name, raising=False)
