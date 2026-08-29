"""All configuration comes from environment variables (or a local .env file).

Nothing secret is ever hard-coded here - see .env.example for the full list.
"""

from functools import lru_cache

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- LinkedIn session -------------------------------------------------
    # Preferred: the whole cookie header copied from a browser (see README).
    linkedin_cookie: str = ""
    # Legacy two-cookie form; works, but the session is short-lived.
    linkedin_li_at: str = ""
    linkedin_jsessionid: str = ""
    linkedin_email: str = ""
    linkedin_password: str = ""

    # --- This API ---------------------------------------------------------
    api_key: str = ""
    # The public-page fallback is a degraded secondary path. Turn it off to
    # prove the reverse-engineered API path is doing all the work.
    enable_public_fallback: bool = True
    cache_ttl_seconds: int = 3600
    rate_limit_per_minute: int = 20
    request_timeout: int = 25

    @property
    def has_session(self) -> bool:
        """True when we have enough to talk to the authenticated Voyager API."""
        return bool(self.linkedin_cookie or self.linkedin_li_at)

    @property
    def can_login(self) -> bool:
        return bool(self.linkedin_email and self.linkedin_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()
