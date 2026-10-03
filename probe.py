#!/usr/bin/env python3
"""
CLI over the Voyager client -- session diagnostics and raw profile capture.

    python3 probe.py --check                 # session health only, 1 request
    python3 probe.py williamhgates           # fetch + save raw blob
    python3 probe.py a b c                   # several profiles, one session

Saved blobs feed normalize.py, so the parser can be developed against fixtures
without touching LinkedIn again.
"""
import argparse
import json
import os
import sys
import time

import voyager


def summarize(doc):
    types = {}
    for e in doc.get("included", []):
        t = e.get("$type", "?").rsplit(".", 1)[-1]
        types[t] = types.get(t, 0) + 1
    return len(doc.get("included", [])), types


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("profile", nargs="*",
                    help="profile URL or public identifier from /in/<name>")
    ap.add_argument("--check", action="store_true", help="session health only")
    ap.add_argument("--delay", type=float, default=2.0,
                    help="seconds between profiles (default 2)")
    args = ap.parse_args()

    try:
        session = voyager.Session()
    except voyager.CredentialError as e:
        sys.exit(str(e))

    os.makedirs(voyager.STATE, exist_ok=True)

    try:
        who = session.check()
    except voyager.VoyagerError as e:
        print(f"\n  SESSION DEAD -- {e.reason}\n  {e}\n\n  {e.remedy}\n",
              file=sys.stderr)
        return 2
    print(f"  session OK -- authenticated as {who or '(unknown)'}")

    if args.check or not args.profile:
        return 0

    failures = 0
    for i, value in enumerate(args.profile):
        if i:
            time.sleep(args.delay)
        try:
            vanity = voyager.parse_profile_url(value)
        except voyager.BadProfileUrl as e:
            print(f"\n  {value}\n    SKIPPED [{e.reason}] {e}", file=sys.stderr)
            failures += 1
            continue

        print(f"\n  {vanity}")
        try:
            doc, version = voyager.fetch_profile(session, vanity)
        except voyager.VoyagerError as e:
            failures += 1
            print(f"    FAILED [{e.reason}] {e}", file=sys.stderr)
            if e.fatal_to_session:
                print(f"    {e.remedy}\n    stopping.", file=sys.stderr)
                return 2
            continue

        path = os.path.join(voyager.STATE, f"{vanity}.raw.json")
        with open(path, "w") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
        count, types = summarize(doc)
        print(f"    deco -{version}, {count} entities -> {path}")
        for t, n in sorted(types.items(), key=lambda x: -x[1])[:8]:
            print(f"      {n:4d}  {t}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
