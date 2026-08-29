#!/usr/bin/env python3
"""
Browserless Voyager reader + session diagnostics. Zero dependencies (stdlib only).

Credentials come from .env, in preference order:

    LI_COOKIE       full cookie header pasted from DevTools  (preferred)
    LI_AT + LI_JSESSIONID                                    (fallback)

The full cookie string is preferred because it carries bcookie/bscookie/lidc/liap
alongside li_at, which is what a real browser sends. A two-cookie request is a
less usual shape, and unusual shapes are what get sessions revoked.

Usage:
    python3 probe.py --check                 # session health only, 1 request
    python3 probe.py williamhgates           # fetch + save raw blob
    python3 probe.py a b c                   # several profiles, one session
"""
import argparse
import gzip
import hashlib
import http.cookies
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://www.linkedin.com/voyager/api"
OUT = os.path.join(HERE, "out")
PIN_FILE = os.path.join(OUT, ".decoration")
JAR_FILE = os.path.join(OUT, ".cookies.json")

DECO = "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-{}"
DEFAULT_DECO_VERSION = 96
# Only consulted if the pinned version fails; the winner is then cached.
DECO_LADDER = list(range(100, 84, -1))

# There is deliberately NO default User-Agent. LinkedIn binds a session to the
# UA that created it: reusing a valid li_at from a different UA reads as session
# hijacking and gets the token revoked server-side, usually within minutes. A
# fallback default here would silently destroy the very session it was handed,
# so an unset LI_USER_AGENT is a hard error rather than a warning.


# --- error taxonomy ---------------------------------------------------------

class VoyagerError(Exception):
    """Base. `.reason` is a stable machine-readable code."""
    reason = "voyager_error"
    remedy = ""


class SessionRevoked(VoyagerError):
    reason = "session_revoked"
    remedy = ("LinkedIn destroyed the session server-side (it answered with "
              "`li_at=delete me`). Log in again in the browser, load /feed/ once, "
              "then re-copy the cookie. Do not press Log out afterwards -- that "
              "revokes the token immediately.")


class SessionExpired(VoyagerError):
    reason = "session_expired"
    remedy = "Redirected to login. Re-copy the cookie from a logged-in browser."


class ChallengeRequired(VoyagerError):
    reason = "challenge_required"
    remedy = ("LinkedIn wants a security checkpoint solved. Open linkedin.com in "
              "the browser, clear the challenge, then re-copy the cookie.")


class CsrfMismatch(VoyagerError):
    reason = "csrf_mismatch"
    remedy = ("The csrf-token header does not match the JSESSIONID cookie. They "
              "are a paired set -- copy both from the same browser session.")


class RateLimited(VoyagerError):
    reason = "rate_limited"
    remedy = "Back off. Reduce request volume and retry later."


class NotFound(VoyagerError):
    reason = "profile_not_found"
    remedy = "No profile for that vanity name, or it is not visible to this account."


# --- session ----------------------------------------------------------------

def load_env():
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def parse_cookie_header(raw):
    """'a=1; b=2' -> {'a': '1', 'b': '2'}. Tolerates quoted values."""
    jar = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            jar[k.strip()] = v.strip().strip('"')
    return jar


class Session:
    def __init__(self):
        load_env()
        cookie_header = os.environ.get("LI_COOKIE", "").strip()
        if cookie_header:
            self.jar = parse_cookie_header(cookie_header)
        else:
            li_at = os.environ.get("LI_AT", "").strip()
            jsess = os.environ.get("LI_JSESSIONID", "").strip().strip('"')
            if not li_at or not jsess:
                sys.exit("Missing credentials. Set LI_COOKIE (preferred) or "
                         "LI_AT + LI_JSESSIONID in .env -- see .env.example")
            self.jar = {"li_at": li_at, "JSESSIONID": jsess}

        if "JSESSIONID" not in self.jar:
            sys.exit("No JSESSIONID in the cookie. Load linkedin.com/feed/ once in "
                     "the browser -- it is only set after a page load -- then re-copy.")
        if "li_at" not in self.jar:
            sys.exit("No li_at in the cookie. Are you logged in?")

        # li_at rotates: LinkedIn periodically issues a replacement and kills the
        # old one. A browser rides that rotation because it keeps its jar; a
        # snapshot pasted into .env goes stale within minutes. So we persist the
        # jar and prefer the rotated value -- unless .env itself changed, which
        # means a human pasted something new and that must win.
        self.env_fingerprint = hashlib.sha256(
            self.jar["li_at"].encode()).hexdigest()[:16]
        self._load_jar()
        self.csrf = self.jar["JSESSIONID"].strip('"')
        self.ua = os.environ.get("LI_USER_AGENT", "").strip()
        if not self.ua:
            sys.exit(
                "LI_USER_AGENT is not set, and there is no safe default.\n\n"
                "LinkedIn ties a session to the User-Agent that created it. Sending\n"
                "the same li_at under a different UA looks like a stolen session and\n"
                "gets it revoked -- so guessing here would burn the cookie you just\n"
                "pasted.\n\n"
                "Copy it from the SAME request you copied the cookie from:\n"
                "  DevTools -> Network -> any linkedin.com request -> Request Headers\n"
                "  -> copy both `cookie:` and `user-agent:`\n\n"
                "Then in .env:\n"
                "  LI_USER_AGENT=Mozilla/5.0 (...) Chrome/... Safari/537.36")

    def _load_jar(self):
        if not os.path.exists(JAR_FILE):
            return
        try:
            with open(JAR_FILE) as f:
                saved = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
        if saved.get("env_fingerprint") != self.env_fingerprint:
            print("  ! credentials in .env changed -- discarding the stored jar")
            return
        stored = saved.get("jar") or {}
        if stored.get("li_at") and stored["li_at"] != self.jar["li_at"]:
            age = int(time.time() - saved.get("saved_at", 0))
            print(f"  using a rotated li_at picked up {age}s ago (from {JAR_FILE})")
        self.jar.update(stored)

    def _save_jar(self):
        os.makedirs(OUT, exist_ok=True)
        tmp = JAR_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"env_fingerprint": self.env_fingerprint,
                       "saved_at": time.time(), "jar": self.jar}, f, indent=2)
        os.chmod(tmp, 0o600)          # session cookies -- owner only
        os.replace(tmp, JAR_FILE)     # atomic, so a crash cannot truncate it

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
        """Merge Set-Cookie into the jar. LinkedIn rotates `lidc` constantly and
        expects the new value echoed back; carrying it forward keeps a multi-call
        run looking like one continuous browser session."""
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
        """Returns parsed JSON, or raises a typed VoyagerError."""
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw):
                return None

        req = urllib.request.Request(url, headers=self.headers())
        try:
            r = urllib.request.build_opener(NoRedirect).open(req, timeout=30)
            raw = r.read()
            if r.headers.get("content-encoding") == "gzip":
                raw = gzip.decompress(raw)
            self.absorb(r.headers)
            return json.loads(raw.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raise classify(e, url) from None

    def check(self):
        return self.get(f"{BASE}/me")


def classify(err, url):
    """HTTPError -> a typed VoyagerError.

    The decisive signal for a dead session is NOT the status code -- a revoked
    session answers 302 with `Location` equal to the request URL, which looks
    like a routing bounce. What gives it away is `Set-Cookie: li_at=delete me`,
    LinkedIn's sentinel for destroying the token server-side.
    """
    code = err.code
    set_cookies = " ".join(err.headers.get_all("Set-Cookie") or [])
    location = err.headers.get("Location") or ""

    if "li_at=delete me" in set_cookies:
        return SessionRevoked(f"session revoked by LinkedIn ({url})")
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
        except ValueError:
            pass
    return DEFAULT_DECO_VERSION


def pin(version):
    os.makedirs(OUT, exist_ok=True)
    with open(PIN_FILE, "w") as f:
        f.write(str(version))


def fetch_profile(session, vanity):
    """Try the pinned decoration first; ladder only if it fails.

    A blind ladder is 16 requests per profile against a session we are trying
    hard not to lose, so the winner is cached in out/.decoration.
    """
    ident = urllib.parse.quote(vanity, safe="")

    def attempt(version):
        url = (f"{BASE}/identity/dash/profiles?q=memberIdentity"
               f"&memberIdentity={ident}&decorationId={DECO.format(version)}")
        return session.get(url)

    first = pinned_version()
    try:
        return attempt(first), first
    except NotFound:
        pass  # wrong decoration version, or no such profile -- ladder decides

    for version in DECO_LADDER:
        if version == first:
            continue
        try:
            doc = attempt(version)
        except NotFound:
            continue
        print(f"    decoration -{first} failed, -{version} works (pinned)")
        pin(version)
        return doc, version
    raise NotFound(f"no working decorationId for '{vanity}' "
                   f"(profile may not exist, or the schema moved outside the ladder)")


def summarize(doc):
    inc = doc.get("included", [])
    types = {}
    for e in inc:
        t = e.get("$type", "?").rsplit(".", 1)[-1]
        types[t] = types.get(t, 0) + 1
    return len(inc), types


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vanity", nargs="*", help="public identifier(s) from /in/<name>")
    ap.add_argument("--check", action="store_true", help="session health only")
    ap.add_argument("--delay", type=float, default=2.0,
                    help="seconds between profiles (default 2)")
    args = ap.parse_args()

    session = Session()
    os.makedirs(OUT, exist_ok=True)

    try:
        me = session.check()
    except VoyagerError as e:
        print(f"\n  SESSION DEAD -- {e.reason}\n  {e}\n\n  {e.remedy}\n", file=sys.stderr)
        return 2

    # /me answers with the same URN indirection as the profile blob: the
    # identity is a *pointer* into included[], not an inline object.
    mini_urn = (me.get("data") or {}).get("*miniProfile")
    mini = next((e for e in me.get("included", [])
                 if e.get("entityUrn") == mini_urn), {})
    who = mini.get("publicIdentifier")
    print(f"  session OK -- authenticated as {who or '(unknown)'}")
    with open(os.path.join(OUT, "me.json"), "w") as f:
        json.dump(me, f, indent=2)

    if args.check or not args.vanity:
        return 0

    failures = 0
    for i, vanity in enumerate(args.vanity):
        if i:
            time.sleep(args.delay)
        print(f"\n  {vanity}")
        try:
            doc, version = fetch_profile(session, vanity)
        except VoyagerError as e:
            failures += 1
            print(f"    FAILED [{e.reason}] {e}", file=sys.stderr)
            if isinstance(e, (SessionRevoked, SessionExpired, ChallengeRequired,
                              RateLimited)):
                print(f"    {e.remedy}\n    stopping.", file=sys.stderr)
                return 2
            continue

        path = os.path.join(OUT, f"{vanity}.raw.json")
        with open(path, "w") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
        count, types = summarize(doc)
        print(f"    deco -{version}, {count} entities -> {path}")
        for t, n in sorted(types.items(), key=lambda x: -x[1])[:8]:
            print(f"      {n:4d}  {t}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
