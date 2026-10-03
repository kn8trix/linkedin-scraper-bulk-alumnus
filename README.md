# LinkedIn Profile API

Give it a LinkedIn profile URL, get structured JSON back.

**Live:** https://linkedin-profile-api-4iev.onrender.com/

```bash
curl "https://linkedin-profile-api-4iev.onrender.com/api/profile?url=https://www.linkedin.com/in/harshilmalani"
```

No browser, no headless Chrome, no Selenium. The service makes authenticated
HTTP calls straight to LinkedIn's internal **Voyager** API and parses the
response graph itself. FastAPI and uvicorn are the only dependencies —
everything that talks to LinkedIn is Python standard library.

> Hosted on Render's free tier, which idles out after ~15 minutes. The first
> request after a pause may take 30–60s while the container wakes.

---

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # fill in, see below
.venv/bin/uvicorn app:app --reload --port 8000
```

### Credentials

No OAuth, no API key — authentication is the session cookie of a real
logged-in browser.

1. Log into LinkedIn. **Use a throwaway account.**
2. Load `linkedin.com/feed/` once — `JSESSIONID` is only set after a page load.
3. DevTools → Network → click any `linkedin.com` request → Request Headers.
4. From **that same request**, copy two values into `.env`:

```
LI_COOKIE=<the entire `cookie:` value, on ONE line>
LI_USER_AGENT=<the `user-agent:` value from the same request>
```

Both must come from the same request. That is not a style preference — see
[the User-Agent finding](#the-user-agent-binds-the-session). `LI_USER_AGENT`
has no default and the app refuses to start without it.

Optional knobs (`API_KEYS`, `CACHE_TTL_SECONDS`, `LI_MIN_INTERVAL`,
`LI_STATE_DIR`, the bulk `BULK_*` / `MAX_BULK_URLS` limits) are documented in
`.env.example`. Note that the dashboard supplies its own cookie and User-Agent
per request, so the bulk endpoint works even when `LI_COOKIE` / `LI_USER_AGENT`
are unset — those are only needed for `GET /api/profile`.

### CLI

```bash
python3 probe.py --check                     # session health, 1 request
python3 probe.py harshilmalani andrewyng     # fetch + save raw blobs to out/
python3 normalize.py out/andrewyng.raw.json  # raw blob -> clean JSON
```

---

## API

Interactive docs: [`/docs`](https://linkedin-profile-api-4iev.onrender.com/docs)

### `GET /api/profile`

| Param | Default | Notes |
|---|---|---|
| `url` | required | Profile URL or bare public identifier |
| `refresh` | `false` | Bypass the cache |
| `raw` | `false` | Return LinkedIn's untouched response |

`url` accepts anything a human would paste — `https://www.linkedin.com/in/x/?trk=…`,
`https://in.linkedin.com/in/x`, `linkedin.com/in/x`, or just `x`. Non-LinkedIn
hosts and company URLs are rejected with `400` before any upstream call.

Responses carry `X-Cache: HIT|MISS|BYPASS` and `X-Cache-TTL`.

### `POST /api/bulk-scrape`

Scrape many profiles in one call. Accepts `multipart/form-data` (the same shape
the dashboard submits) and returns JSON only — **nothing is written to disk**,
so the whole payload can be inserted straight into a database.

| Field | Type | Notes |
|---|---|---|
| `cookie` | string, required | The whole LinkedIn `cookie:` header |
| `user_agent` | string, required | Must match the browser that minted the cookie |
| `profile_urls` | string, optional | Newline- or comma-separated URLs / identifiers |
| `file` | file, optional | `.csv` or `.txt` containing URLs |
| `X-API-Key` | header, optional | Only enforced when `API_KEYS` is set |

`profile_urls` and `file` are merged, whitespace-trimmed and deduplicated
(case-insensitively, ignoring a trailing slash) before anything is fetched, and
the order of first appearance is preserved. CSV rows work whether the URL is
bare, quoted, or sitting in a named column.

Unlike `/api/profile`, credentials are supplied **per request** rather than read
from the environment, because a cookie and User-Agent are a matched pair tied to
the browser session that created them. Each URL therefore gets its own
short-lived session, and the shared cache / circuit breaker are not involved —
this endpoint is deliberately independent of the long-lived env session.

```bash
curl -X POST https://linkedin-profile-api-4iev.onrender.com/api/bulk-scrape \
  -F 'cookie=li_at=...; JSESSIONID="ajax:..."' \
  -F 'user_agent=Mozilla/5.0 (…) Chrome/151.0.0.0 Safari/537.36' \
  -F 'profile_urls=https://www.linkedin.com/in/x
https://www.linkedin.com/in/y' \
  -F 'file=@urls.csv'
```

Response:

```jsonc
{
  "success": true,
  "total": 5,
  "data": [
    {
      "profile_url": "https://www.linkedin.com/in/harshilmalani",
      "full_name": "Harshil Malani",
      "headline": "ex-sde intern @humantic ai | …",
      "location": "Surat, Gujarat, India",
      "profile_picture_base64": "data:image/jpeg;base64,/9j/4AAQ…",
      "details": { /* the full /api/profile payload */ },
      "success": true
    }
  ]
}
```

`profile_picture_base64` is the image **bytes**, inlined as a data URL, so it
survives the signed CDN link expiring. A picture that cannot be downloaded is
omitted rather than failing the profile.

**Failures are per item, not per request.** A bad URL or an unreachable profile
produces `success: false` with an `error` object (`reason`, `message`, and
usually `remedy`) while the rest of the batch completes:

```jsonc
{ "profile_url": "https://example.com/in/x", "success": false,
  "error": { "reason": "bad_profile_url",
             "message": "not a linkedin.com URL: example.com" } }
```

#### Rate limiting and back-off

Bulk scraping is the fastest way to get a session revoked, so URLs are spaced
rather than fired back to back:

- A **random delay** of `BULK_DELAY_MIN_SECONDS`–`BULK_DELAY_MAX_SECONDS`
  (default 1–3s) is inserted between profiles.
- A **`rate_limited` response doubles the delay** for the URLs that follow,
  capped at `BULK_MAX_BACKOFF_SECONDS` (default 30s). A single throttle is
  recovered from; the batch only gives up after
  `BULK_MAX_CONSECUTIVE_THROTTLES` (default 3) in a row.
- A **dead session aborts the batch.** On `session_revoked`, `session_expired`,
  `challenge_required` or `csrf_mismatch`, the remaining URLs are marked
  `reason: "aborted"` and no further requests are made — retrying a revoked
  cookie is how "session died" escalates into "account restricted".

The response always contains one entry per submitted URL, so `total` matches the
deduplicated input even when the batch stops early.

#### Bulk errors

| HTTP | `error` | Meaning |
|---|---|---|
| 400 | `missing_cookie` / `missing_user_agent` | A required credential is blank |
| 400 | `no_urls` | Neither `profile_urls` nor a file yielded a URL |
| 400 | `too_many_urls` | More than `MAX_BULK_URLS` (default 50) |
| 413 | `file_too_large` | Upload exceeds `MAX_UPLOAD_BYTES` (default 2 MB) |
| 422 | `validation_error` | A required form field was absent |

### Web UI

The service ships a single-page dashboard at [`/`](https://linkedin-profile-api-4iev.onrender.com/)
(`static/index.html`, Tailwind via CDN, no build step):

1. Paste the **cookie** and matching **User-Agent**.
2. Add profiles — one URL per line in the textarea, and/or upload a CSV/TXT.
3. Press **Start Bulk Scrape**.

Results appear in two tabs: **Formatted UI View** renders responsive profile
cards (avatar, name, headline, location, latest role, education, skills) with
failed URLs as distinct error cards; **Raw JSON View** shows the exact response
with *Copy to Clipboard* and *Download JSON File*.

#### Base64 avatars

Card avatars do not use LinkedIn's image URL. They render
`profile_picture_base64` — the image bytes inlined as a data URL
(`data:image/jpeg;base64,…`) — because the CDN links are **signed and expire**,
so a stored URL becomes a broken image within days. A data URL is
self-contained and survives both that expiry and the LinkedIn session dying,
which is also what makes it safe to insert straight into a database. If an
image cannot be downloaded, the avatar falls back to initials.

#### Stateless by design

The dashboard holds results in **in-memory variables only**:

- No `localStorage` / `sessionStorage` — refreshing or closing the tab discards
  every result *and* the pasted cookie.
- No server-side persistence — the API never writes scraped profiles to disk or
  a database, and uploaded CSV/TXT files are parsed in memory and dropped.
- The bulk endpoint is independent of the shared env session, cache and circuit
  breaker, so nothing about a bulk run outlives the request.

The only durable state in the whole service is the credential cookie jar under
`LI_STATE_DIR`, which the *single-profile* endpoint uses to ride an `li_at`
rotation. Bulk scraping never touches it.

### Other endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | Session state, cache stats. **503** when the session is dead — monitor this |
| `GET /health/live` | Liveness only, always 200. Platform health checks point here |
| `POST /admin/reload` | Re-read credentials and re-probe, without a redeploy |

### Errors

Every error is a JSON object with `error`, `message`, and usually `remedy`.

| HTTP | `error` | Meaning |
|---|---|---|
| 400 | `bad_profile_url` | Not a parseable LinkedIn profile URL |
| 401 | `unauthorized` | `API_KEYS` set, key missing or wrong |
| 404 | `profile_not_found` | No such member, **or** not visible to this account |
| 429 | `rate_limited` | LinkedIn throttled us (HTTP 429 or 999) |
| 502 | `unparsable_profile` | Response arrived but had no profile in it |
| 503 | `session_revoked` / `session_expired` / `challenge_required` / `csrf_mismatch` | Session-level failure; see `remedy` |

---

## Response

**Top-level keys are always present** — empty sections are `[]`, and a missing
value is `null` rather than an absent key. *Inside* section items, null fields
are omitted, so one education entry may carry `grade` while the next has no
such key. Read item fields with a default.

Real response for `/in/harshilmalani`, with long strings and image variants
trimmed:

```jsonc
{
  "public_identifier": "harshilmalani",
  "profile_url": "https://www.linkedin.com/in/harshilmalani",
  "urn": "urn:li:fsd_profile:ACoAAD82vx4Bd9JMLqMWsNEUgHQnyPpRheBcASc",
  "member_id": "1060552478",
  "first_name": "Harshil",
  "last_name": "Malani",
  "full_name": "Harshil Malani",
  "headline": "ex-sde intern @humantic ai | guardian(top 0.65%) @leetcode | expert @codeforces",
  "about": null,                       // top-level keys stay, even when empty
  "location": {
    "name": "Surat, Gujarat, India",
    "short_name": "Surat, Gujarat",
    "country_code": "IN",
    "geo_urn": "urn:li:fsd_geo:101866859"
  },
  "industry": "Computer Software",
  "profile_picture": {                 // 800/400/200/100, largest first
    "url": "https://media.licdn.com/dms/image/v2/D4D03AQFs0IBu0gANgw/...",
    "sizes": [{ "width": 800, "height": 800, "url": "..." }]
  },
  "background_image": { "url": "...", "sizes": [ ... ] },
  "flags": { "premium": false, "influencer": false,
             "creator": false, "memorialized": false },

  // Several roles at one employer nest under one company, the way LinkedIn
  // itself groups them.
  "experience": [{
    "company": "Humantic AI",
    "company_urn": "urn:li:fsd_company:20388343",
    "company_details": {
      "name": "Humantic AI",
      "url": "https://www.linkedin.com/company/humantic-ai/",
      "universal_name": "humantic-ai",
      "logo": { "url": "...", "sizes": [ ... ] }
    },
    "date_range": { "start": {"year": 2026, "month": 1},
                    "end": {"year": 2026, "month": 7},
                    "text": "Jan 2026 - Jul 2026" },
    "positions": [{
      "title": "Software Engineer",
      "employment_type": "Internship",
      "location": "Bengaluru",
      "description": "• Built a MongoDB-based caching system for the Miia AI agent...",
      "date_range": { "start": {...}, "end": {...}, "text": "Jan 2026 - Jul 2026" }
    }]
  }],

  "education": [{
    "school": "Indian Institute of Information Technology, Design and Manufacturing, Jabalpur",
    "school_urn": "urn:li:fsd_school:162000",
    "school_url": "https://www.linkedin.com/school/...",
    "school_logo": { "url": "...", "sizes": [ ... ] },
    "degree": "Bachelor of Technology - BTech",
    "field_of_study": "Computer Science",
    "date_range": { "start": {"year": 2022, "month": 11},
                    "end": {"year": 2026, "month": 5},
                    "text": "Nov 2022 - May 2026" }
  }],

  "skills": ["Django REST Framework", "Agentic AI Development",
             "RAG with MongoDB", "SQL", "Data Structures", "C++", "..."],

  // Certifications carry an ISSUE date, not a span.
  "certifications": [{
    "name": "Meta Hacker Cup",
    "authority": "Meta",
    "issuer": { "name": "Meta", "url": "...", "logo": { ... } },
    "date_range": { "issued": {...}, "text": "Issued Sep 2024" }
  }],

  "languages": [
    { "name": "English",  "proficiency": "FULL_PROFESSIONAL" },
    { "name": "Gujarati", "proficiency": "NATIVE_OR_BILINGUAL" },
    { "name": "Hindi",    "proficiency": "FULL_PROFESSIONAL" }
  ],
  "projects":  [],                     // empty, not missing
  "volunteer": [],

  "_meta": {
    "sections": {
      "experience": { "returned": 1,  "total": 1,  "complete": true },
      "skills":     { "returned": 11, "total": 11, "complete": true }
    },
    "incomplete_sections": [],
    "entities_seen": 54,
    "decoration_version": 96,
    "fetched_at": 1788026826
  }
}
```

### `_meta` is the honest part

LinkedIn reports how many items exist per section, and sometimes sends fewer.
Rather than returning 20 skills as though they were all 41, `_meta.sections`
reports both counts and flags the section incomplete. **Truncation is detected
from LinkedIn's own `paging.total`, not assumed.** The web UI shows it too — a
section header turns orange and reads `Skills 20 of 41`.

---

## How it works

### Why the Network tab looks empty

The obvious approach — open a profile, watch DevTools, copy the request — does
not work. The profile page is **server-rendered**, so the JSON arrives embedded
in the HTML inside `<code style="display:none" id="bpr-guid-…">` blocks and no
XHR fires for it. What *does* fire, on "Show all experiences", is
`…/voyager/api/graphql?variables=(…)&queryId=voyagerIdentityDashProfileCards.<md5>`
— an opaque hash with no operation name, which **rotates on every LinkedIn
deploy**. Hardcoding one is a time bomb.

So this uses an older REST surface that still works and needs no hash:

```
GET /voyager/api/identity/dash/profiles
    ?q=memberIdentity&memberIdentity=<vanity>
    &decorationId=com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-96
```

It takes the vanity name straight from the URL and returns the whole profile in
**one request**. `decorationId` is LinkedIn's schema filter; the `-96` suffix
drifts between deploys, so the client pins a known-good version, probes a range
only if that breaks, and caches whichever answered.

Auth is two cookies: `li_at` (the session) and `JSESSIONID`, which doubles as
the CSRF token and must be echoed in a `csrf-token` header. Omit it and every
call is a 403. Endpoints tutorials still recommend but which are now **dead**:
`/identity/profiles/{id}/profileView`, `/skills`, `/languages`.

### The User-Agent binds the session

Sessions kept dying within minutes. The usual suspects — expired cookies, bot
detection, fingerprinting — were all wrong.

| # | User-Agent sent | Outcome |
|---|---|---|
| 1 | `Chrome/139` (a hardcoded default) | worked, revoked ~15 min later |
| 2 | `Chrome/139` | worked, revoked again |
| 3 | `Chrome/151` (the real browser's) | previous session revoked within **60 seconds** |

The browser created the session as Chrome/151 while the script sent Chrome/139.
One `li_at` under two User-Agents reads as session hijacking, and LinkedIn
revokes the token server-side. Pinned to match, it has been stable since.

So `LI_USER_AGENT` has **no default and is a hard startup error**. A guessed
default is not a convenience — it silently destroys the credential it is given.
The most-cited public reference for this API asserts the opposite, that
"browser fingerprint plays NO role." That is wrong, at least for the UA.

**The IP is not bound, though.** The cookie was minted on an Indian residential
connection and the service runs from a Singapore datacenter — the session
survived unchanged. LinkedIn has to tolerate roaming, so only the UA is strict.

### It is a graph, not a document

With the `normalized+json` accept header the response is not a nested object:

```jsonc
{
  "data":     { "*elements": ["urn:li:fsd_profile:ACoAA…"] },
  "included": [ /* 54 entities, flat, unordered, cross-referenced by URN */ ]
}
```

Positions, schools, skills, companies and the profile itself are all siblings
in one pool. Fields prefixed `*` are pointers.

**The ordering trap:** iterating `included[]` returns entities in arbitrary
order, so experience comes out shuffled — which looks like data you must re-sort
by date. You must not. The correct order is in the response, one indirection
away:

```
data["*elements"][0]              -> the root Profile URN
Profile["*profilePositionGroups"] -> a CollectionResponse URN
CollectionResponse["*elements"]   -> the ordered list of PositionGroup URNs
```

So the parser never iterates the pool. It indexes by `entityUrn` and walks
pointers — two levels for experience (`PositionGroup` → `Position`), which
reproduces LinkedIn's own grouping of multiple roles at one employer.

Other things that are not where you would expect: **location** is not on the
Profile (`locationName` is null; the real value is `geoLocation.*geo` →
`Geo.defaultLocalizedName`), **images** need assembling from `rootUrl` plus each
artifact's path segment, and most text fields have `multiLocale` twins that
recover values left null on the primary field.

### Two responses that lie

**A revoked session does not look revoked.** It returns neither 401 nor a login
redirect:

```
HTTP 302
Location:   https://www.linkedin.com/voyager/api/me     <- the SAME url
Set-Cookie: li_at=delete me; Max-Age=0; Expires=Thu, 01-Jan-1970
```

A 302 pointing at the URL you just requested reads as a routing bounce. The
status code tells you nothing; `Set-Cookie: li_at=delete me` is the tell, and
that is what the client keys on.

**403 means two different things.** LinkedIn returns 403 both for a bad CSRF
token and for a profile that does not exist. Reading it as a dead session was
an outage — one typo'd URL tripped the circuit breaker and 503'd everything.
Since status alone cannot separate them, a 403 now re-probes `/me`: if the
session still answers, the session is fine and the profile is not. The extra
call is justified by the asymmetry — mistaking a typo for a dead session is far
more expensive than the reverse.

---

## Deploying to Render

The repo ships a [`render.yaml`](render.yaml) blueprint, so deployment is:

1. Push the repo to GitHub.
2. In the Render Dashboard: **New → Blueprint**, pick the repo, apply.
3. Fill in the two secrets it prompts for (`LI_COOKIE`, `LI_USER_AGENT`).
4. Open the service URL and scrape.

Prefer a manual deploy? **New → Web Service**, set **Runtime: Docker**, and let
it use the repo's `Dockerfile`. No build command or start command is needed —
the image's `CMD` already honours `$PORT`.

### Environment variables

| Key | Required | Notes |
|---|---|---|
| `LI_COOKIE` | for `/api/profile` | Secret. Whole `cookie:` header, one line |
| `LI_USER_AGENT` | for `/api/profile` | Secret. Must match the cookie's browser |
| `LI_STATE_DIR` | recommended | `/tmp/state` — writable, ephemeral |
| `CACHE_TTL_SECONDS` | optional | Default `21600` (6h) |
| `LI_MIN_INTERVAL` | optional | Upstream spacing, default `1.5` |
| `BULK_DELAY_MIN/MAX_SECONDS` | optional | Per-URL spacing, default `1`–`3` |
| `MAX_BULK_URLS` | optional | Default `50` |
| `KEEPALIVE_INTERVAL_SECONDS` | optional | Default `600` (10 min) |
| `API_KEYS` | recommended | Comma-separated. **Unset means the API is open** |

`RENDER_EXTERNAL_URL` is injected by Render automatically — do not set it by
hand, or you risk pinning a stale hostname.

**The bulk endpoint needs no environment credentials.** It takes `cookie` and
`user_agent` per request, so the dashboard works on a fresh deploy with no
LinkedIn secrets configured; only `GET /api/profile` reads the environment.

### Deployment notes that matter

- **`healthCheckPath: /health/live`, not `/health`.** `/health` returns 503
  whenever the LinkedIn session is dead, and a platform check pointed there
  would restart the container over an expired cookie. A restart cannot revive a
  dead credential — that is a crash loop, not a recovery.
- **One worker, one instance.** The cache, circuit breaker and single-flight
  locks are per-process, and there is exactly one upstream session. The
  blueprint sets `numInstances: 1` and the image runs `--workers 1`; changing
  either breaks the breaker and doubles the upstream call rate.
- **The filesystem is ephemeral.** The cookie jar and decoration pin are lost on
  every spin-down and redeploy, so the app re-reads credentials from the
  environment on boot. That is the correct fallback — do not expect the jar to
  survive.
- **Region.** The blueprint uses `singapore`, chosen to sit near the session's
  original geography (see the User-Agent finding below).

### Free tier: spin-down and keep-alive

Render **spins down a Free web service after 15 minutes without inbound
traffic**, and the next request takes **~1 minute** to cold-start. The blueprint
mitigates this with an in-process keep-alive: a `lifespan` task pings
`$RENDER_EXTERNAL_URL/health/live` every 10 minutes, starting 10 seconds after
boot. Every failure is logged and swallowed, so a blip cannot kill the loop.

**The ping must go to the public URL.** A request to `localhost` never crosses
Render's edge, is not counted as inbound traffic, and will *not* keep the
service awake — which is why the target is `RENDER_EXTERNAL_URL` rather than
`http://localhost:8000`. The endpoint is `/health/live` rather than `/health`
so that an expired LinkedIn cookie (503) does not make every ping look like a
failure.

> **An external monitor is still recommended.** The self-ping only works while
> the service is already awake, so it cannot recover from a spin-down, a
> crash, or a Render restart — and it does nothing if the app itself fails to
> boot. Point a free uptime monitor (e.g. **UptimeRobot**) at
> `https://<your-app>.onrender.com/health` every 5 minutes. That generates
> genuine inbound traffic from outside Render's network, so it keeps the
> service warm *and* alerts you when it goes down. Treat the self-ping as a
> best-effort backstop, not a replacement.

Two other free-tier limits are worth knowing: a workspace gets **750 instance
hours per month** (a spun-down service does not consume them), and Render may
suspend a service that makes an uncommonly high volume of *outbound* requests —
relevant here, since bulk scraping calls LinkedIn once per URL.

---

## Design notes

There is exactly **one** upstream LinkedIn session, shared by every caller, and
it is fragile. Most of the architecture follows from that.

- **Serialised, spaced calls** — upstream requests go through a lock with a
  minimum interval. Parallel bursts are what abuse detection looks for.
- **Single-flight** — concurrent requests for the same profile collapse into one
  upstream call plus cache reads.
- **6-hour cache** — every hit is a request not made; the biggest lever on both
  latency and session survival.
- **Circuit breaker** — the first session-level failure short-circuits later
  requests from memory. Retrying a revoked token is how "session died" becomes
  "account restricted".
- **Liveness split from readiness** — `/health` returns 503 on a dead session,
  which is right for monitoring and wrong for a platform check: the orchestrator
  would restart the container over an expired cookie, and a restart cannot
  revive a dead credential. That is a crash loop. Hence `/health/live`.
- **One worker, one instance** — cache, breaker and locks are per-process, so
  `--workers 1` is load-bearing, not a default.

```
voyager.py         Voyager client: session, cookie jar, errors, fetch. Stdlib only.
normalize.py       Raw entity graph -> structured JSON. Pure function.
app.py             FastAPI service: cache, breaker, single-flight, auth, keep-alive.
probe.py           CLI for session diagnostics and capturing fixtures.
static/index.html  Web UI: stateless bulk dashboard, formatted + raw JSON views.
```

---

## Limitations

- **Skills cap at 20** per this decoration; the gap is reported in `_meta`
  (e.g. `20/41`). Closing it needs the GraphQL `profileCards` endpoint and its
  rotating hash — deliberately not done, since that trades an honest, stable gap
  for a fragile dependency.
- **Featured/media is declared but withheld** — LinkedIn reports
  `total: 24` and sends zero elements.
- **Not extracted:** honors, publications, patents, courses, organizations,
  test scores. Empty on every profile tested, so nothing is verified.
- **Contact info** is a separate endpoint and is not fetched.
- **Image URLs are signed and expire.** Persist the bytes, not the URL.
- **Results are what the authenticated account can see.** Private profiles
  return 404, indistinct from "does not exist" — LinkedIn does not separate them.
- **One shared session** is the throughput ceiling and single point of failure
  for `/api/profile`. Bulk scraping sidesteps it with per-request credentials,
  at the cost of the cache and circuit breaker that protect that session.
- **Bulk requests are not authenticated separately**, so the endpoint is a
  credential-relay: whoever can reach it can run their own cookie through it.
  Set `API_KEYS` before exposing it to anyone else.
- **No per-IP rate limiting yet**, and `API_KEYS` is unset on the hosted
  instance so it can be evaluated freely. Bulk work is spaced and capped, but a
  caller can still queue many batches.
- **The keep-alive is best-effort.** It cannot wake a spun-down service, and it
  competes with the same 750 monthly instance hours an external monitor would
  consume — running both keeps the service awake essentially 24/7, which will
  exhaust the quota before month end.
- **No tests.** Five real fixtures sit in `out/` and the normalizer is a pure
  function — the suite is the most valuable next commit.

---

## Legal

This uses LinkedIn's private, undocumented API with a real member session,
which **violates the LinkedIn User Agreement**. Built as a reverse-engineering
exercise for a technical assignment.

Use a throwaway account and expect it to be restricted eventually. Do not
scrape at volume or redistribute personal data collected through it. Not
affiliated with or endorsed by LinkedIn; for anything commercial, use their
official partner APIs.
