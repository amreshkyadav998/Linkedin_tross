"""One exception type for everything that can go wrong upstream."""

from __future__ import annotations


class LinkedInError(Exception):
    """Something went wrong talking to LinkedIn.

    `status` is the HTTP status this API should return to its caller and
    `code` is a short machine-readable string for clients to switch on.
    """

    def __init__(self, message: str, *, status: int = 502, code: str = "upstream_error"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class ProfileNotFound(LinkedInError):
    def __init__(self, public_id: str):
        super().__init__(
            f"No LinkedIn profile found for '{public_id}'. It may be deleted, "
            "renamed, or not visible to the account this API is signed in as.",
            status=404,
            code="profile_not_found",
        )


class SessionExpired(LinkedInError):
    def __init__(self, detail: str = ""):
        super().__init__(
            "The LinkedIn session is no longer valid. Refresh the LINKEDIN_LI_AT "
            "cookie and redeploy." + (f" ({detail})" if detail else ""),
            status=502,
            code="linkedin_session_expired",
        )


class Throttled(LinkedInError):
    def __init__(self):
        super().__init__(
            "LinkedIn is rate-limiting this account. Wait a few minutes before "
            "retrying.",
            status=429,
            code="linkedin_rate_limited",
        )
