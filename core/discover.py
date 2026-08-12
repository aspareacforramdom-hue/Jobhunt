"""
LAYER 1 — DISCOVERY
Pulls raw listings from Indian aggregators + direct ATS boards.
Returns a flat list of dicts. No scoring here.
"""

import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from core import firecrawl


# ------------------------------------------------------------
# A. Aggregators via JobSpy
# ------------------------------------------------------------
def from_jobspy(cfg):
    """Naukri + LinkedIn + Indeed + Google, concurrently."""
    from jobspy import scrape_jobs

    rows = []
    for term in cfg["search_terms"]:
        for loc in cfg["locations"]:
            try:
                df = scrape_jobs(
                    site_name=cfg["sites"],
                    search_term=term,
                    location=loc,
                    results_wanted=cfg["results_wanted"],
                    hours_old=cfg["hours_old"],
                    country_indeed=cfg["country_indeed"],
                )
                for _, r in df.iterrows():
                    rows.append({
                        "title": r.get("title"),
                        "company": r.get("company"),
                        "location": r.get("location"),
                        "url": r.get("job_url"),
                        "description": (r.get("description") or "")[:4000],
                        "source": r.get("site"),
                        "skills": r.get("skills"),
                        "experience_range": r.get("experience_range"),
                        "company_rating": r.get("company_rating"),
                        "found_at": datetime.now().isoformat(),
                    })
            except Exception as e:
                print(f"  [jobspy] {term} @ {loc} failed: {e}")
    return rows


# ------------------------------------------------------------
# B. Direct ATS — public JSON, no scraping, never breaks
# ------------------------------------------------------------
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
                "found_at": datetime.now().isoformat(),
            })
    except Exception as e:
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
                "found_at": datetime.now().isoformat(),
            })
    except Exception as e:
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
                "found_at": datetime.now().isoformat(),
            })
    except Exception as e:
        print(f"  [ashby:{slug}] {e}")
    return out


# ------------------------------------------------------------
# C. Apify — the PRIMARY Naukri path
#
# Naukri's bot protection changes monthly and returned 406 recaptcha
# from a cloud IP during testing. A maintained actor at ~$7/month beats
# a free scraper that silently returns zero rows on a Tuesday morning.
# ------------------------------------------------------------
APIFY_RUN = ("https://api.apify.com/v2/acts/{actor}/"
             "run-sync-get-dataset-items?token={token}&timeout=300")

# Actors disagree about field names. Try each in order.
_FIELDS = {
    "title": ("title", "jobTitle", "name", "designation"),
    "company": ("company", "companyName", "employer"),
    "location": ("location", "jobLocation", "place", "city"),
    "url": ("url", "jobUrl", "link", "jobLink", "detailUrl"),
    "description": ("description", "jobDescription", "jd", "summary"),
    "rating": ("companyRating", "rating", "ambitionBoxRating"),
    "review_count": ("reviewCount", "companyReviews", "reviewsCount"),
}


def _pick(row, field):
    for key in _FIELDS[field]:
        if row.get(key) not in (None, ""):
            return row[key]
    return None


def apify_available():
    return bool(os.environ.get("APIFY_TOKEN"))


def from_apify(cfg, search_terms, locations):
    """Naukri via a maintained actor. Returns [] and says why on failure."""
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        return []

    actor = cfg.get("naukri_actor", "").replace("/", "~")
    url = APIFY_RUN.format(actor=actor, token=token)
    rows = []

    for term in search_terms:
        payload = {
            "keyword": term,
            "searchQuery": term,
            "location": ", ".join(locations) if locations else "India",
            "maxItems": cfg.get("results_wanted", 40),
        }
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                items = json.loads(r.read().decode())
        except (urllib.error.URLError, ValueError, OSError) as e:
            print(f"  [apify] '{term}' failed: {e}")
            continue

        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict) or not _pick(it, "url"):
                continue
            rows.append({
                "title": _pick(it, "title"),
                "company": _pick(it, "company"),
                "location": _pick(it, "location"),
                "url": _pick(it, "url"),
                "description": (_pick(it, "description") or "")[:4000],
                "source": "naukri",
                # This actor attaches AmbitionBox ratings to the listing,
                # which is why it is preferred — reviews and the Naukri
                # block are solved in one call. See core/reviews.py.
                "company_rating": _pick(it, "rating"),
                "company_review_count": _pick(it, "review_count"),
                "found_at": datetime.now().isoformat(),
            })

    print(f"  [apify] {len(rows)} listings from Naukri")
    return rows


# ------------------------------------------------------------
# Tier 1 fan-out
# ------------------------------------------------------------
ATS_WORKERS = 8


def _fetch_ats(ats):
    """
    Fetch every configured ATS board concurrently.

    These were fetched one slug at a time. Measured, a single Greenhouse
    board takes ~960ms, essentially all of it waiting on the socket — so
    thirty boards cost ~29 seconds of an otherwise idle CPU, and that
    delay lands squarely on the boot scan the user is waiting behind.

    Threads, not asyncio: urllib releases the GIL during I/O, so a small
    pool gets the full win without making every caller async. The pool is
    capped so a long slug list cannot open thirty sockets at once and
    look like abuse to the ATS.
    """
    jobs = ([(from_greenhouse, s) for s in ats.get("greenhouse", []) or []]
            + [(from_lever, s) for s in ats.get("lever", []) or []]
            + [(from_ashby, s) for s in ats.get("ashby", []) or []])
    if not jobs:
        return []

    rows = []
    with ThreadPoolExecutor(max_workers=min(ATS_WORKERS, len(jobs))) as pool:
        futures = {pool.submit(fn, slug): slug for fn, slug in jobs}
        for future in as_completed(futures):
            try:
                rows += future.result()
            except Exception as e:               # noqa: BLE001
                # One dead slug must not take the whole tier down.
                print(f"  [ats:{futures[future]}] failed: {e}")
    return rows


# ------------------------------------------------------------
# Orchestrator
# ------------------------------------------------------------
def discover_all(sources, dry=False):
    """
    Runs the tiers in reliability order and reports which ones ran.

    Returns (rows, notes) where notes is shown in the UI — a scan that
    quietly degraded to two sources should say so rather than look like
    a thin day in the market.
    """
    rows, notes = [], []

    ats = sources.get("ats_targets", {})
    print("→ Tier 1: direct ATS boards (public JSON, never breaks)...")
    rows += _fetch_ats(ats)
    notes.append(f"ATS boards: {len(rows)} listings")

    apify_cfg = sources.get("apify", {})
    terms = sources.get("jobspy", {}).get("search_terms", [])
    locs = sources.get("jobspy", {}).get("locations", [])

    # Tier 2.5: Firecrawl covers what neither the ATS JSON nor Apify
    # reaches — boards with no adapter here, and bespoke careers pages.
    # Search only; the expensive full-text fetch happens after the
    # prefilter, on survivors only. See core/firecrawl.py.
    fc_cfg = sources.get("firecrawl", {})
    if fc_cfg.get("enabled") and not dry:
        if firecrawl.available():
            print("→ Tier 2.5: Firecrawl (open web + career pages)...")
            before = len(rows)
            if fc_cfg.get("search_enabled", True):
                rows += firecrawl.search_jobs(fc_cfg, terms, locs)
            rows += firecrawl.scrape_career_pages(fc_cfg)
            notes.append(f"Firecrawl: {len(rows) - before} listings")
        else:
            notes.append("FIRECRAWL_API_KEY not set — open-web search "
                         "and career pages skipped")

    naukri_ok = False
    if apify_cfg.get("enabled") and terms and not dry:
        if apify_available():
            print("→ Tier 3: Naukri via Apify...")
            before = len(rows)
            rows += from_apify(apify_cfg, terms, locs)
            naukri_ok = len(rows) > before
            notes.append(f"Naukri via Apify: {len(rows) - before} listings")
        else:
            notes.append("APIFY_TOKEN not set — Naukri skipped, "
                         "running on free sources only")
    elif dry:
        notes.append("dry run — paid sources skipped")

    if sources.get("jobspy", {}).get("enabled") and terms:
        print("→ Tier 2: JobSpy (LinkedIn/Indeed)...")
        before = len(rows)
        cfg = dict(sources["jobspy"])
        if not naukri_ok and "naukri" not in cfg.get("sites", []):
            # Free fallback for Naukri only when the paid path produced
            # nothing. Expected to fail from some IPs; that is the point
            # of it being the fallback.
            cfg["sites"] = list(cfg.get("sites", [])) + ["naukri"]
        rows += from_jobspy(cfg)
        notes.append(f"JobSpy: {len(rows) - before} listings")

    print(f"→ {len(rows)} raw listings found")
    return rows, notes
