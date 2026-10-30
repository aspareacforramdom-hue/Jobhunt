"""
TIER 2.5 — Firecrawl Cloud.

Sits between the direct ATS boards and Apify, and covers what neither
reaches: roles on job boards this tool has no adapter for, and companies
whose careers page is bespoke rather than Greenhouse or Lever.

Cloud only. The self-hosted build was evaluated and rejected: its
anti-bot layer ("Fire-engine") is proprietary and absent from the open
source stack, so it fetches with plain Playwright and gets fingerprinted
by exactly the protection that makes Naukri hard — the one job it would
have been hired for. It also wants Postgres, Redis, RabbitMQ, Playwright
and an API container on a laptop that has to serve the portal instantly
on boot. Apify keeps Naukri; this covers the long tail.

COST CONTROL IS THE DESIGN

Search is billed per result set, scraping is billed per page. So this
module searches cheaply and scrapes almost nothing:

  1. /search returns title, url and a snippet for ~2 credits per 10
     results. That is enough to dedup and prefilter on.
  2. Only listings that SURVIVE the free prefilter get scraped for their
     full text, one credit each.

That is the same bargain core/prefilter.py already makes with the LLM —
spend nothing on the 80% you were going to throw away — applied to
crawl credits. Scraping every search result first would cost roughly six
times as much for listings the prefilter kills seconds later.

Every entry point no-ops without FIRECRAWL_API_KEY, and every failure is
caught and reported rather than raised: a scan that loses one source
should degrade, not die.
"""

import json
import os
import re
import time
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from contextvars import ContextVar

from core import firecrawl_budget

API = "https://api.firecrawl.dev/v2"
TIMEOUT = 120
_NOTES = ContextVar("firecrawl_notes", default=())


def _note(message):
    _NOTES.set((*_NOTES.get(), message))
    print("  [firecrawl] " + message)


def take_notes():
    notes = list(dict.fromkeys(_NOTES.get()))
    _NOTES.set(())
    return notes

# What a listing looks like, for the extractor. Deliberately free of any
# role, industry or seniority language — the same constraint the scorer
# lives under. The query supplies the subject matter, not this schema.
JOB_SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "company": {"type": "string"},
                    "location": {"type": "string"},
                    "url": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["title"],
            },
        }
    },
}

EXTRACT_PROMPT = (
    "Extract every individual job opening listed on this page. "
    "Return the role title, the hiring company, the work location, the "
    "direct link to that specific opening, and the fullest description "
    "text available. Ignore navigation, promotional copy and adverts for "
    "other companies. If the page is not a job listing, return an empty "
    "list rather than inventing entries."
)


def available():
    return bool(os.environ.get("FIRECRAWL_API_KEY"))


def _post(path, payload):
    """Returns the parsed body, or None. Never raises."""
    key = os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        return None

    try:
        entry, cached = firecrawl_budget.reserve(path, payload)
    except (firecrawl_budget.BudgetError, OSError, sqlite3.Error) as error:
        _note(str(error) if isinstance(error, firecrawl_budget.BudgetError)
              else "Firecrawl ledger unavailable; paid requests stopped")
        return None
    if cached is not None:
        _note("Firecrawl cached response reused; no new credits")
        return cached

    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json",
                 "authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read(4_000_001)
            if len(raw) > 4_000_000:
                raise ValueError("Response too large")
            body = json.loads(raw.decode())
    except urllib.error.HTTPError as e:
        # Bodies may echo submitted URLs, credentials or private data.
        # A server error may occur after charging; preserve its reservation.
        if 400 <= e.code < 500 and e.code != 408:
            firecrawl_budget.finish(entry)
        _note(f"Firecrawl {path} HTTP {e.code}; no automatic paid retry")
        return None
    except (urllib.error.URLError, ValueError, OSError):
        _note(f"Firecrawl {path} outcome unknown; paid requests paused for review")
        return None

    if not isinstance(body, dict) or not body.get("success"):
        firecrawl_budget.finish(entry)
        _note(f"Firecrawl {path} returned an invalid or failed response")
        return None
    firecrawl_budget.finish(entry, body)
    return body


def _host(url):
    try:
        return urllib.parse.urlsplit(url).netloc.replace("www.", "")
    except Exception:                           # noqa: BLE001
        return ""


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _web_url(value, base=""):
    if not isinstance(value, str):
        return ""
    value = urllib.parse.urljoin(base, value.strip())
    try:
        parts = urllib.parse.urlsplit(value)
        return value if parts.scheme in {"http", "https"} and parts.hostname and not parts.username else ""
    except ValueError:
        return ""


def _collection_page(title, url):
    """Search pages are discovery pointers, never individual applications."""
    title = str(title or "").lower()
    parts = urllib.parse.urlsplit(url)
    path = parts.path.lower()
    host = (parts.hostname or "").lower()
    return bool(re.search(r"\bjobs? in\b|\bjobs\s*(?:[-|–]|$)|\d[\d,+]*\s+(?:\w+\s+){0,5}jobs\b|\bbest\b.*\bjobs\b", title)
                or any(part in path for part in ("/jobs/search", "/jobs-in-", "-jobs-in-", "/career-advice/", "-jobs-srch"))
                or (host.endswith("glassdoor.com") and path.startswith("/job/")
                    and "-jobs-" in path))


def _individual_posting_url(url):
    """A search snippet needs a plausible job-detail path before storage."""
    path = urllib.parse.urlsplit(url).path.lower()
    return bool(re.search(r"/(?:job|jobs|careers)/(?:view/)?[^/]{3,}", path)
                or re.search(r"/(?:job-listing|job-details|job-listings|positions)/[^/]{3,}", path))


# ------------------------------------------------------------
# 1. Discovery — driven by each user's own search terms
# ------------------------------------------------------------
def search_jobs(cfg, search_terms, locations, dry=False):
    """
    Find listings across the open web using the union of the users'
    calibrated search terms.

    No company list and no role vocabulary is hardcoded here. The query
    is built from the profile, so a brand marketer and an infrastructure
    engineer search for genuinely different things through the same code.

    Returns cheap rows: title, url, and the search snippet as the
    description. `enrich_descriptions` fills in the real text later, for
    the few that survive the prefilter.
    """
    if dry or cfg.get("enabled") is False or not available() or not search_terms:
        return []

    limit = max(1, min(10, int(cfg.get("results_per_term", 10))))
    max_terms = max(0, int(cfg.get("max_terms_per_scan", 6)))
    place = (locations or ["India"])[0]

    rows, seen = [], set()
    terms = list(search_terms)
    if cfg.get("rotate_daily"):
        offset = (int(time.time() // 86400) * max_terms) % len(terms)
        terms = (terms + terms)[offset:offset + max_terms]
    for term in terms[:max_terms]:
        query = f"{term} jobs {place}"
        if cfg.get("individual_postings_only"):
            query += (" (inurl:job/ OR inurl:jobs/view/ OR inurl:job-details/ OR inurl:careers/job/)"
                      " -site:glassdoor.com -site:indeed.com -site:linkedin.com"
                      " -site:naukri.com")
        body = _post("/search", {
            "query": query[:500],
            "sources": ["web"],
            "limit": limit,
            # No scrapeOptions on purpose — see the module docstring.
            # This call must stay cheap.
        })
        if not body:
            continue

        items = _mapping(_mapping(body).get("data")).get("web")
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            url = _web_url(item.get("url"))
            if (not url or url in seen or _collection_page(item.get("title"), url)
                    or (cfg.get("individual_postings_only") and not _individual_posting_url(url))):
                continue
            seen.add(url)
            rows.append({
                "title": str(item.get("title") or ""),
                "company": item.get("companyName") or _host(url),
                # Query location is a preference, not employer evidence.
                "location": item.get("location") or None,
                "url": url,
                # A snippet, not a description. Enough for dedup and the
                # title heuristic; deliberately not enough to score on.
                "description": str(item.get("description") or "")[:600],
                "source": "firecrawl",
                "needs_enrichment": True,
                "source_note": "Search result; opening and work location require verification",
            })

    _note(f"Firecrawl: {len(rows)} individual candidate links; verify before applying")
    return rows


# ------------------------------------------------------------
# 2. Career pages — companies not on any ATS we speak
# ------------------------------------------------------------
def scrape_career_pages(cfg, dry=False):
    """
    Pull listings off configured careers URLs.

    This is the gap Greenhouse and Lever leave: an employer running
    Darwinbox, Keka, Zoho Recruit or something bespoke. Those pages have
    no bot protection, so one scrape with structured extraction is
    enough.
    """
    pages = cfg.get("career_pages") or []
    if dry or cfg.get("enabled") is False or not available() or not pages:
        return []

    rows = []
    for url in pages[:max(0, int(cfg.get("max_pages_per_scan", 15)))]:
        url = _web_url(url)
        if not url:
            continue
        body = _post("/scrape", {
            "url": url,
            "proxy": "basic",
            "parsers": [],
            "onlyMainContent": True,
            "formats": [{"type": "json", "schema": JOB_SCHEMA,
                         "prompt": EXTRACT_PROMPT}],
        })
        if not body:
            continue

        data = _mapping(_mapping(body).get("data"))
        found = _mapping(data.get("json")).get("jobs")
        for j in found if isinstance(found, list) else []:
            if not isinstance(j, dict) or not isinstance(j.get("title"), str) or not j["title"].strip():
                continue

            # Many career pages list roles without per-role links, and
            # the extractor then hands back the page's own URL for every
            # one of them. Since url is the shared pool's identity, that
            # collapses a whole company's openings into a single row —
            # ten Swiggy roles became one. Synthesise a stable per-role
            # fragment whenever the URL is missing or is just the page.
            found_url = _web_url(j.get("url"), url)
            if not found_url or found_url.rstrip("/") == url.rstrip("/"):
                found_url = (f"{url.split('#')[0]}#careerloop-job="
                             f"{_slug(j['title'])}-{_slug(j.get('location'))}")

            rows.append({
                "title": j.get("title"),
                "company": j.get("company") or _host(url),
                "location": j.get("location"),
                "url": found_url,
                "description": (j.get("description") or "")[:4000],
                "source": "firecrawl",
                "source_note": "Extracted career-page lead; verify current opening with employer",
            })

    print(f"  [firecrawl] {len(rows)} listings from {len(pages)} career pages")
    return rows


def _slug(text):
    return "".join(c if c.isalnum() else "-"
                   for c in (text or "").lower()).strip("-")[:80]


# ------------------------------------------------------------
# 3. Enrichment — pay only for what survived
# ------------------------------------------------------------
def enrich_descriptions(jobs, cfg, dry=False):
    """
    Fetch full text for the listings that made it through the prefilter.

    Called after prefilter, deliberately. The scorer needs real
    description text to judge a role, but a search snippet is enough to
    decide the role is irrelevant — so the expensive fetch happens only
    for the survivors. Mutates and returns the list.
    """
    if dry or cfg.get("enabled") is False or not available():
        return jobs

    budget = int(cfg.get("max_enrich_per_scan", 25))
    spent = 0

    for job in jobs:
        if spent >= budget:
            break
        if not job.get("needs_enrichment") or not job.get("url"):
            continue

        body = _post("/scrape", {"url": job["url"], "proxy": "basic", "parsers": [], "onlyMainContent": True,
                                 "formats": ["markdown"]})
        spent += 1
        if not body:
            continue

        data = _mapping(_mapping(body).get("data"))
        text = data.get("markdown") or ""
        meta = _mapping(data.get("metadata"))
        if (not isinstance(text, str) or len(text.strip()) < 350
                or _collection_page(meta.get("title"), job["url"])
                or str(meta.get("statusCode", "200"))[:1] in {"4", "5"}):
            _note("Firecrawl returned empty or unavailable page; listing remains unverified")
            continue
        if len(text) > len(job.get("description") or ""):
            # Preserve requirements near the end/middle instead of spending
            # a page credit only to retain its navigation and company intro.
            from core.score import compact_description
            job["description"] = compact_description(text)
        if meta.get("title") and not job.get("title"):
            job["title"] = meta["title"]
        job["needs_enrichment"] = False

    if spent:
        _note(f"Firecrawl: attempted {spent} shortlisted pages; unresolved descriptions stay unverified")
    return jobs
