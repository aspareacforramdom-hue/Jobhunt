"""Career source inventory, bounded collectors, and persistent retry scheduling.

Imported boards are candidates, not endorsements or verified openings. Workers
never write SQLite; the owner thread commits every completed result.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from core import db
from core.enrich import fetch, public_url

DATASET = "https://github.com/Feashliaa/job-board-aggregator"
RAW = "https://raw.githubusercontent.com/Feashliaa/job-board-aggregator/main/data/"
SUPPORTED = {"greenhouse", "lever", "ashby", "workday"}
ROLE = re.compile(r"operations|onboarding|client servic|customer (?:experience|service|operations)|service delivery|process improvement|business execution|project manager|program manager|implementation|branch manager", re.IGNORECASE)
TECH = re.compile(r"software|developer|devops|machine learning|data scientist|site reliability", re.IGNORECASE)
FINANCE = re.compile(r"bank|financial|finance|payment|insurance|credit|capital|fintech|fidelity|visa|mastercard|paypal|statestreet|morgan|barclays|northerntrust|wellsfargo|crisil|hsbc|goldman", re.IGNORECASE)
CURATED = [
    ("HSBC", "https://portal.careers.hsbc.com/careers?location=India&query=operations", "eightfold"),
    ("Standard Chartered", "https://jobs.standardchartered.com/search/?q=operations&locationsearch=India", "successfactors"),
    ("Goldman Sachs", "https://higher.gs.com/", "goldman"),
    ("CRISIL", "https://career.crisil.com/", "career_page"),
    ("JPMorgan Chase", "https://careers.jpmorgan.com/", "career_page"),
    ("Morgan Stanley", "https://www.morganstanley.com/people-opportunities", "career_page"),
    ("Citi", "https://jobs.citi.com/", "career_page"),
    ("Deutsche Bank", "https://careers.db.com/", "career_page"),
    ("UBS", "https://www.ubs.com/global/en/careers.html", "career_page"),
    ("BNP Paribas", "https://group.bnpparibas/en/careers", "career_page"),
    ("RBI recruitment", "https://opportunities.rbi.org.in/", "government"),
    ("SEBI recruitment", "https://www.sebi.gov.in/sebiweb/about/AboutAction.do?doVacancies=yes", "government"),
    ("SBI careers", "https://sbi.co.in/web/careers", "government"),
    ("IBPS", "https://www.ibps.in/", "government"),
    ("NABARD", "https://www.nabard.org/careers-notices1.aspx?cid=693&id=26", "government"),
    ("Employment News", "https://employmentnews.gov.in/", "government"),
]


def seed_curated(conn):
    for company, url, platform in CURATED:
        conn.execute("INSERT INTO career_sources(company,url,platform,locator,provenance,priority) VALUES (?,?,?,?,?,100) ON CONFLICT(url) DO UPDATE SET platform=excluded.platform, next_check=CASE WHEN career_sources.platform != excluded.platform THEN NULL ELSE career_sources.next_check END",
                     (company, url, platform, url, "Curated career-page candidate; verify on fetch"))
    conn.commit()


def board_record(platform, value):
    """Strict identifiers only; never execute a URL supplied by the dataset."""
    if not isinstance(value, str) or len(value) > 240:
        return None
    if platform == "workday":
        parts = value.split("|")
        if len(parts) != 3:
            return None
        tenant, shard, site = parts
        if not re.fullmatch(r"[a-zA-Z0-9-]+", tenant) or not re.fullmatch(r"wd\d+", shard) or not re.fullmatch(r"[a-zA-Z0-9_-]+", site):
            return None
        if tenant == shard or tenant.startswith("wd") and tenant[2:].isdigit():
            return None
        url = f"https://{tenant}.{shard}.myworkdayjobs.com/{site}"
        company = tenant
    elif platform in SUPPORTED and re.fullmatch(r"[a-zA-Z0-9_-]+", value):
        host = {"greenhouse": "job-boards.greenhouse.io", "lever": "jobs.lever.co", "ashby": "jobs.ashbyhq.com"}[platform]
        url, company = f"https://{host}/{value}", value
    else:
        return None
    priority = 30 if FINANCE.search(company + " " + value) else 0
    return (company, url, platform, value, DATASET + " · CC BY-NC 4.0 · candidate identifiers", priority)


def import_catalog(conn, directory=None):
    seed_curated(conn)
    count = 0
    for platform in sorted(SUPPORTED):
        if directory:
            payload = json.loads((Path(directory) / f"{platform}_companies.json").read_text(encoding="utf-8"))
        else:
            response = requests.get(RAW + f"{platform}_companies.json", timeout=30)
            response.raise_for_status()
            if len(response.content) > 5_000_000:
                raise ValueError("Catalog exceeds size limit")
            payload = response.json()
        if not isinstance(payload, list) or len(payload) > 50000:
            raise ValueError("Invalid company catalog")
        records = [record for value in payload if (record := board_record(platform, value))]
        before = conn.total_changes
        conn.executemany("INSERT INTO career_sources(company,url,platform,locator,provenance,priority) VALUES (?,?,?,?,?,?) ON CONFLICT(url) DO NOTHING", records)
        count += conn.total_changes - before
        conn.commit()
    return count


def inventory(conn, query="", state="", page=1):
    from core.scrapling_source import available
    clause, args = "1=1", []
    if query:
        clause += " AND (company LIKE ? OR platform LIKE ?)"
        args.extend([f"%{query[:100]}%"] * 2)
    if state == "attention":
        clause += " AND state IN ('manual','blocked','error')"
    elif state == "returned":
        clause += " AND state IN ('live','partial')"
    elif state:
        clause += " AND state=?"
        args.append(state)
    total = conn.execute(f"SELECT COUNT(*) FROM career_sources WHERE {clause}", args).fetchone()[0]
    page = max(1, min(page, max(1, (total + 49) // 50)))
    rows = [dict(r) for r in conn.execute(f"SELECT * FROM career_sources WHERE {clause} ORDER BY priority DESC,company,id LIMIT 50 OFFSET ?", [*args, (page - 1) * 50])]
    counts = {r["state"]: r["n"] for r in conn.execute("SELECT state,COUNT(*) n FROM career_sources GROUP BY state")}
    return {"html_fetcher": "Scrapling" if available() else "Requests (Scrapling unavailable or disabled)",
            "rows": rows, "total": total, "page": page, "pages": max(1, (total + 49) // 50), "counts": counts,
            "all_count": sum(counts.values()), "checked": sum(n for s, n in counts.items() if s != "queued")}


def _json(url, body=None):
    public_url(url)
    method = requests.get if body is None else requests.post
    kwargs = {} if body is None else {"json": body}
    with method(url, timeout=(8, 18), allow_redirects=False, stream=True,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, **kwargs) as response:
        response.raise_for_status()
        if response.is_redirect:
            raise ValueError("API redirect needs review")
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > 12_000_000:
                raise ValueError("Board response exceeds 12 MB")
            chunks.append(chunk)
        return json.loads(b"".join(chunks))


def _text(value):
    return BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)[:18000]


def _job(source, title, url, location="", description=""):
    if not isinstance(title, str) or not title.strip() or not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"}:
        return None
    clean_description = _text(description)
    return {"title": _text(title), "url": url, "company": source["company"], "location": _text(location),
            "description": clean_description, "needs_enrichment": len(clean_description) < 350,
            "source": source["platform"], "source_url": source["url"],
            "source_note": "Direct source fetch; geographic and candidate eligibility still require checks", "found_at": db.now()}


def collect(source, pages=2):
    """Return jobs, state, explanation. Partial coverage is never labelled complete."""
    kind, token = source["platform"], source["locator"]
    jobs, partial = [], False
    if kind == "greenhouse":
        data = _json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
        if not isinstance(data.get("jobs"), list):
            raise TypeError("Unrecognised Greenhouse response")
        jobs = [_job(source, j.get("title"), j.get("absolute_url"), (j.get("location") or {}).get("name", ""), j.get("content")) for j in data["jobs"]]
    elif kind == "lever":
        for page in range(pages):
            data = _json(f"https://api.lever.co/v0/postings/{token}?mode=json&limit=100&skip={page * 100}")
            if not isinstance(data, list):
                raise TypeError("Unrecognised Lever response")
            for j in data:
                desc = (j.get("descriptionPlain") or "") + " " + " ".join(str(x.get("content", "")) for x in j.get("lists", []))
                jobs.append(_job(source, j.get("text"), j.get("hostedUrl"), (j.get("categories") or {}).get("location", ""), desc))
            if len(data) < 100:
                break
        else:
            partial = True
    elif kind == "ashby":
        data = _json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
        if not isinstance(data.get("jobs"), list):
            raise TypeError("Unrecognised Ashby response")
        jobs = [_job(source, j.get("title"), j.get("jobUrl"), j.get("location", ""), j.get("descriptionPlain")) for j in data["jobs"]]
        for job, raw in zip(jobs, data["jobs"]):
            if job and raw.get("publishedAt"):
                job["posted_at"] = raw["publishedAt"]
    elif kind == "workday":
        tenant, shard, site = token.split("|")
        base = f"https://{tenant}.{shard}.myworkdayjobs.com"
        for page in range(pages):
            data = _json(f"{base}/wday/cxs/{tenant}/{site}/jobs", {"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": "operations"})
            if not isinstance(data.get("jobPostings"), list):
                raise TypeError("Unrecognised Workday response")
            for j in data["jobPostings"]:
                job = _job(source, j.get("title"), f"{base}/{site}{j.get('externalPath', '')}", j.get("locationsText", ""))
                if job and j.get("postedOn"):
                    job["posted_at"] = j["postedOn"]
                jobs.append(job)
            if len(jobs) >= int(data.get("total", 0)) or not data["jobPostings"]:
                break
        else:
            partial = True
        # Search is deliberately focused; this is not a complete employer census.
        partial = True
    elif kind == "goldman":
        query = "query GetRoles($searchQueryInput: RoleSearchQueryInput!) { roleSearch(searchQueryInput: $searchQueryInput) { totalCount items { roleId jobTitle externalSource { sourceId } locations { primary state country city } status } } }"
        for page in range(pages):
            data = _json("https://api-higher.gs.com/gateway/api/v1/graphql", {
                "query": query, "operationName": "GetRoles", "variables": {"searchQueryInput": {
                    "page": {"pageSize": 20, "pageNumber": page}, "filters": [],
                    "experiences": ["EARLY_CAREER", "PROFESSIONAL"], "searchTerm": "operations"}}})
            result = (data.get("data") or {}).get("roleSearch")
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                raise TypeError("Unrecognised Goldman response")
            for j in result["items"]:
                identifier = str((j.get("externalSource") or {}).get("sourceId") or j["roleId"].split("_")[0])
                locations = "; ".join(", ".join(x.get(k, "") or "" for k in ("city", "state", "country")) for x in j.get("locations", []))
                if j.get("status") == "POSTED":
                    jobs.append(_job(source, j.get("jobTitle"), f"https://higher.gs.com/roles/{quote(identifier, safe='')}", locations))
            if not result["items"]:
                break
        partial = True
    elif kind == "successfactors":
        base = f"https://{urlsplit(source['url']).netloc}"
        for page in range(pages):
            data = _json(base + "/services/recruiting/v1/jobs", {
                "locale": "en_GB", "pageNumber": page, "sortBy": "", "keywords": "operations",
                "location": "India", "facetFilters": {}, "brand": "", "skills": [],
                "categoryId": 0, "alertId": "", "rcmCandidateId": ""})
            if not isinstance(data.get("jobSearchResult"), list):
                raise TypeError("Unrecognised SuccessFactors response")
            for item in data["jobSearchResult"]:
                j = item["response"]
                url = base + f"/job/{j['unifiedUrlTitle']}/{j['id']}-en_GB"
                job = _job(source, j.get("unifiedStandardTitle"), url, ", ".join(j.get("jobLocationShort", [])))
                if job:
                    jobs.append(job)
            if not data["jobSearchResult"]:
                break
        partial = True
    else:
        soup = BeautifulSoup(fetch(source["url"]), "html.parser")
        if kind == "eightfold":
            embedded = soup.select_one("#smartApplyData")
            if embedded:
                data = json.loads(embedded.get_text())
                for j in data.get("positions", []):
                    url = j.get("canonicalPositionUrl") or urljoin(source["url"], f"/careers/job/{j['id']}")
                    jobs.append(_job(source, j.get("name"), url, j.get("location", ""), j.get("job_description")))
            partial = True
        else:
            from core.job_markup import postings
            partial = True
            records = postings(str(soup))
            browser_used = False
            if not records and source.get("_browser_fallback"):
                from core.browser_source import fetch as render
                records = postings(render(source["url"]))
                browser_used = True
            for j in records:
                location = j.get("jobLocation") or []
                location = location if isinstance(location, list) else [location]
                places = []
                for place in location:
                    address = place.get("address", {}) if isinstance(place, dict) else {}
                    if isinstance(address, dict):
                        places.append(", ".join(str(address[k]) for k in ("addressLocality", "addressRegion", "addressCountry") if address.get(k)))
                job = _job(source, j.get("title"), urljoin(source["url"], j.get("url") or "") if j.get("url") else None, "; ".join(places), j.get("description"))
                if job:
                    job.update({key: value for key, value in {
                        "posted_at": j.get("datePosted"),
                        "valid_through": j.get("validThrough"),
                        "work_mode": j.get("jobLocationType"),
                    }.items() if value})
                jobs.append(job)
            jobs = [job for job in jobs if job]
            for job in jobs:
                job["source_note"] += "; " + ("Playwright rendered" if browser_used else "Static") + " structured JobPosting"
            if not jobs:
                return [], "manual", ("Browser + static" if browser_used else "Static") + " page reached; no supported listing feed. Open official page; automated coverage not established."
    jobs = [j for j in jobs if j]
    if not jobs:
        return [], "empty", "Fetch returned no listings; not proof the employer has no vacancies."
    return jobs, "partial" if partial else "live", "Keyword or page-limited fetch" if partial else "Listings returned by board endpoint"


def scan_batch(conn, limit=1000, pages=2, persist=False, progress=None):
    seed_curated(conn)
    limit, pages = max(1, min(int(limit), 1000)), max(1, min(int(pages), 5))
    now = db.now()
    conn.execute("BEGIN IMMEDIATE")
    selected = [dict(r) for r in conn.execute("SELECT * FROM career_sources WHERE enabled=1 AND (next_check IS NULL OR next_check<=?) ORDER BY priority DESC, last_checked IS NOT NULL, last_checked, id LIMIT ?", (now, limit))]
    # A lease prevents an ordinary restart from immediately buying duplicate work.
    lease = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(timespec="seconds")
    conn.executemany("UPDATE career_sources SET next_check=? WHERE id=?", [(lease, s["id"]) for s in selected])
    conn.commit()
    # Only two generic pages can launch Chromium in one batch; ATS/HTTP
    # workers continue concurrently. Never escalate an HTTP block to a browser.
    browser_slots = 2
    for source in selected:
        if browser_slots and source["platform"] in {"career_page", "government"}:
            source["_browser_fallback"] = True
            browser_slots -= 1
    kept, outcomes = [], {}
    if progress:
        progress(0, len(selected), 0)

    def worker(source):
        try:
            return collect(source, pages)
        except requests.HTTPError as error:
            code = error.response.status_code if error.response is not None else 0
            return [], "blocked" if code in {401, 403, 429} else "error", f"HTTP {code}; no coverage claimed"
        except (ValueError, TypeError, KeyError, AttributeError, requests.RequestException, OSError) as error:
            return [], "error", f"{type(error).__name__}: {str(error)[:160]}"
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(worker, s): s for s in selected}
        for future in as_completed(futures):
            source = futures[future]
            jobs, state, detail = future.result()
            relevant = [j for j in jobs if ROLE.search(j["title"]) and not TECH.search(j["title"])]
            if persist:
                for job in relevant:
                    db.upsert_job(conn, job)
            kept.extend(relevant)
            failures = source["failures"] + 1 if state in {"error", "blocked"} else 0
            hours = min(168, 2 ** min(failures, 7)) if failures else (168 if state == "manual" else 24)
            due = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")
            conn.execute("UPDATE career_sources SET state=?, last_checked=?, next_check=?, failures=?, jobs_seen=?, relevant_seen=?, detail=? WHERE id=?", (state, db.now(), due, failures, len(jobs), len(relevant), detail, source["id"]))
            conn.commit()
            outcomes[state] = outcomes.get(state, 0) + 1
            if progress:
                progress(sum(outcomes.values()), len(selected), len(kept))
    return kept, [f"Career registry: checked {len(selected)} sources; {len(kept)} role-relevant listings worldwide", ", ".join(f"{n} {state}" for state, n in sorted(outcomes.items()))]
