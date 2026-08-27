---
title: LinkedIn Profile API
emoji: 🔗
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 8000
pinned: false
short_description: Turn a LinkedIn profile URL into structured JSON
---

# LinkedIn Profile API

Give it a LinkedIn profile URL, get back the profile as structured JSON.

```bash
curl "https://<your-app>.onrender.com/api/v1/profile?url=https://www.linkedin.com/in/williamhgates/"
```

```jsonc
{
  "public_id": "williamhgates",
  "source": "voyager",
  "basics": { "full_name": "...", "headline": "...", "location": { ... }, "profile_picture": { ... } },
  "experience": [ ... ],
  "education": [ ... ],
  "skills": [ ... ],
  "certifications": [ ... ],
  "languages": [ ... ]
}
```

A full sample response is in [`examples/response.json`](examples/response.json).

**Live demo UI:** `/` &nbsp;·&nbsp; **Interactive docs:** `/docs` &nbsp;·&nbsp; **Health:** `/health`

---

## Contents

1. [Approach](#approach)
2. [Quick start (local)](#quick-start-local)
3. [Getting a LinkedIn session](#getting-a-linkedin-session)
4. [Deploying](#deploying)
5. [API documentation](#api-documentation)
6. [Response schema](#response-schema)
7. [Project layout](#project-layout)
8. [Known limitations](#known-limitations)

---

## Approach

### The short version

LinkedIn's website is a single-page app. When you open a profile, your browser
calls a private JSON API at `linkedin.com/voyager/api/...` - **Voyager** - and
renders the result. That API returns the profile page's data already
structured. So rather than scraping HTML, this service **replays the same call
the browser makes**, using a real logged-in session.

Getting that to work reliably meant solving three things that are not
documented anywhere, each of which failed in a way that pointed at the next.

**1. The obvious endpoint is dead.** Almost every LinkedIn scraper you will
find online calls `/identity/profiles/{id}/profileView`, which returned the
whole profile in one nested tree. It now answers **HTTP 410 Gone**. The live
replacement is `/identity/dash/profiles`, with a `decorationId` naming how
much of the object graph to expand.

**2. The response is a graph, not a tree.** Dash returns *normalised* JSON:
every entity sits flat in an `included` array and references between them are
string URNs in `*`-prefixed keys. A profile does not contain its jobs - it has
`"*profilePositionGroups": "urn:li:collectionResponse:xyz"`, that collection
is itself in `included`, and *its* `*elements` are the position URNs. So the
parser indexes `included` by URN and walks the pointers (`dash.Resolver`).

**3. Partial cookies get your session killed.** This was the expensive one.
The intuitive approach - send `li_at` (the session) and `JSESSIONID` (the CSRF
token) - works for about three requests. Then every call returns
`403 CSRF check failed`, and shortly after LinkedIn responds with
`li_at=delete me` and invalidates the session entirely.

Two separate mechanisms cause that:

  * **LinkedIn rotates `JSESSIONID` mid-session** via `Set-Cookie`. A browser
    follows the rotation; a client that pins the value from config goes stale
    and starts failing the CSRF check.
  * **`li_at` is bound to a browser identity.** A browser sends ~10 cookies,
    including `bcookie`/`bscookie` (browser id) and `lidc` (routing). An
    `li_at` arriving without them looks like a *replayed stolen cookie*, so
    LinkedIn kills the session defensively.

**4. Correct cookies still are not enough.** With the full header and rotation
handling in place, sessions survived longer — and still died. The remaining
tell is **TLS fingerprinting**: Python's handshake (cipher order, extensions,
HTTP/2 settings — the JA3/JA4 signature) differs from Chrome's. A request whose
cookies say Chrome but whose handshake says Python reads as a replayed session.

So the client speaks TLS *as Chrome does*, via `curl_cffi`
(`impersonate="chrome"`). It is an optional dependency: without it everything
still runs, `/health` reports `"transport": "httpx"`, and you should expect
short-lived sessions.

Hence the design: take the **entire cookie header** copied from a browser,
**follow every `Set-Cookie` rotation**, derive `csrf-token` from the current
cookie state on each request, and **impersonate Chrome's TLS handshake**.

| Concern | Handling |
|---|---|
| **Authentication** | Full browser cookie header, sent verbatim. |
| **CSRF** | `csrf-token` header always echoes the *current* `JSESSIONID`. |
| **Rotation** | `Set-Cookie` updates are adopted; `delete me` is ignored. |
| **TLS** | `curl_cffi` replays Chrome's handshake so the connection matches the cookies. |
| **Protocol** | Rest.li 2.0 - `x-restli-protocol-version: 2.0.0` is required. |
| **Format** | `accept: application/vnd.linkedin.normalized+json+2.1` for `included`. |

### Which endpoints

| Endpoint | Gives us |
|---|---|
| `GET /identity/dash/profiles?q=memberIdentity&memberIdentity={id}&decorationId=...FullProfileWithEntities-63` | name, headline, location, about, images, industry, experience, education, skills, certifications, languages, projects, publications, honours, volunteering, courses, patents, test scores |
| `GET /identity/profiles/{id}/profileContactInfo` | email, phone, websites, Twitter — **currently returns HTTP 410**, see limitations |

One call does everything visible on the profile page. The contact card is the
only extra; LinkedIn has retired that endpoint too, so it is opt-in
(`include_contact=true`), off by default, and never fails the request.

### The fallback

If the session is missing, expired or throttled, the service does **not** just
error out. It fetches the logged-out public profile page and parses the
`schema.org` JSON-LD block LinkedIn embeds there. That yields name, headline,
about, location, picture, employers and schools — enough to stay useful — and
the response is tagged `"source": "public_page"` with a `warning` field so the
caller always knows which path produced the data.

This is why the demo works even before you configure a cookie.

### Design decisions worth calling out

- **Format knowledge is isolated.** `app/dash.py` is the only module that
  knows what LinkedIn's payloads look like. If LinkedIn changes shape again -
  and it will - exactly one file changes.
- **Dates are structured *and* pre-formatted.** Every date is
  `{year, month, day, text}`, and every date range also carries `date_range`
  (`"Mar 2020 - Present"`) and `duration_months`, so clients don't re-implement
  the same formatting.
- **Images expose every size.** LinkedIn returns a root URL plus one path
  segment per rendered resolution; we join them, expose all of them under
  `sizes`, and default `url` to the largest.
- **Caching is on by default** (1 hour). Repeat lookups never touch LinkedIn.
  Fewer requests to LinkedIn is both faster and much safer for the account.
- **Rate limiting is on by default** (20/min per IP), for the same reason.
- **No database.** A dict and a deque. It runs on a free instance with nothing
  to provision.

---

## Quick start (local)

Requires Python 3.11+.

```bash
git clone https://github.com/<you>/linkedin-profile-api.git
cd linkedin-profile-api

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # then fill in LINKEDIN_COOKIE (see below)

uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000> for the demo UI, or <http://127.0.0.1:8000/docs>
for interactive API docs.

Run the tests:

```bash
pip install pytest pytest-asyncio
pytest
```

> It starts and answers requests **without** any credentials — you just get the
> thinner public-page data until you add a cookie.

---

## Getting a LinkedIn session

Two ways. Both end up in the same place — a full cookie jar — and `/health`
tells you which mode you are in.

### Option A — sign in with credentials (best for a deployed instance)

```bash
LINKEDIN_EMAIL=you@example.com      # a phone number works too
LINKEDIN_PASSWORD=...
```

The app performs the login itself and keeps **every** cookie LinkedIn sets,
including the `bcookie`/`bscookie` browser-identity pair. Because the whole
flow happens in one session, the cookies, the IP and the TLS fingerprint all
agree with each other — which a header copied out of Chrome cannot claim.

The real advantage is recovery: when LinkedIn ends the session, the server
re-authenticates and retries by itself. With a pasted cookie you would be
editing environment variables by hand instead.

**The catch:** LinkedIn answers a scripted login with a checkpoint (2FA, an
emailed PIN, a captcha) whenever the account or the IP looks unfamiliar. New
accounts and cloud hosts both qualify. If that happens, use Option B.

### Option B — paste a cookie header

Mint one locally, where your IP is already familiar to LinkedIn, then paste the
result into your host's secrets:

```bash
python scripts/get_cookie.py you@example.com     # or a phone number
# -> LINKEDIN_COOKIE='lang=...; bcookie="..."; li_at=...; JSESSIONID="ajax:..."'
```

Or copy it straight out of the browser:

1. Log in to <https://www.linkedin.com> in Chrome.
2. `F12` → **Network** → reload the page.
3. Click any request to `www.linkedin.com` → **Headers** → **Request Headers**.
4. Right-click the `cookie:` line → **Copy value**.
5. Paste it into `.env` as `LINKEDIN_COOKIE`, wrapped in **single quotes** —
   the header contains `"` and `;`.

> Copy the whole header, not just `li_at`. A partial cookie set gets the
> session invalidated within a few requests — see [Approach](#approach).

### Checking it worked

```bash
curl localhost:8000/health
# {"linkedin_session":"configured","cookie_mode":"full","transport":"curl_cffi-chrome"}
```

| Field | Want | Meaning if not |
|---|---|---|
| `cookie_mode` | `full` | `partial` = no browser-identity cookies; sessions will die. |
| `transport` | `curl_cffi-chrome` | `httpx` = curl_cffi missing; TLS fingerprint gives you away. |

> **Never commit credentials.** `.env` is git-ignored; on a host, use its
> secrets settings.

> **Sessions end when you log out.** Signing out in the browser — or LinkedIn's
> "sign out of all sessions" — invalidates a copied cookie immediately. That is
> also how you revoke access after deploying.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `LINKEDIN_COOKIE` | — | **The whole cookie header** from your browser. The supported way to authenticate. |
| `LINKEDIN_LI_AT` / `LINKEDIN_JSESSIONID` | — | Legacy two-cookie fallback. Authenticates, but LinkedIn kills the session after a few requests — see the approach section. |
| `LINKEDIN_EMAIL` / `LINKEDIN_PASSWORD` | — | Optional auto-login when `LINKEDIN_LI_AT` is empty. |
| `API_KEY` | — | If set, callers must send `X-API-Key`. Empty = open API. |
| `CACHE_TTL_SECONDS` | `3600` | Response cache lifetime. `0` disables. |
| `RATE_LIMIT_PER_MINUTE` | `20` | Per-IP limit. `0` disables. |
| `REQUEST_TIMEOUT` | `25` | Seconds to wait on LinkedIn. |

---

## Deploying

All of these give you HTTPS automatically.

### Render (free tier — what this repo is set up for)

`render.yaml` is already in the repo.

1. Push the repo to GitHub.
2. On [render.com](https://render.com): **New → Blueprint**, pick the repo.
3. When prompted, paste `LINKEDIN_COOKIE` (and optionally `API_KEY`). They
   are marked `sync: false`, so they live in Render's dashboard, never in git.
4. Deploy → `https://<name>.onrender.com`.

> The free instance sleeps after ~15 minutes idle; the first request afterwards
> takes ~30s to wake it. `/health` is the health-check path.

### Railway / Koyeb / Fly.io

A `Dockerfile` is included, and `Procfile` covers buildpack-based hosts:

```bash
fly launch                                  # detects the Dockerfile
fly secrets set LINKEDIN_COOKIE='...'
fly deploy
```

### Vercel / Netlify

Not recommended. Both are serverless-first: the in-process cache and rate
limiter reset on every cold start, and the profile fetch can exceed the free
function timeout. Use a host that runs a real process.

---

## API documentation

Base URL: your deployment. All responses are JSON.

### `GET /api/v1/profile`

| Query param | Type | Default | Description |
|---|---|---|---|
| `url` | string | **required** | Profile URL, or just the slug (`williamhgates`). |
| `include_contact` | bool | `false` | Also try the legacy contact-info card. LinkedIn returns 410 for it today, so this normally just costs a wasted request. |
| `raw` | bool | `false` | Attach LinkedIn's untouched payload under `raw` (bypasses the cache). |
| `refresh` | bool | `false` | Ignore the cached copy and re-fetch. |

Accepted input formats — all resolve to the same profile:

```
https://www.linkedin.com/in/williamhgates/
https://in.linkedin.com/in/williamhgates
linkedin.com/in/williamhgates
https://www.linkedin.com/in/williamhgates/?originalSubdomain=us
williamhgates
```

```bash
curl "$BASE/api/v1/profile?url=https://www.linkedin.com/in/williamhgates/"
curl "$BASE/api/v1/profile?url=williamhgates&refresh=true"
curl -H "X-API-Key: $API_KEY" "$BASE/api/v1/profile?url=williamhgates"
```

### `POST /api/v1/profile`

Same thing with a JSON body — handy for long URLs.

```bash
curl -X POST "$BASE/api/v1/profile" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.linkedin.com/in/williamhgates/"}'
```

### `GET /health`

```json
{
  "status": "ok",
  "version": "1.0.0",
  "linkedin_session": "configured",
  "cookie_mode": "full",
  "transport": "curl_cffi-chrome",
  "cached_profiles": 3
}
```

### `GET /docs`

Swagger UI, generated from the code.

### Errors

Every failure returns the same shape:

```json
{ "error": "human readable explanation", "code": "machine_readable", "status": 404 }
```

| Status | `code` | Meaning |
|---|---|---|
| 400 | `invalid_url` | Not a `linkedin.com/in/...` URL (company pages included). |
| 401 | `unauthorized` | `API_KEY` is set and the `X-API-Key` header is wrong or missing. |
| 404 | `profile_not_found` | Deleted, renamed, or not visible to the signed-in account. |
| 429 | `rate_limited` | **Our** per-IP limit. |
| 429 | `linkedin_rate_limited` | **LinkedIn** is throttling the account — back off for a few minutes. |
| 502 | `linkedin_session_expired` | `li_at` is stale or was rejected. Refresh it. |
| 503 | `no_session` | No cookie configured on the server. |
| 504 | `upstream_timeout` | LinkedIn did not answer in time. |

---

## Response schema

Top level:

| Field | Type | Notes |
|---|---|---|
| `public_id` | string | The slug from the URL. |
| `profile_url` | string | Canonical profile URL. |
| `member_id` | string \| null | LinkedIn's internal member id. |
| `fetched_at` | string | ISO-8601 UTC. |
| `source` | `"voyager"` \| `"public_page"` | Which path produced this. |
| `cached` | bool | Served from the server-side cache. |
| `warning` | string | Present only when the fallback was used. |
| `basics` | object | Identity — see below. |
| `network` | object | `followers`, `connections`, `degree`. |
| `contact` | object | `emails`, `phone_numbers`, `websites`, `twitter`, `address`, `birthday`. |
| `experience` | array | Positions, most recent first. |
| `education` | array | Schools. |
| `skills` | string[] | Flat, de-duplicated. |
| `certifications` | array | `name`, `authority`, `license_number`, `url`, `issued_on`, `expires_on`. |
| `languages` | array | `name`, `proficiency` (human-readable). |
| `projects`, `publications`, `honors`, `volunteering`, `courses`, `organizations`, `patents`, `test_scores` | array | Same treatment as above. |
| `counts` | object | Quick totals for `experience`, `education`, `skills`. |

`basics`:

```jsonc
{
  "first_name": "Ada",
  "last_name": "Lovelace",
  "full_name": "Ada Lovelace",
  "headline": "Analytical Engine Programmer | Mathematician",
  "about": "I write algorithms for machines that do not exist yet.",
  "industry": "Computer Software",
  "location": {
    "text": "London, England, United Kingdom",
    "country": "United Kingdom",
    "country_code": "GB",
    "postal_code": "EC1A"
  },
  "is_student": false,
  "birth_date": null,
  "profile_picture": {
    "url": "https://media.licdn.com/.../shrink_400_400/photo.jpg",   // largest
    "sizes": [ { "width": 100, "height": 100, "url": "..." }, ... ]  // all of them
  },
  "background_image": null
}
```

An `experience` entry:

```jsonc
{
  "title": "Chief Algorithm Designer",
  "company": "Analytical Engine Co",
  "employment_type": "Full time",
  "location": "London, United Kingdom",
  "description": "Wrote the first published computer program.",
  "start_date": { "year": 1843, "month": 3, "day": null, "text": "Mar 1843" },
  "end_date": null,
  "is_current": true,
  "duration_months": 2202,
  "date_range": "Mar 1843 - Present",
  "company_id": "99",
  "company_linkedin_url": "https://www.linkedin.com/company/analytical-engine",
  "company_logo": null,
  "company_industries": ["Computer Hardware"]
}
```

**Conventions**

- Every field is always present. Missing data is `null` (or `[]`), never absent
  — clients don't need existence checks.
- Empty strings are normalised to `null`.
- Enums are humanised: `FULL_TIME` → `"Full time"`,
  `NATIVE_OR_BILINGUAL` → `"Native or bilingual proficiency"`.
- Dates are structured **and** pre-formatted.

---

## Project layout

```
app/
  main.py        FastAPI app: routes, auth, rate limiting, error handlers
  service.py     Orchestration: what to call, in what order, and the fallback
  linkedin.py    Voyager HTTP client (cookie state, CSRF, rotation, status codes)
  dash.py        LinkedIn's URN graph -> our schema  (the shape-aware module)
  normalize.py   Shared helpers: dates, images, URNs, enums, contact card
  fallback.py    Public-page JSON-LD parser
  urls.py        Profile URL -> public identifier
  cache.py       TTL cache + per-IP rate limiter
  auth.py        Optional email/password -> li_at
  config.py      Environment-variable settings
  errors.py      One error type, mapped to HTTP status + code
  static/        One-page demo UI
scripts/
  get_cookie.py  CLI helper: sign in and print a LINKEDIN_COOKIE line
tests/           70 tests: URL parsing, cookie/rotation handling, the
                 credential login, the dash graph walk, and the HTTP routes
examples/
  response.json  A complete sample response
```

---

## Known limitations

**These are real constraints, not TODOs.**

1. **This uses a private API, and it does rot.** Voyager is undocumented and
   unversioned. The endpoint every tutorial uses (`profileView`) was retired
   mid-development and now returns HTTP 410 — that is not hypothetical, it is
   what this project hit. When it happens again the symptoms are a 410, or a
   400/404 from `dash_profile`, and the fix is `app/dash.py` plus possibly the
   `DASH_DECORATION` version suffix in `app/linkedin.py`. The public-page
   fallback is the safety net meanwhile.

2. **Scraping LinkedIn is against LinkedIn's Terms of Service**, whatever the
   technical means. The account whose cookie you use is the one that carries
   the risk — restriction or a permanent ban. **Use a throwaway account, not
   your real one.** The caching and rate limiting defaults exist to keep the
   request volume low, and they should stay on.

3. **You only see what your account can see.** LinkedIn's data is
   visibility-scoped. A 2nd/3rd-degree connection returns less than a 1st, and
   contact info returns almost nothing outside 1st degree. Two deployments with
   different cookies will return different data for the same profile — that is
   LinkedIn's behaviour, not a bug.

4. **Sessions expire, and LinkedIn will end them proactively.** `li_at` lasts
   roughly a year, but it is invalidated early on password change, sign-out, or
   when traffic looks automated. Supplying only `li_at`/`JSESSIONID` reliably
   gets the session killed within a handful of requests — send the full cookie
   header. You get `502 linkedin_session_expired`; the fix is fresh cookies.
   `cookie_mode` on `/health` tells you whether you are set up correctly.

5. **Detection is layered, and this is an arms race.** Getting a session to
   survive needed three separate things: the full cookie header, following
   `Set-Cookie` rotations, and matching Chrome's TLS fingerprint. LinkedIn can
   add another layer whenever it likes. Treat a working deployment as
   something to re-verify, not something that stays fixed.

6. **Rate limits are real and aggressive.** A few hundred profile views a day
   from one account will trip throttling (`429`) or a `999` block. There is no
   published number, and it tightens for new accounts. Bulk work needs a pool
   of accounts and proxies, which is deliberately out of scope here.

7. **Contact info is not available.** The `profileContactInfo` endpoint was
   retired alongside `profileView` and now returns HTTP 410, so `contact` comes
   back with empty arrays. Everything rendered on the profile page itself is
   unaffected — it all comes from the dash call. The option is kept, off by
   default, in case LinkedIn restores the endpoint.

8. **The fallback is much thinner.** Logged-out pages carry no skills, no
   certifications, no dates on most entries, and LinkedIn masks parts of the
   text with asterisks. Check `"source"` before relying on a field.

9. **Sections are truncated as LinkedIn truncates them.** The dash call returns
   the first page of each section, so a profile with 40+ positions may be cut
   off. Paginating every section would mean several more requests per profile —
   a poor trade against limit #6.

10. **Single-instance state.** The cache and rate limiter live in process
   memory. Correct for one free instance; behind multiple replicas each has its
   own. Swapping both for Redis is a small change in `cache.py`.

11. **No JavaScript rendering.** No headless browser is used — that is the point
   of calling the API directly. It also means anything rendered purely
   client-side and never returned by an endpoint is not available.

12. **English only.** Requests pin `x-li-lang: en_US`, so enum labels and some
    LinkedIn-supplied strings come back in English regardless of the profile's
    locale.

---

## License

MIT — see [`LICENSE`](LICENSE).
