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
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.firecrawl.dev/v2"
TIMEOUT = 120

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

    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json",
                 "authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:200]
        except Exception:                       # noqa: BLE001
            pass
        print(f"  [firecrawl] {path} HTTP {e.code}: {detail}")
        return None
    except (urllib.error.URLError, ValueError, OSError) as e:
        print(f"  [firecrawl] {path} failed: {e}")
        return None

    if not body.get("success"):
        print(f"  [firecrawl] {path} returned success=false")
        return None
    return body


def _host(url):
    try:
        return urllib.parse.urlsplit(url).netloc.replace("www.", "")
    except Exception:                           # noqa: BLE001
        return ""


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
    if dry or not available() or not search_terms:
        return []

    limit = int(cfg.get("results_per_term", 10))
    max_terms = int(cfg.get("max_terms_per_scan", 6))
    place = (locations or ["India"])[0]

    rows, seen = [], set()
    for term in list(search_terms)[:max_terms]:
        body = _post("/search", {
            "query": f"{term} jobs {place}"[:500],
            "sources": ["web"],
            "limit": limit,
            # No scrapeOptions on purpose — see the module docstring.
            # This call must stay cheap.
        })
        if not body:
            continue

        for item in (body.get("data") or {}).get("web", []) or []:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            rows.append({
                "title": item.get("title") or "",
                "company": item.get("companyName") or _host(url),
                "location": place,
                "url": url,
                # A snippet, not a description. Enough for dedup and the
                # title heuristic; deliberately not enough to score on.
                "description": (item.get("description") or "")[:600],
                "source": "firecrawl",
                "needs_enrichment": True,
            })

    print(f"  [firecrawl] {len(rows)} listings from search")
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
    if dry or not available() or not pages:
        return []

    rows = []
    for url in pages[:int(cfg.get("max_pages_per_scan", 15))]:
        body = _post("/scrape", {
            "url": url,
            "onlyMainContent": True,
            "formats": [{"type": "json", "schema": JOB_SCHEMA,
                         "prompt": EXTRACT_PROMPT}],
        })
        if not body:
            continue

        data = body.get("data") or {}
        found = (data.get("json") or {}).get("jobs") or []
        for j in found:
            if not isinstance(j, dict) or not j.get("title"):
                continue

            # Many career pages list roles without per-role links, and
            # the extractor then hands back the page's own URL for every
            # one of them. Since url is the shared pool's identity, that
            # collapses a whole company's openings into a single row —
            # ten Swiggy roles became one. Synthesise a stable per-role
            # fragment whenever the URL is missing or is just the page.
            found_url = (j.get("url") or "").strip()
            if not found_url or found_url.rstrip("/") == url.rstrip("/"):
                found_url = f"{url}#{_slug(j['title'])}-{_slug(j.get('location'))}"

            rows.append({
                "title": j.get("title"),
                "company": j.get("company") or _host(url),
                "location": j.get("location"),
                "url": found_url,
                "description": (j.get("description") or "")[:4000],
                "source": "firecrawl",
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
    if dry or not available():
        return jobs

    budget = int(cfg.get("max_enrich_per_scan", 25))
    spent = 0

    for job in jobs:
        if spent >= budget:
            break
        if not job.get("needs_enrichment") or not job.get("url"):
            continue

        body = _post("/scrape", {"url": job["url"], "onlyMainContent": True,
                                 "formats": ["markdown"]})
        spent += 1
        if not body:
            continue

        data = body.get("data") or {}
        text = data.get("markdown") or ""
        if len(text) > len(job.get("description") or ""):
            job["description"] = text[:4000]
        meta = data.get("metadata") or {}
        if meta.get("title") and not job.get("title"):
            job["title"] = meta["title"]
        job["needs_enrichment"] = False

    if spent:
        print(f"  [firecrawl] enriched {spent} surviving listings")
    return jobs
