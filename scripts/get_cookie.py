"""Sign in to LinkedIn and print a ready-to-paste LINKEDIN_COOKIE line.

    python scripts/get_cookie.py you@example.com
    python scripts/get_cookie.py +919876543210

The password is prompted for, so it never lands in your shell history.

Why run this locally rather than letting the server log in? A cloud host's IP
is unfamiliar to LinkedIn and is far more likely to trigger a checkpoint. Mint
the session from your own machine, then paste the printed line into your
host's secrets.
"""

import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import LoginFailed, fetch_session_cookies, looks_like_phone  # noqa: E402


async def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    username = sys.argv[1]
    kind = "phone" if looks_like_phone(username) else "email"
    password = getpass.getpass(f"LinkedIn password for {username} ({kind}): ")

    try:
        jar = await fetch_session_cookies(username, password)
    except LoginFailed as exc:
        print(f"\nLogin failed: {exc}", file=sys.stderr)
        return 1

    header = "; ".join(f"{name}={value}" for name, value in jar.items())
    print(f"\nCaptured {len(jar)} cookies. Add this to your .env or host secrets:\n")
    print(f"LINKEDIN_COOKIE='{header}'")

    if not (jar.get("bcookie") or jar.get("bscookie")):
        print(
            "\nWarning: no bcookie/bscookie captured - this session will be "
            "short-lived.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
