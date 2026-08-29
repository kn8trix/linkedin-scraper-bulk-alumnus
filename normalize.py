#!/usr/bin/env python3
"""
Voyager `identity/dash/profiles` blob -> clean structured JSON.

Pure function, no network. The raw response is a flat pool of entities in
`included[]` plus an envelope in `data`; nothing in `included[]` is ordered.
The authoritative order lives in CollectionResponse entities:

    data["*elements"][0]              -> root Profile urn
    Profile["*profileEducations"]     -> CollectionResponse urn
    CollectionResponse["*elements"]   -> ordered list of Education urns

So we never iterate `included[]` -- we index it by entityUrn and walk pointers.

Usage:
    python3 normalize.py out/raj1238.raw.json [-o out/raj1238.json]
"""
import argparse
import json
import sys

MONTHS = [None, "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class Index:
    """URN -> entity, plus pointer-following helpers."""

    def __init__(self, doc):
        self.doc = doc
        self.by_urn = {e["entityUrn"]: e
                       for e in doc.get("included", []) if "entityUrn" in e}

    def get(self, urn):
        return self.by_urn.get(urn) if isinstance(urn, str) else None

    def deref(self, entity, key):
        """Follow a single `*pointer` field to its entity."""
        return self.get((entity or {}).get(key))

    def collection(self, entity, key):
        """Follow a `*pointer` -> CollectionResponse -> ordered entities.

        Returns (items, paging). `paging.total` may exceed len(items): the
        decoration caps some sections (skills at 20), and that gap is what the
        GraphQL profileCards fallback would close.
        """
        coll = self.deref(entity, key)
        if not coll:
            return [], {}
        urns = coll.get("*elements") or []
        paging = coll.get("paging") or {}
        return [self.get(u) for u in urns if self.get(u)], paging


# --- small field helpers ----------------------------------------------------

def text(entity, field):
    """Plain field, falling back to its multiLocale twin."""
    if not entity:
        return None
    val = entity.get(field)
    if val:
        return val
    ml = entity.get("multiLocale" + field[0].upper() + field[1:]) or {}
    if not ml:
        return None
    return ml.get("en_US") or next(iter(ml.values()), None)


def date(d):
    if not d:
        return None
    return {"year": d.get("year"), "month": d.get("month"), "day": d.get("day")}


def date_text(d):
    if not d:
        return None
    y, m = d.get("year"), d.get("month")
    if y and m:
        return f"{MONTHS[m]} {y}"
    return str(y) if y else None


def issued_range(entity):
    """Certifications carry an issue date and (rarely) an expiry -- not a span.
    Rendering the generic "Feb 2021 - Present" would claim an end date that the
    payload does not have; LinkedIn itself says "Issued Feb 2021"."""
    dr = (entity or {}).get("dateRange")
    if not dr:
        return None
    start, end = dr.get("start"), dr.get("end")
    issued, expires = date_text(start), date_text(end)
    parts = []
    if issued:
        parts.append(f"Issued {issued}")
    if expires:
        parts.append(f"Expires {expires}")
    return {"issued": date(start), "expires": date(end),
            "text": " \u00b7 ".join(parts) or None}


def date_range(entity):
    dr = (entity or {}).get("dateRange")
    if not dr:
        return None
    start, end = dr.get("start"), dr.get("end")
    left, right = date_text(start), date_text(end)
    if left or right:
        label = f"{left or '?'} - {right or 'Present'}"
    else:
        label = None
    return {"start": date(start), "end": date(end), "text": label}


def images(container):
    """VectorImage -> list of {width, height, url}, largest first.

    rootUrl and each artifact's path segment concatenate into the real CDN URL;
    the signed segments carry an expiry (`e=`), so these links are not durable.
    """
    if not container:
        return []
    vec = container.get("vectorImage")
    if vec is None:
        vec = (container.get("displayImageReference") or {}).get("vectorImage")
    if not vec:
        return []
    root = vec.get("rootUrl") or ""
    out = [{"width": a.get("width"), "height": a.get("height"),
            "url": root + (a.get("fileIdentifyingUrlPathSegment") or "")}
           for a in vec.get("artifacts") or []]
    return sorted(out, key=lambda a: a.get("width") or 0, reverse=True)


def image_set(container):
    sizes = images(container)
    if not sizes:
        return None
    return {"url": sizes[0]["url"], "sizes": sizes}


def company_ref(idx, entity, key="*company"):
    c = idx.deref(entity, key)
    if not c:
        return None
    return {"name": c.get("name"), "urn": c.get("entityUrn"),
            "url": c.get("url"), "logo": image_set(c.get("logo")),
            "universal_name": c.get("universalName")}


def prune(obj):
    """Drop null / empty values so the payload stays readable."""
    if isinstance(obj, dict):
        out = {k: prune(v) for k, v in obj.items()}
        return {k: v for k, v in out.items() if v not in (None, [], {}, "")}
    if isinstance(obj, list):
        return [prune(v) for v in obj]
    return obj


# --- section builders -------------------------------------------------------

def build_experience(idx, profile, meta):
    """PositionGroup (one per company) -> nested Positions (roles held there).

    The grouping is LinkedIn's own: several roles at one employer collapse into
    a single group with the company's overall span.
    """
    groups, paging = idx.collection(profile, "*profilePositionGroups")
    meta["experience"] = section_meta(groups, paging)
    out = []
    for g in groups:
        positions, _ = idx.collection(g, "*profilePositionInPositionGroup")
        out.append({
            "company": text(g, "companyName"),
            "company_urn": g.get("companyUrn"),
            "company_details": company_ref(idx, g),
            "date_range": date_range(g),
            "positions": [{
                "title": text(p, "title"),
                "employment_type": (idx.deref(p, "*employmentType") or {}).get("name"),
                "location": text(p, "locationName") or text(p, "geoLocationName"),
                "description": text(p, "description"),
                "date_range": date_range(p),
            } for p in positions],
        })
    return out


def build_education(idx, profile, meta):
    items, paging = idx.collection(profile, "*profileEducations")
    meta["education"] = section_meta(items, paging)
    out = []
    for e in items:
        school = idx.deref(e, "*school") or idx.deref(e, "*company")
        out.append({
            "school": text(e, "schoolName") or (school or {}).get("name"),
            "school_urn": e.get("schoolUrn"),
            "school_url": (school or {}).get("url"),
            "school_logo": image_set((school or {}).get("logo")),
            "degree": text(e, "degreeName"),
            "field_of_study": text(e, "fieldOfStudy"),
            "grade": text(e, "grade"),
            "activities": text(e, "activities"),
            "description": text(e, "description"),
            "date_range": date_range(e),
        })
    return out


def build_skills(idx, profile, meta):
    items, paging = idx.collection(profile, "*profileSkills")
    meta["skills"] = section_meta(items, paging)
    return [text(s, "name") for s in items]


def build_certifications(idx, profile, meta):
    items, paging = idx.collection(profile, "*profileCertifications")
    meta["certifications"] = section_meta(items, paging)
    return [{
        "name": text(c, "name"),
        "authority": text(c, "authority"),
        "issuer": company_ref(idx, c),
        "license_number": text(c, "licenseNumber"),
        "url": c.get("url"),
        "display_source": c.get("displaySource"),
        "date_range": issued_range(c),
    } for c in items]


def build_languages(idx, profile, meta):
    items, paging = idx.collection(profile, "*profileLanguages")
    meta["languages"] = section_meta(items, paging)
    return [{"name": text(l, "name"), "proficiency": l.get("proficiency")}
            for l in items]


def build_projects(idx, profile, meta):
    items, paging = idx.collection(profile, "*profileProjects")
    meta["projects"] = section_meta(items, paging)
    return [{
        "title": text(p, "title"),
        "description": text(p, "description"),
        "url": p.get("url"),
        "date_range": date_range(p),
    } for p in items]


def build_volunteer(idx, profile, meta):
    items, paging = idx.collection(profile, "*profileVolunteerExperiences")
    meta["volunteer"] = section_meta(items, paging)
    return [{
        "role": text(v, "role"),
        "organization": text(v, "companyName"),
        "organization_details": company_ref(idx, v),
        "cause": v.get("cause"),
        "description": text(v, "description"),
        "date_range": date_range(v),
    } for v in items]


# Sections are always present in the output, even when empty. A key that
# disappears when a member has no projects would force every consumer to
# distinguish "missing" from "empty" -- an API should not make them.
SECTIONS = ["experience", "education", "skills", "certifications",
            "languages", "projects", "volunteer"]

SCALARS = ["public_identifier", "profile_url", "urn", "member_id", "first_name",
           "last_name", "full_name", "headline", "about", "location", "industry",
           "profile_picture", "background_image"]


def section_meta(items, paging):
    total = paging.get("total")
    returned = len(items)
    return {"returned": returned, "total": total,
            "complete": total is None or returned >= total}


# --- entry point ------------------------------------------------------------

def normalize(doc):
    idx = Index(doc)
    roots = (doc.get("data") or {}).get("*elements") or []
    if not roots:
        raise ValueError("no root profile urn in data['*elements'] "
                         "(profile may be private, or the session is dead)")
    profile = idx.get(roots[0])
    if not profile:
        raise ValueError(f"root urn {roots[0]} not present in included[]")

    geo = idx.deref(profile.get("geoLocation") or {}, "*geo")
    first, last = text(profile, "firstName"), text(profile, "lastName")
    public_id = profile.get("publicIdentifier")
    sections = {}

    result = {
        "public_identifier": public_id,
        "profile_url": f"https://www.linkedin.com/in/{public_id}" if public_id else None,
        "urn": profile.get("entityUrn"),
        "member_id": (profile.get("objectUrn") or "").rsplit(":", 1)[-1] or None,
        "first_name": first,
        "last_name": last,
        "full_name": " ".join(x for x in (first, last) if x) or None,
        "headline": text(profile, "headline"),
        "about": text(profile, "summary"),
        "location": prune({
            "name": (geo or {}).get("defaultLocalizedName") or text(profile, "locationName"),
            "short_name": (geo or {}).get("defaultLocalizedNameWithoutCountryName"),
            "country_code": (profile.get("location") or {}).get("countryCode"),
            "geo_urn": (profile.get("geoLocation") or {}).get("geoUrn"),
        }) or None,
        "industry": (idx.get(profile.get("industryUrn")) or {}).get("name"),
        "flags": {
            "premium": bool(profile.get("premium")),
            "influencer": bool(profile.get("influencer")),
            "creator": bool(profile.get("creator")),
            "memorialized": bool(profile.get("memorialized")),
        },
        "profile_picture": image_set(profile.get("profilePicture")),
        "background_image": image_set(profile.get("backgroundPicture")),
        "experience": build_experience(idx, profile, sections),
        "education": build_education(idx, profile, sections),
        "skills": build_skills(idx, profile, sections),
        "certifications": build_certifications(idx, profile, sections),
        "languages": build_languages(idx, profile, sections),
        "projects": build_projects(idx, profile, sections),
        "volunteer": build_volunteer(idx, profile, sections),
    }
    # prune tidies nested nulls, then the contract is restored: every scalar and
    # every section keeps its key. Without this, prune() silently deletes the
    # empty `incomplete_sections` list and its own reader raises KeyError.
    result = prune(result)
    for key in SCALARS:
        result.setdefault(key, None)
    for key in SECTIONS:
        result.setdefault(key, [])
    result["flags"] = {k: bool(v) for k, v in (result.get("flags") or {}).items()}
    result["_meta"] = {
        "sections": sections,
        "incomplete_sections": sorted(k for k, v in sections.items()
                                      if not v["complete"]),
        "entities_seen": len(idx.by_urn),
    }
    return {k: result[k] for k in SCALARS + ["flags"] + SECTIONS + ["_meta"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("raw", help="path to a raw voyager blob (from probe.py)")
    ap.add_argument("-o", "--out", help="write JSON here instead of stdout")
    args = ap.parse_args()

    with open(args.raw) as f:
        doc = json.load(f)
    result = normalize(doc)
    body = json.dumps(result, indent=2, ensure_ascii=False)

    if args.out:
        with open(args.out, "w") as f:
            f.write(body + "\n")
        m = result["_meta"]
        print(f"wrote {args.out}")
        for name, s in m["sections"].items():
            mark = "ok " if s["complete"] else "GAP"
            print(f"  [{mark}] {name:16s} {s['returned']}/{s.get('total')}")
        if m["incomplete_sections"]:
            print(f"\n  truncated by the decoration: {', '.join(m['incomplete_sections'])}")
    else:
        print(body)


if __name__ == "__main__":
    sys.exit(main())
