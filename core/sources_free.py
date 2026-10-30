"""
THE FREE TIER — every source that costs nothing to ask.

Split out of discover.py, which had grown past a thousand lines and was
the file most often edited when a source was added. Nothing here is
paid: public ATS JSON endpoints that need no key, and JobSpy scraping
Indeed and LinkedIn. Measured, this tier is 700+ listings a scan.

discover.py re-exports all of it, so `discover.from_greenhouse` still
resolves and the fan-out can still be monkeypatched there.
"""

import json
import math
import urllib.error
import urllib.request
from datetime import datetime, timezone


def _now():
    """
    UTC, in the same spelling `jobs.found_at` uses everywhere else.

    This was `datetime.now().isoformat()` — local time, no offset, and a
    different shape from db.now(). Two consequences, both user-visible:
    the board renders found_at by treating a naive value as UTC, so a
    listing scraped at 21:00 IST displayed tomorrow's date; and the
    "found: today / this week" filter compares these strings against a
    UTC bound, which a local-time value fails at the day boundary.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------
# A. Aggregators via JobSpy
# ------------------------------------------------------------
def _clean(value):
    """
    A pandas cell, made safe to treat as text.

    JobSpy returns a DataFrame, and a missing string cell there is not
    None — it is float('nan'), which is TRUTHY. So the obvious
    `(x or "")[:4000]` sails past the guard and then raises
    "'float' object is not subscriptable", killing the rest of that
    search. Measured, this silently discarded every LinkedIn row on
    every term, because LinkedIn listings carry no description.
    """
    if value is None:
        return None
    if isinstance(value, float):
        return None if math.isnan(value) else str(value)
    return str(value)


def from_jobspy(cfg):
    """
    Naukri + LinkedIn + Indeed + Google.

    Every config key is read with a default. This is user-edited YAML, and
    a missing `hours_old` used to raise KeyError out of discover_all and
    take down the whole scan — including the ATS tier that had already
    succeeded.

    An empty location list is the other silent failure: the inner loop
    simply never runs, so the tier reports zero listings and looks like a
    quiet market. A single unrestricted search is the honest fallback.
    """
    from jobspy import scrape_jobs

    rows = []
    locations = list(cfg.get("locations") or []) or [None]
    terms = list(cfg.get("search_terms") or [])

    # Every term is searched in every location, so six terms across two
    # cities is twelve searches at roughly 5-10 seconds each. Unbounded,
    # a growing term list quietly turns a two-minute scan into ten.
    budget = int(cfg.get("max_searches_per_scan", 12) or 0)
    pairs = [(t, l) for t in terms for l in locations]
    if budget and len(pairs) > budget:
        print(f"  [jobspy] {len(pairs)} term/location searches capped at "
              f"{budget}")
        offset = int(cfg.get("search_offset", 0)) % len(pairs)
        pairs = (pairs + pairs)[offset:offset + budget]

    for term, loc in pairs:
        try:
            df = scrape_jobs(
                site_name=cfg.get("sites") or ["linkedin", "indeed"],
                search_term=term,
                location="India" if loc and "remote" in loc.lower() else loc,
                is_remote=bool(loc and "remote" in loc.lower()),
                results_wanted=int(cfg.get("results_wanted", 40)),
                hours_old=int(cfg.get("hours_old", 48)),
                country_indeed=cfg.get("country_indeed", "India"),
                linkedin_fetch_description=bool(cfg.get("fetch_descriptions", True)),
            )
            for _, r in df.iterrows():
                rows.append({
                    "title": _clean(r.get("title")),
                    "company": _clean(r.get("company")),
                    "location": _clean(r.get("location")),
                    "url": _clean(r.get("job_url")),
                    "description": (_clean(r.get("description")) or "")[:18000],
                    "source": _clean(r.get("site")),
                    "skills": _clean(r.get("skills")),
                    "experience_range": _clean(r.get("experience_range")),
                    "company_rating": r.get("company_rating"),
                    "found_at": _now(),
                })
        except Exception as e:
            if STRICT:
                raise
            print(f"  [jobspy] {term} @ {loc} failed: {e}")
    return rows


# ------------------------------------------------------------
# B. Direct ATS — public JSON, no scraping, never breaks
# ------------------------------------------------------------
# A scan must survive one dead board: every fetcher below catches its own
# errors, prints, and returns an empty list, so a 404 on one slug cannot
# take the tier down. The cost of that is a config that cannot be
# checked -- a slug that has 404'd for weeks and a company that simply
# is not hiring both come back as zero listings.
#
# STRICT lets one caller opt out. `run.py doctor` turns it on so it can
# tell "this slug is wrong" from "this board is quiet"; nothing on the
# scanning path ever touches it.
STRICT = False


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "careerloop/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def from_greenhouse(slug):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    out = []
    try:
        for j in _get_json(url).get("jobs", []):
            out.append({
                "title": j.get("title"),
                "company": slug,
                "location": (j.get("location") or {}).get("name"),
                "url": j.get("absolute_url"),
                "description": (j.get("content") or "")[:4000],
                "source": "greenhouse",
                "found_at": _now(),
            })
    except Exception as e:
        if STRICT:
            raise
        print(f"  [greenhouse:{slug}] {e}")
    return out


def from_lever(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    out = []
    try:
        for j in _get_json(url):
            out.append({
                "title": j.get("text"),
                "company": slug,
                "location": (j.get("categories") or {}).get("location"),
                "url": j.get("hostedUrl"),
                "description": (j.get("descriptionPlain") or "")[:4000],
                "source": "lever",
                "found_at": _now(),
            })
    except Exception as e:
        if STRICT:
            raise
        print(f"  [lever:{slug}] {e}")
    return out


def from_ashby(slug):
    """
    Ashby boards, the third public-JSON ATS.

    config/sources.yml has declared an `ashby:` key since the first
    version, but nothing read it — adding slugs there produced silence,
    no listings and no warning. Ashby skews toward newer, well-funded
    startups, which is exactly the stretch-role end of the market this
    tool is pointed at.
    """
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    out = []
    try:
        for j in _get_json(url).get("jobs", []):
            if j.get("isListed") is False:
                continue
            out.append({
                "title": j.get("title"),
                "company": slug,
                "location": j.get("location"),
                # jobUrl is the public posting; applyUrl jumps straight
                # into the form, which is not what a board should link to.
                "url": j.get("jobUrl"),
                "description": (j.get("descriptionPlain") or "")[:4000],
                "source": "ashby",
                "found_at": _now(),
            })
    except Exception as e:
        if STRICT:
            raise
        print(f"  [ashby:{slug}] {e}")
    return out


def from_smartrecruiters(slug, cap=100):
    """
    SmartRecruiters boards. Free, no auth, same bargain as the three above.

    Verified live against this endpoint: it returns `totalFound` and a
    `content` array whose items carry `name`, `location`, `id` and
    `company`. The public posting URL is built from the slug and id —
    `ref` in the payload points at the API, not at something a person
    can open.

    List-only, deliberately: the description needs one extra request per
    posting, and the shortlister reads titles anyway (see
    core/semantic.py). A thin description is already a handled case here
    — the career-page scraper produces them too.
    """
    url = (f"https://api.smartrecruiters.com/v1/companies/{slug}"
           f"/postings?limit={cap}")
    out = []
    try:
        for j in _get_json(url).get("content", []):
            job_id = j.get("id")
            if not job_id:
                continue
            loc = j.get("location") or {}
            where = ", ".join(str(p) for p in (loc.get("city"),
                                               loc.get("region"),
                                               loc.get("country")) if p)
            out.append({
                "title": j.get("name"),
                "company": (j.get("company") or {}).get("name") or slug,
                "location": where or None,
                "url": f"https://jobs.smartrecruiters.com/{slug}/{job_id}",
                "description": "",
                "source": "smartrecruiters",
                "found_at": _now(),
            })
    except Exception as e:
        if STRICT:
            raise
        print(f"  [smartrecruiters:{slug}] {e}")
    return out


def from_instahyre(max_pages=8, page_size=35):
    """
    Instahyre — India-specific tech hiring, open, free, no key.

    Verified live: 13,949 live roles behind a public JSON endpoint.
    This is the largest free India-first source found, and unlike the
    ATS boards it is not per-company, so one call reaches employers
    nobody has listed anywhere in this config.

    IT CANNOT BE SEARCHED. Every parameter tried — q, query, search,
    keyword, title, company_name — returns the identical 13,949 and the
    identical first page, so there is no server-side filtering to lean
    on. That is fine here in a way it would not be for a paid source:
    the shortlister is local and free at about 7ms a listing, so pulling
    a few hundred and ranking them costs nothing but a few seconds of
    HTTP. It is bounded by pages rather than by relevance because
    relevance is not on offer.

    No descriptions in the feed — same as SmartRecruiters and Workday,
    and the shortlister reads titles anyway.
    """
    base = "https://www.instahyre.com/api/v1/job_search"
    out, seen = [], set()
    for page in range(max_pages):
        url = f"{base}?limit={page_size}&offset={page * page_size}"
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0",
                              "accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                payload = json.loads(r.read().decode())
        except Exception as e:
            if STRICT:
                raise
            print(f"  [instahyre] page {page}: {e}")
            break

        objects = payload.get("objects") or []
        if not objects:
            break

        for j in objects:
            url_ = j.get("public_url")
            title = j.get("title")
            if not url_ or not title or url_ in seen:
                continue
            seen.add(url_)
            employer = j.get("employer") or {}
            # `keywords` is the only text the feed carries, and the
            # scorer does better with something than with nothing.
            keywords = j.get("keywords") or ""
            out.append({
                "title": title,
                "company": employer.get("company_name"),
                "location": (j.get("locations") or "").replace(",", ", ")
                or None,
                "url": url_,
                "description": str(keywords)[:4000],
                "source": "instahyre",
                "found_at": _now(),
            })
    print(f"  [instahyre] {len(out)} listings from {max_pages} pages")
    return out


def from_workday(target, cap=100):
    """
    Workday boards — the largest single gap in free coverage.

    Most big enterprises run Workday, and unlike the other four this one
    is a POST with a JSON body rather than a GET, which is why it is
    usually skipped. It is still public and unauthenticated.

    `target` is "tenant/site" or "tenant/site/wd5" — read them off the
    careers URL, which looks like:
        https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
                ^tenant  ^wd                     ^site

    Verified live: NVIDIA returns total=2000 with jobPostings carrying
    title, locationsText, postedOn and externalPath. There is no
    description in the list response — same trade as SmartRecruiters,
    and the shortlister reads titles anyway.
    """
    parts = [p for p in str(target or "").split("/") if p]
    if len(parts) < 2:
        print(f"  [workday:{target}] expected 'tenant/site' — skipped")
        return []
    tenant, site = parts[0], parts[1]
    wd = parts[2] if len(parts) > 2 else "wd5"

    url = (f"https://{tenant}.{wd}.myworkdayjobs.com"
           f"/wday/cxs/{tenant}/{site}/jobs")
    body = json.dumps({"appliedFacets": {}, "limit": min(cap, 100),
                       "offset": 0, "searchText": ""}).encode()
    out = []
    try:
        req = urllib.request.Request(url, data=body, headers={
            "content-type": "application/json",
            "accept": "application/json",
            "User-Agent": "careerloop/1.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            payload = json.loads(r.read().decode())

        for j in payload.get("jobPostings", []) or []:
            path = j.get("externalPath")
            title = j.get("title")
            if not path or not title:
                continue
            out.append({
                "title": title,
                "company": tenant,
                "location": j.get("locationsText"),
                "url": (f"https://{tenant}.{wd}.myworkdayjobs.com"
                        f"/en-US/{site}{path}"),
                "description": "",
                "source": "workday",
                "found_at": _now(),
            })
    except Exception as e:
        if STRICT:
            raise
        print(f"  [workday:{tenant}] {e}")
    return out


def from_workable(slug):
    """
    Workable boards. Free and no auth — the account endpoint answers, but
    every public slug tried while writing this had an empty `jobs` array,
    so the per-job field names below are NOT verified against live data.

    Hence the tolerant reads: a shape mismatch costs one skipped board
    and one printed line, not a broken scan. If a Workable board is added
    and returns nothing, this is the first place to look.
    """
    url = f"https://www.workable.com/api/accounts/{slug}?details=true"
    out = []
    try:
        payload = _get_json(url)
        company = payload.get("name") or slug
        for j in payload.get("jobs", []) or []:
            link = j.get("url") or j.get("shortlink") or j.get("application_url")
            if not link:
                continue
            loc = j.get("location")
            if isinstance(loc, dict):
                loc = ", ".join(str(p) for p in (loc.get("city"),
                                                 loc.get("country")) if p)
            out.append({
                "title": j.get("title") or j.get("full_title"),
                "company": company,
                "location": loc or None,
                "url": link,
                "description": (j.get("description") or "")[:4000],
                "source": "workable",
                "found_at": _now(),
            })
    except Exception as e:
        if STRICT:
            raise
        print(f"  [workable:{slug}] {e}")
    return out
