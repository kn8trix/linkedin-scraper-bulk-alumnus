# LinkedIn Profile API

Give it a LinkedIn profile URL, get structured JSON back.

**Live:** https://linkedin-profile-api-4iev.onrender.com/

```bash
curl "https://linkedin-profile-api-4iev.onrender.com/api/profile?url=https://www.linkedin.com/in/williamhgates"
```

No browser, no headless Chrome, no Selenium. The service makes authenticated
HTTP calls straight to LinkedIn's internal **Voyager** API and parses the
response graph itself. The only dependencies are FastAPI and uvicorn;
everything that talks to LinkedIn is Python standard library.

> The hosted instance runs on Render's free tier, which idles a service out
> after ~15 minutes. The first request after a pause may take 30–60s while the
> container wakes. Subsequent requests are fast.

---

## Contents

- [Quick start](#quick-start)
- [API reference](#api-reference)
- [Response schema](#response-schema)
- [How it works](#how-it-works)
- [Design decisions](#design-decisions)
- [Deployment](#deployment)
- [Known limitations](#known-limitations)
- [Legal](#legal)

---

## Quick start

```bash
git clone https://github.com/HarshiLMalani07/linkedin-profile-api
cd linkedin-profile-api
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # then fill it in, see below
.venv/bin/uvicorn app:app --reload --port 8000
```

Open http://localhost:8000.

### Getting credentials

There is no OAuth and no API key. Authentication is the session cookie of a
real logged-in browser.

1. Log into LinkedIn. **Use a throwaway account** — see [Legal](#legal).
2. Load `linkedin.com/feed/` at least once. `JSESSIONID` is only set after a
   page load.
3. DevTools → Network → click any `linkedin.com` request → Request Headers.
4. From **that same request**, copy two values into `.env`:

```
LI_COOKIE=<the entire `cookie:` value, on ONE line>
LI_USER_AGENT=<the `user-agent:` value from the same request>
```

Both must come from the same request. This is not a style preference — see
[the User-Agent finding](#the-user-agent-binds-the-session) below.

<details>
<summary>All environment variables</summary>

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `LI_COOKIE` | yes* | — | Full cookie header, one line |
| `LI_USER_AGENT` | **yes** | none | Must match the browser that created the session |
| `LI_AT` / `LI_JSESSIONID` | no | — | Fallback if `LI_COOKIE` is unset |
| `API_KEYS` | no | unset (open) | Comma-separated; when set, `X-API-Key` is required |
| `CACHE_TTL_SECONDS` | no | `21600` | Response cache lifetime |
| `LI_MIN_INTERVAL` | no | `1.5` | Minimum seconds between upstream calls |
| `LI_STATE_DIR` | no | `./out` | Writable dir for the cookie jar and schema pin |
| `LI_DECORATION_VERSION` | no | auto | Pin the schema version instead of probing |

\* either `LI_COOKIE` or the `LI_AT`/`LI_JSESSIONID` pair.

There is deliberately **no default** for `LI_USER_AGENT`. The app refuses to
start without it.
</details>

### CLI

`probe.py` exercises the same client without the HTTP layer, and saves raw
blobs so the parser can be developed against fixtures:

```bash
python3 probe.py --check                        # session health, 1 request
python3 probe.py williamhgates andrewyng        # fetch + save to out/
python3 normalize.py out/andrewyng.raw.json     # raw blob -> clean JSON
```

---

## API reference

Interactive OpenAPI docs: [`/docs`](https://linkedin-profile-api-4iev.onrender.com/docs)

### `GET /api/profile`

| Param | Type | Default | Notes |
|---|---|---|---|
| `url` | string | required | Profile URL or bare public identifier |
| `refresh` | bool | `false` | Bypass the cache |
| `raw` | bool | `false` | Return LinkedIn's untouched response |

`url` accepts anything a human would paste:

```
https://www.linkedin.com/in/raj1238/?originalSubdomain=in
https://in.linkedin.com/in/raj1238
linkedin.com/in/raj1238
raj1238
```

Non-LinkedIn hosts, company URLs and implausible identifiers are rejected with
`400` before any upstream call is made.

**Response headers**

| Header | Meaning |
|---|---|
| `X-Cache` | `HIT` / `MISS` / `BYPASS` |
| `X-Cache-TTL` | Seconds until the cached entry expires |

### `GET /health`

Session state, cache stats, auth mode. Returns **503** when the LinkedIn
session is dead — this is the endpoint to monitor.

### `GET /health/live`

Liveness only. Always 200 while the process is up. Point platform health
checks *here*, not at `/health` — see [Design decisions](#design-decisions).

### `POST /admin/reload`

Re-reads credentials from the environment and re-probes the session. Recovering
from an expired cookie is "update the env var and reload", not "redeploy".
Requires `X-API-Key` when `API_KEYS` is set.

### Errors

Every error is a JSON object with `error`, `message`, and usually `remedy`.

| HTTP | `error` | Meaning |
|---|---|---|
| 400 | `bad_profile_url` | Not a parseable LinkedIn profile URL |
| 401 | `unauthorized` | `API_KEYS` set, key missing or wrong |
| 404 | `profile_not_found` | No such member, **or** not visible to this account |
| 429 | `rate_limited` | LinkedIn throttled us (HTTP 429 or 999) |
| 503 | `session_revoked` | LinkedIn destroyed the session server-side |
| 503 | `session_expired` | Redirected to login |
| 503 | `challenge_required` | A security checkpoint must be cleared |
| 503 | `csrf_mismatch` | `csrf-token` header does not match `JSESSIONID` |
| 502 | `unparsable_profile` | Response arrived but had no profile in it |

```json
{
  "error": "session_revoked",
  "message": "session revoked by LinkedIn",
  "remedy": "LinkedIn destroyed the session server-side (it answered with `li_at=delete me`). Log in again in the browser, load /feed/ once, then re-copy the cookie AND the matching user-agent."
}
```

---

## Response schema

**Top-level keys are always present**, and empty sections are `[]` rather than
missing — a consumer should never have to tell "absent" apart from "empty" for
a whole section.

*Inside* section items the rule is different: null fields are omitted to keep
payloads readable. One education entry may carry `grade` and `activities` while
the next has neither key. Treat item fields as optional and read them with a
default.

```jsonc
{
  "public_identifier": "raj1238",
  "profile_url": "https://www.linkedin.com/in/raj1238",
  "urn": "urn:li:fsd_profile:ACoAACD7H-8...",
  "member_id": "553328623",
  "first_name": "Raj",
  "last_name": "S.",
  "full_name": "Raj S.",
  "headline": "Engineering @ Anyscale | Ex-Concentric AI | Ex-Tekion",
  "about": "Experienced software developer with a strong foundation in...",
  "location": {
    "name": "Bangalore Urban, Karnataka, India",
    "short_name": "Bangalore Urban, Karnataka",
    "country_code": "IN",
    "geo_urn": "urn:li:fsd_geo:112376381"
  },
  "industry": "Computer & Network Security",
  "profile_picture": {
    "url": "https://media.licdn.com/dms/image/.../800_800/...",
    "sizes": [ { "width": 800, "height": 800, "url": "..." } ]
  },
  "background_image": { "url": "...", "sizes": [ ... ] },
  "flags": { "premium": false, "influencer": false,
             "creator": false, "memorialized": false },

  // Several roles at one employer are grouped under one company, the way
  // LinkedIn itself groups them.
  "experience": [{
    "company": "Concentric AI",
    "company_urn": "urn:li:fsd_company:18749690",
    "company_details": { "name": "...", "url": "...", "logo": { ... } },
    "date_range": { "start": {"year": 2022, "month": 10},
                    "end": {"year": 2025, "month": 12},
                    "text": "Oct 2022 - Dec 2025" },
    "positions": [{
      "title": "Staff Software Engineer",
      "employment_type": "Full-time",
      "location": "Bengaluru, Karnataka, India",
      "description": "Leading two teams focused on...",
      "date_range": { "start": {...}, "end": {...}, "text": "Mar 2025 - Dec 2025" }
    }]
  }],

  "education": [{
    "school": "Ahmedabad University", "school_urn": "urn:li:fsd_school:372040",
    "school_url": "...", "school_logo": { ... },
    "degree": "Bachelor of Technology - BTech",
    "field_of_study": "Computer Science Major",
    "grade": "3.30/4.00",              // omitted entirely when absent
    "activities": "Programming Club - Founder and Secretary...",
    "description": "...",
    "date_range": { "text": "2016 - 2020" }
  }],

  "skills": ["Jenkins", "GraphQL", "Kubernetes", "..."],

  // Certifications carry an ISSUE date, not a span.
  "certifications": [{
    "name": "Machine Learning", "authority": "Coursera",
    "issuer": { "name": "Coursera", "logo": { ... } },
    "license_number": "FMLWK293NRH3", "url": "https://...",
    "display_source": "coursera.org",
    "date_range": { "issued": {"year": 2021, "month": 2},
                    "text": "Issued Feb 2021" }   // `expires` only if set
  }],

  "languages": [{ "name": "English", "proficiency": "PROFESSIONAL_WORKING" }],
  "projects":  [{ "title": "...", "description": "...",
                  "date_range": { "text": "Aug 2018 - Dec 2018" } }],
  "volunteer": [{ "role": "...", "organization": "...", "cause": "EDUCATION",
                  "description": "...", "date_range": { ... } }],

  "_meta": {
    "sections": {
      "experience": { "returned": 8,  "total": 8,  "complete": true },
      "skills":     { "returned": 20, "total": 41, "complete": false }
    },
    "incomplete_sections": ["skills"],
    "entities_seen": 152,
    "decoration_version": 96,
    "fetched_at": 1788026826
  }
}
```

### `_meta` is the honest part

LinkedIn's response reports how many items exist in each section, and that
number is sometimes larger than what it actually sends. Rather than returning
20 skills as though they were all 41, `_meta.sections` reports both counts and
flags the section incomplete. **Truncation is detected from LinkedIn's own
`paging.total`, not assumed.** The web UI surfaces this too: a section header
turns orange and reads `Skills 20 of 41`.

---

## How it works

### Why the Network tab looks empty

The obvious approach — open a profile, watch DevTools, copy the request — does
not work, for two reasons:

1. **The profile page is server-rendered.** The JSON is already embedded in the
   HTML inside `<code style="display:none" id="bpr-guid-…">` blocks. No XHR
   fires for the main profile data.
2. **What does fire is GraphQL with an opaque hash.** Clicking "Show all
   experiences" hits
   `…/voyager/api/graphql?variables=(…)&queryId=voyagerIdentityDashProfileCards.<md5>`.
   There is no readable operation name or query body, and **the hash rotates on
   every LinkedIn deploy**. Hardcoding one is a time bomb.

### The endpoint this uses

There is an older REST surface that still works and needs no rotating hash:

```
GET /voyager/api/identity/dash/profiles
    ?q=memberIdentity
    &memberIdentity=<vanity>
    &decorationId=com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-96
```

It takes the vanity name straight out of the URL and returns the whole profile
— top card, experience, education, skills, certifications, languages, projects,
volunteering, images — in **one request**. `decorationId` is LinkedIn's schema
filter; the `-96` suffix is a schema version that drifts between deploys, so
the client pins a known-good version, falls back to probing a range only if it
breaks, and caches whichever version answered.

Endpoints that tutorials still recommend but which are now **dead**:
`/identity/profiles/{id}/profileView`, `/skills`, `/languages` — they return
410/400 or data that lags minutes behind reality.

### Authentication

Two cookies from a logged-in browser:

| Cookie | Role |
|---|---|
| `li_at` | The session |
| `JSESSIONID` | Doubles as the CSRF token — echoed in a `csrf-token` header, quotes stripped |

Plus `x-restli-protocol-version: 2.0.0` and
`accept: application/vnd.linkedin.normalized+json+2.1`. Omit the CSRF header
and every call is a 403.

### The User-Agent binds the session

**This was the single most expensive thing to learn, and no public source
documents it.**

Sessions kept dying within minutes. The obvious suspects — expired cookies,
bot detection, fingerprinting — were all wrong. The evidence:

| # | User-Agent sent | Outcome |
|---|---|---|
| 1 | `Chrome/139` (a hardcoded default) | worked, then revoked ~15 min later |
| 2 | `Chrome/139` | worked, revoked again |
| 3 | `Chrome/151` (the real browser's) | previous session revoked within **60 seconds** |

The browser had created the session as Chrome/151 while the script used
Chrome/139. Reusing one `li_at` under two different User-Agents reads as
session hijacking, and LinkedIn revokes the token server-side. Once the UA was
pinned to match, the session became stable.

So `LI_USER_AGENT` has **no default and is a hard error when unset**. A guessed
default is not a convenience — it silently destroys the credential it is given.

The most-cited public reference for this API asserts the opposite: that 302
loops are caused by expired cookies and that "browser fingerprint plays NO
role." That is wrong, at least for the User-Agent.

### A revoked session does not look like one

The natural assumption is that a dead session returns 401, or redirects to a
login page. It does neither:

```
HTTP 302
Location:   https://www.linkedin.com/voyager/api/me     <- the SAME url
Set-Cookie: li_at=delete me; Max-Age=0; Expires=Thu, 01-Jan-1970
```

A 302 pointing at the URL you just requested looks like a harmless routing
bounce. The status code cannot tell you what happened. The tell is
`Set-Cookie: li_at=delete me` — LinkedIn's sentinel for destroying the token —
and that is what this client keys on.

### IP is not bound, though

Worth recording because it was an open question: the session cookie was minted
on an Indian residential connection, and the service runs from a Singapore
datacenter. **The session survived unchanged.** LinkedIn binds the session
strictly to the User-Agent, but tolerates the IP moving continents — which
makes sense, since it also has to tolerate roaming and mobile networks.

### Parsing: it is a graph, not a document

With the `normalized+json` accept header, the response is *not* a nested
profile object. It is:

```jsonc
{
  "data":     { "*elements": ["urn:li:fsd_profile:ACoAA…"] },
  "included": [ /* 152 entities, flat, unordered, cross-referenced by URN */ ]
}
```

Positions, schools, skills, companies and the profile itself are all siblings
in one flat pool. Fields prefixed with `*` are pointers.

**The ordering trap:** iterating `included[]` gives entities in arbitrary
order. Experience comes out shuffled, which looks like data you have to
re-sort by date. You do not — the correct order is in the response, one
indirection away:

```
data["*elements"][0]            -> the root Profile URN
Profile["*profilePositionGroups"] -> a CollectionResponse URN
CollectionResponse["*elements"]   -> the ordered list of PositionGroup URNs
```

So the parser never iterates `included[]`. It indexes it by `entityUrn` and
walks pointers. Experience needs two levels: `PositionGroup` (one per employer)
→ `*profilePositionInPositionGroup` → `Position` (each role). That reproduces
LinkedIn's own grouping, where several roles at one company nest under a single
entry.

Other things that are not where you would expect them:

- **Location** is not on the Profile. `locationName` is `null`; the real value
  is `geoLocation.*geo` → `Geo.defaultLocalizedName`. Most `Geo` entities in
  `included[]` are bare stubs — only referenced ones get decorated.
- **Images** need assembling: `vectorImage.rootUrl` + each artifact's
  `fileIdentifyingUrlPathSegment`.
- **Text fields have `multiLocale` twins.** The parser prefers the plain field
  and falls back to the locale map, which recovers values LinkedIn sometimes
  leaves `null` on the primary field.
- **`schoolName` can be `null`** while the `*school` pointer resolves fine.

### 403 means two different things

LinkedIn returns **HTTP 403 for a profile that does not exist**, which is the
same status as a genuine CSRF failure. Reading it as a dead session was an
outage: one typo'd URL tripped the circuit breaker and 503'd every subsequent
request.

Status code alone cannot separate them, so on a 403 the client re-probes
`/me`. If the session still answers, the session is fine and the profile is
not — return 404. The extra call is justified by the asymmetry: mistaking a
typo for a dead session is far more expensive than the reverse.

---

## Design decisions

There is exactly **one** upstream LinkedIn session, it is shared by every
caller, and it is fragile. Most of the architecture follows from that.

**Serialised, spaced upstream calls.** Requests to LinkedIn go through a lock
with a minimum interval between them. Bursts of parallel traffic are what
rate-limiting and abuse detection look for.

**Single-flight.** Ten simultaneous requests for the same profile collapse into
one upstream call via a per-identifier lock, and nine cache reads.

**A 6-hour cache.** Every hit is a request not made — the single biggest lever
on both latency and session survival. Profile data barely changes.

**Circuit breaker.** The first session-level failure marks the session dead;
every later request is answered from memory with 503 and a remedy, without
touching LinkedIn. Retrying against a revoked token is how "the session died"
escalates into "the account got restricted."

**Hot reload over redeploy.** `POST /admin/reload` re-reads credentials and
re-probes. Recovery is a config change, not a rebuild.

**Liveness split from readiness.** `/health` returns 503 when the session is
dead, which is right for monitoring and *wrong* for a platform health check —
the orchestrator would restart the container over an expired cookie, and a
restart cannot revive a credential that is dead in the environment. That is a
crash loop, not a recovery. `/health/live` exists for orchestrators.

**One worker, one instance.** The cache, breaker and flight locks are all
per-process. A second worker would double the upstream call rate and split the
breaker's state. `--workers 1` and `numInstances: 1` are load-bearing.

**Cookie jar persistence.** Rotated cookies are written back to disk (atomic,
`0600`) keyed to a fingerprint of the configured credential, so a freshly
pasted cookie always wins over a stale stored one.

---

## Deployment

Hosted on Render's free tier from `render.yaml` (Docker, `region: singapore`,
health check on `/health/live`, one instance).

```bash
# render.com -> New -> Blueprint -> select this repo
# It reads render.yaml and prompts for the two secrets:
#   LI_COOKIE, LI_USER_AGENT
```

Credentials are `sync: false` in the blueprint, so Render prompts for them and
stores them encrypted. `.dockerignore` excludes `.env`, so no image can be
built with credentials baked in.

The container also runs anywhere Docker does:

```bash
docker build -t linkedin-profile-api .
docker run -p 8080:8080 --env-file .env linkedin-profile-api
```

---

## Known limitations

**Data completeness**

- **Skills are capped at 20** by this decoration; profiles with more report the
  gap in `_meta` (e.g. `20/41`). Closing it needs the GraphQL `profileCards`
  endpoint and its rotating `queryId` hash — deliberately not done, because it
  trades an honest, stable gap for a fragile dependency.
- **Featured / media is declared but withheld.** LinkedIn reports
  `*profileTreasuryMediaProfile` with `total: 24` and sends zero elements.
- **Not extracted:** honors, publications, patents, courses, organizations,
  test scores. The decoration returns these collections and they were empty on
  every profile tested, so nothing is verified against real data.
- **Contact info** (email, websites, phone) is a separate endpoint and is not
  fetched.
- **CDN image URLs are signed and expire** (typically months). Persist the
  bytes, not the URL.

**Visibility**

- Results are what *the authenticated account* can see. A different account may
  legitimately get different data for the same profile.
- Private profiles and members who block the account return 404, indistinct
  from "does not exist" — LinkedIn does not distinguish them either.

**Operational**

- **One shared session** is the throughput ceiling and the single point of
  failure. Heavy use risks the account being restricted.
- **No per-IP rate limiting yet.** `API_KEYS` is unset on the hosted instance
  so it can be evaluated freely; a real deployment should set it or add a
  limiter.
- **The cache is per-process** and unbounded. Fine for one instance, wrong the
  moment you scale out — that would want Redis.
- **Free-tier cold starts** of 30–60s after ~15 minutes idle.
- **No tests.** Five real fixtures sit in `out/`, and the normalizer is a pure
  function — the suite is straightforward to write and is the most valuable
  next commit.
- **Schema drift is unhandled at the sub-field level.** If LinkedIn renames a
  field, that field goes null silently; only a wholesale decoration change is
  detected.

---

## Project structure

```
voyager.py         Voyager client: session, cookie jar, error taxonomy,
                   URL parsing, profile fetch. Standard library only.
normalize.py       Raw entity graph -> structured JSON. Pure function.
app.py             FastAPI service: cache, breaker, single-flight, auth.
probe.py           CLI for session diagnostics and capturing fixtures.
static/index.html  Web UI: rendered profile, JSON view, history, themes.
render.yaml        Render blueprint.
Dockerfile         Container image.
```

---

## Legal

This uses LinkedIn's private, undocumented API with a real member session,
which **violates the LinkedIn User Agreement**. It was built as a
reverse-engineering exercise for a technical assignment.

- Use a throwaway account. Expect it to be restricted eventually.
- Do not scrape at volume, and do not redistribute personal data collected
  through it.
- Personal data belongs to the people it describes. GDPR/DPDP obligations
  apply to anyone storing it.
- Not affiliated with, endorsed by, or supported by LinkedIn. For anything
  commercial, use LinkedIn's official partner APIs.
