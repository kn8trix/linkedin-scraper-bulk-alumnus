#!/usr/bin/env python3
"""
Step 1 probe: confirm the cookies work and find a live decorationId for the
profile read. Zero dependencies (stdlib urllib only).

Usage:
    cp .env.example .env      # fill in li_at + JSESSIONID
    python3 probe.py williamhgates
"""
import gzip
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://www.linkedin.com/voyager/api"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")

# decorationId version suffixes drift with LinkedIn deploys; probe a ladder.
DECOS = [f"com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-{v}"
         for v in range(100, 84, -1)]


def load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    li_at = os.environ.get("LI_AT")
    jsess = os.environ.get("LI_JSESSIONID")
    if not li_at or not jsess:
        sys.exit("Missing LI_AT / LI_JSESSIONID. See .env.example")
    return li_at, jsess.strip('"')


def headers(li_at, csrf):
    return {
        "csrf-token": csrf,
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
        "user-agent": UA,
        "cookie": f'li_at={li_at}; JSESSIONID="{csrf}"',
        "referer": "https://www.linkedin.com/feed/",
    }


def get(url, li_at, csrf):
    """Returns (status, body_text). Redirects are NOT followed -- a 302 means
    the session is dead, and we want to see that rather than get login HTML."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, headers=headers(li_at, csrf))
    try:
        r = opener.open(req, timeout=30)
        raw, status = r.read(), r.status
        if r.headers.get("content-encoding") == "gzip":
            raw = gzip.decompress(raw)
        return status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        if e.headers.get("content-encoding") == "gzip":
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        return e.code, raw.decode("utf-8", "replace")


def main():
    vanity = sys.argv[1] if len(sys.argv) > 1 else "williamhgates"
    li_at, csrf = load_env()
    os.makedirs(OUT, exist_ok=True)

    print("[0] auth probe -> /voyager/api/me")
    status, body = get(f"{BASE}/me", li_at, csrf)
    print(f"    HTTP {status}  ({len(body)} bytes)")
    if status == 302:
        sys.exit("    DEAD SESSION: redirected to login. Re-copy li_at from the browser.")
    if status == 403:
        sys.exit("    403: csrf-token header does not match the JSESSIONID cookie.")
    if status != 200:
        sys.exit(f"    unexpected: {body[:400]}")
    try:
        me = json.loads(body)
        mini = me.get("data", {}).get("miniProfile") or me.get("miniProfile") or {}
        print(f"    logged in as: {mini.get('publicIdentifier') or me.get('data', {}).get('publicIdentifier')}")
    except Exception:
        print("    (200 but unparsed body -- saved anyway)")
    open(os.path.join(OUT, "me.json"), "w").write(body)

    print(f"\n[1] profile read -> {vanity}")
    ident = urllib.parse.quote(vanity, safe="")
    for deco in DECOS:
        url = (f"{BASE}/identity/dash/profiles?q=memberIdentity"
               f"&memberIdentity={ident}&decorationId={deco}")
        status, body = get(url, li_at, csrf)
        ver = deco.rsplit("-", 1)[1]
        if status == 200:
            path = os.path.join(OUT, f"{vanity}.raw.json")
            open(path, "w").write(body)
            doc = json.loads(body)
            inc = doc.get("included", [])
            print(f"    FullProfileWithEntities-{ver}: HTTP 200  "
                  f"{len(body)} bytes, {len(inc)} included entities")
            print(f"    saved -> {path}")
            types = {}
            for e in inc:
                t = e.get("$type", "?").rsplit(".", 1)[-1]
                types[t] = types.get(t, 0) + 1
            print("\n    entity types present:")
            for t, n in sorted(types.items(), key=lambda x: -x[1]):
                print(f"      {n:4d}  {t}")
            return
        print(f"    FullProfileWithEntities-{ver}: HTTP {status}")
    sys.exit("    no working decorationId found -- widen the ladder or switch to GraphQL")


if __name__ == "__main__":
    main()
