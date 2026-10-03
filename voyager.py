#!/usr/bin/env python3
"""
Browserless LinkedIn Voyager client.

Shared by the CLI (probe.py) and the HTTP service (app.py) so there is exactly
one place that knows how to talk to LinkedIn.

Everything here is stdlib. The only endpoint we need is a REST one:

    GET /voyager/api/identity/dash/profiles
        ?q=memberIdentity&memberIdentity=<vanity>&decorationId=<schema>

which takes the vanity name straight out of a profile URL and needs no rotating
GraphQL queryId hash.
"""
import gzip
import hashlib
import http.cookies
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://www.linkedin.com/voyager/api"
STATE = os.environ.get("LI_STATE_DIR") or os.path.join(HERE, "out")
PIN_FILE = os.path.join(STATE, ".decoration")
JAR_FILE = os.path.join(STATE, ".cookies.json")

DECO = "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-{}"
DEFAULT_DECO_VERSION = 96
DECO_LADDER = list(range(100, 84, -1))

# LinkedIn is a shared, rate-limited resource and the session is fragile, so
# upstream calls are serialised and spaced out rather than issued concurrently.
MIN_INTERVAL_SECONDS = float(os.environ.get("LI_MIN_INTERVAL", "1.5"))


# --- error taxonomy ---------------------------------------------------------

class VoyagerError(Exception):
    reason = "voyager_error"
    remedy = ""
    http_status = 502
    #: session-level failures mean the credential itself is dead; retrying only
    #: makes LinkedIn more suspicious, so callers trip a breaker on these
    fatal_to_session = False


class SessionRevoked(VoyagerError):
    reason = "session_revoked"
    http_status = 503
    fatal_to_session = True
    remedy = ("LinkedIn destroyed the session server-side (it answered with "
              "`li_at=delete me`). Log in again in the browser, load /feed/ once, "
              "then re-copy the cookie AND the matching user-agent.")


class SessionExpired(VoyagerError):
    reason = "session_expired"
    http_status = 503
    fatal_to_session = True
    remedy = "Redirected to login. Re-copy the cookie from a logged-in browser."


class ChallengeRequired(VoyagerError):
    reason = "challenge_required"
    http_status = 503
    fatal_to_session = True
    remedy = ("LinkedIn wants a security checkpoint solved. Open linkedin.com in "
              "the browser, clear the challenge, then re-copy the cookie.")


class CsrfMismatch(VoyagerError):
    reason = "csrf_mismatch"
    http_status = 503
    fatal_to_session = True
    remedy = ("The csrf-token header does not match the JSESSIONID cookie. Copy "
              "both from the same browser session.")


class RateLimited(VoyagerError):
    reason = "rate_limited"
    http_status = 429
    remedy = "Back off and retry later."


class NotFound(VoyagerError):
    reason = "profile_not_found"
    http_status = 404
    remedy = "No profile for that identifier, or it is not visible to this account."


class BadProfileUrl(VoyagerError):
    reason = "bad_profile_url"
    http_status = 400
    remedy = "Expected a linkedin.com/in/<name> URL or a bare public identifier."


# --- profile URL parsing ----------------------------------------------------

_VANITY = re.compile(r"^[A-Za-z0-9\-_%À-ɏЀ-ӿ]{1,120}$")


def parse_profile_url(value):
    """'https://www.linkedin.com/in/raj1238/?trk=x' -> 'raj1238'.

    Accepts a full URL, a scheme-less URL, or a bare public identifier. Country
    subdomains (in./de./www.) and trailing locale segments are tolerated because
    real-world copied URLs carry them.
    """
    if not value or not value.strip():
        raise BadProfileUrl("empty profile URL")
    raw = value.strip()

    if "/" in raw or "." in raw:
        candidate = raw if "://" in raw else "https://" + raw
        parts = urllib.parse.urlsplit(candidate)
        host = (parts.netloc or "").lower().split(":")[0]
        if host and not (host == "linkedin.com" or host.endswith(".linkedin.com")):
            raise BadProfileUrl(f"not a linkedin.com URL: {host}")
        segments = [urllib.parse.unquote(s) for s in parts.path.split("/") if s]
        if "in" not in segments:
            raise BadProfileUrl("URL has no /in/<name> segment")
        idx = segments.index("in")
        if idx + 1 >= len(segments):
            raise BadProfileUrl("URL has /in/ but no identifier after it")
        vanity = segments[idx + 1]
    else:
        vanity = urllib.parse.unquote(raw)

    vanity = vanity.strip().rstrip("/")
    if not _VANITY.match(vanity):
        raise BadProfileUrl(f"implausible public identifier: {vanity!r}")
    return vanity


# --- credentials ------------------------------------------------------------

def load_env(path=None):
    path = path or os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def parse_cookie_header(raw):
    jar = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            jar[k.strip()] = v.strip().strip('"')
    return jar


class CredentialError(Exception):
    """Misconfiguration -- raised before any network call is attempted."""


# --- session ----------------------------------------------------------------

class Session:
    def __init__(self, persist=True, cookie_header=None, user_agent=None):
        load_env()
        self.persist = persist
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._ephemeral = cookie_header is not None or user_agent is not None

        # Callers may supply credentials explicitly (e.g. the bulk endpoint,
        # which takes a cookie + UA per request). Falling back to the
        # environment keeps the CLI and the single-profile service unchanged.
        raw_cookie = (cookie_header if cookie_header is not None
                      else os.environ.get("LI_COOKIE", "")).strip()
        if raw_cookie:
            self.jar = parse_cookie_header(raw_cookie)
        else:
            li_at = os.environ.get("LI_AT", "").strip()
            jsess = os.environ.get("LI_JSESSIONID", "").strip().strip('"')
            if not li_at or not jsess:
                raise CredentialError(
                    "Missing credentials. Set LI_COOKIE (preferred) or "
                    "LI_AT + LI_JSESSIONID -- see .env.example")
            self.jar = {"li_at": li_at, "JSESSIONID": jsess}

        if "JSESSIONID" not in self.jar:
            raise CredentialError(
                "No JSESSIONID in the cookie. Load linkedin.com/feed/ once in the "
                "browser -- it is only set after a page load -- then re-copy.")
        if "li_at" not in self.jar:
            raise CredentialError("No li_at in the cookie. Are you logged in?")

        # There is deliberately no default User-Agent. LinkedIn binds a session
        # to the UA that created it; reusing a valid li_at from a different UA
        # reads as session hijacking and gets the token revoked server-side,
        # usually within minutes. A fallback would destroy the cookie it is
        # handed, so an unset value is a hard error.
        self.ua = (user_agent if user_agent is not None
                   else os.environ.get("LI_USER_AGENT", "")).strip()
        if not self.ua:
            raise CredentialError(
                "LI_USER_AGENT is not set, and there is no safe default.\n\n"
                "LinkedIn ties a session to the User-Agent that created it. The "
                "same li_at sent under a different UA looks like a stolen session "
                "and gets revoked.\n\n"
                "Copy it from the SAME request you copied the cookie from:\n"
                "  DevTools -> Network -> any linkedin.com request -> Request "
                "Headers -> copy both `cookie:` and `user-agent:`")

        # Caller-supplied credentials are stateless: never read or write the
        # on-disk jar, which is keyed to the environment's own credential.
        self.env_fingerprint = hashlib.sha256(
            self.jar["li_at"].encode()).hexdigest()[:16]
        if self.persist and cookie_header is None:
            self._load_jar()
        self.csrf = self.jar["JSESSIONID"].strip('"')

    # -- jar persistence: ride li_at rotation instead of going stale ---------

    def _load_jar(self):
        if not os.path.exists(JAR_FILE):
            return
        try:
            with open(JAR_FILE) as f:
                saved = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
        if saved.get("env_fingerprint") != self.env_fingerprint:
            return  # a human pasted new credentials; those win
        self.jar.update(saved.get("jar") or {})

    def _save_jar(self):
        if not self.persist or self._ephemeral:
            return
        try:
            os.makedirs(STATE, exist_ok=True)
            tmp = JAR_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"env_fingerprint": self.env_fingerprint,
                           "saved_at": time.time(), "jar": self.jar}, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, JAR_FILE)
        except OSError:
            pass  # a read-only filesystem must not break the request

    def cookie_header(self):
        return "; ".join(f"{k}={v}" for k, v in self.jar.items())

    def headers(self):
        return {
            "csrf-token": self.csrf,
            "x-restli-protocol-version": "2.0.0",
            "accept": "application/vnd.linkedin.normalized+json+2.1",
            "accept-encoding": "gzip",
            "accept-language": "en-US,en;q=0.9",
            "x-li-lang": "en_US",
            "x-li-track": json.dumps({
                "clientVersion": "1.13.36530", "mpVersion": "1.13.36530",
                "osName": "web", "timezoneOffset": 5.5, "timezone": "Asia/Kolkata",
                "deviceFormFactor": "DESKTOP", "mpName": "voyager-web",
                "displayDensity": 2, "displayWidth": 1512, "displayHeight": 982,
            }, separators=(",", ":")),
            "user-agent": self.ua,
            "cookie": self.cookie_header(),
            "referer": "https://www.linkedin.com/feed/",
        }

    def absorb(self, headers):
        changed = False
        for value in headers.get_all("Set-Cookie") or []:
            try:
                c = http.cookies.SimpleCookie()
                c.load(value)
            except http.cookies.CookieError:
                continue
            for name, morsel in c.items():
                if morsel.value == "delete me" or morsel["max-age"] == "0":
                    continue
                if self.jar.get(name) != morsel.value:
                    self.jar[name] = morsel.value
                    changed = True
        if changed:
            # JSESSIONID and csrf-token are one paired credential; if the cookie
            # rotates and the header does not follow, every later call is a 403.
            self.csrf = self.jar["JSESSIONID"].strip('"')
            self._save_jar()

    def get(self, url):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw):
                return None

        with self._lock:
            gap = time.monotonic() - self._last_call
            if gap < MIN_INTERVAL_SECONDS:
                time.sleep(MIN_INTERVAL_SECONDS - gap)
            try:
                req = urllib.request.Request(url, headers=self.headers())
                r = urllib.request.build_opener(NoRedirect).open(req, timeout=30)
                raw = r.read()
                if r.headers.get("content-encoding") == "gzip":
                    raw = gzip.decompress(raw)
                self.absorb(r.headers)
                return json.loads(raw.decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                raise classify(e, url) from None
            except urllib.error.URLError as e:
                raise VoyagerError(f"network error reaching LinkedIn: {e.reason}") from None
            finally:
                self._last_call = time.monotonic()

    def get_bytes(self, url, headers=None, timeout=30):
        """Fetch raw bytes (e.g. a CDN image) with the session's UA.

        The Voyager JSON path is not used here: profile pictures live on
        media.licdn.com, which is not the API host and answers with an image
        body rather than JSON. Cookies are still sent, because the signed CDN
        URL is only valid for the session that received it.

        Returns (bytes, content_type).
        """
        req_headers = {
            "user-agent": self.ua,
            "accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "referer": "https://www.linkedin.com/feed/",
            "cookie": self.cookie_header(),
        }
        req_headers.update(headers or {})
        req = urllib.request.Request(url, headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
                return data, ctype
        except urllib.error.HTTPError as e:
            raise VoyagerError(
                f"HTTP {e.code} downloading image from {url}") from None
        except urllib.error.URLError as e:
            raise VoyagerError(
                f"network error downloading image: {e.reason}") from None

    def check(self):
        """Returns the authenticated member's public identifier."""
        me = self.get(f"{BASE}/me")
        # /me answers with the same URN indirection as the profile blob: the
        # identity is a pointer into included[], not an inline object.
        urn = (me.get("data") or {}).get("*miniProfile")
        mini = next((e for e in me.get("included", [])
                     if e.get("entityUrn") == urn), {})
        return mini.get("publicIdentifier")


def classify(err, url):
    """HTTPError -> typed VoyagerError.

    The decisive signal for a dead session is NOT the status code: a revoked
    session answers 302 with Location equal to the request URL, which looks like
    a routing bounce. The tell is `Set-Cookie: li_at=delete me`, LinkedIn's
    sentinel for destroying the token server-side.
    """
    code = err.code
    set_cookies = " ".join(err.headers.get_all("Set-Cookie") or [])
    location = err.headers.get("Location") or ""

    if "li_at=delete me" in set_cookies:
        return SessionRevoked("session revoked by LinkedIn")
    if code == 302:
        if "checkpoint" in location or "challenge" in location:
            return ChallengeRequired(f"checkpoint required: {location}")
        if "login" in location or "uas" in location:
            return SessionExpired(f"redirected to login: {location}")
        return SessionExpired(f"unauthenticated redirect to {location or '(none)'}")
    if code == 403:
        return CsrfMismatch("HTTP 403 -- csrf-token / JSESSIONID mismatch")
    if code in (429, 999):
        return RateLimited(f"HTTP {code} -- throttled or blocked by LinkedIn")
    if code == 404:
        return NotFound(f"HTTP 404 for {url}")
    return VoyagerError(f"HTTP {code} for {url}")


# --- profile fetch ----------------------------------------------------------

def pinned_version():
    if os.environ.get("LI_DECORATION_VERSION"):
        return int(os.environ["LI_DECORATION_VERSION"])
    if os.path.exists(PIN_FILE):
        try:
            return int(open(PIN_FILE).read().strip())
        except (ValueError, OSError):
            pass
    return DEFAULT_DECO_VERSION


def pin(version):
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(PIN_FILE, "w") as f:
            f.write(str(version))
    except OSError:
        pass


#: Set once a decoration version has actually returned a profile. After that a
#: 404 can only mean "no such member" -- so we must NOT sweep the ladder, which
#: would spend 16 upstream calls per typo'd URL.
_deco_confirmed = False


def fetch_profile(session, vanity):
    """Raw Voyager blob for a public identifier, plus the decoration used."""
    global _deco_confirmed
    ident = urllib.parse.quote(vanity, safe="")

    def attempt(version):
        doc = session.get(f"{BASE}/identity/dash/profiles?q=memberIdentity"
                          f"&memberIdentity={ident}&decorationId={DECO.format(version)}")
        # A wrong decoration errors; an unknown member comes back 200 with an
        # empty element list. Both are "not found" to a caller, but only the
        # first is worth trying another schema for.
        if not (doc.get("data") or {}).get("*elements"):
            raise NotFound(f"no profile for '{vanity}'")
        return doc

    first = pinned_version()
    try:
        try:
            doc = attempt(first)
            _deco_confirmed = True
            return doc, first
        except NotFound:
            if _deco_confirmed:
                raise

        for version in DECO_LADDER:
            if version == first:
                continue
            try:
                doc = attempt(version)
            except NotFound:
                continue
            pin(version)
            _deco_confirmed = True
            return doc, version
        raise NotFound(f"no profile for '{vanity}' (and no working decorationId)")

    except CsrfMismatch:
        # LinkedIn answers 403 for BOTH a real CSRF failure and a member who
        # does not exist or is not visible to this account. The status code
        # cannot separate them, and guessing wrong is expensive in one
        # direction: treating a typo'd URL as a dead session trips the breaker
        # and takes the whole service down. So ask the session directly --
        # if /me still answers, the session is fine and the profile is not.
        if session.check():
            raise NotFound(
                f"no profile for '{vanity}', or it is not visible to the "
                f"authenticated account") from None
        raise
