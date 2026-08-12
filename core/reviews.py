"""
PHASE 5 — employer reviews, bias-corrected.

Source: AmbitionBox via Apify. AmbitionBox is India's largest employer
review platform and is owned by Naukri/InfoEdge, so its coverage maps
onto the listings this tool already pulls.

WHY BIAS CORRECTION IS NOT OPTIONAL

Employer reviews skew negative. People write them on the way out, angry;
people who are content write nothing. A raw 3.2 rendered next to a
five-star scale reads as "bad company" when it usually means "ordinary
company", and a job seeker who drops a good employer over that has been
harmed by the tool.

So every summary must:
  - compare to the sector median, never to an absolute scale
  - separate current from former employee sentiment
  - weight recent reviews higher
  - keep genuine red flags (salary delays, mass layoffs, litigation)
    in their own category, apart from ordinary grumbling
  - state the review count prominently — eight reviews is an anecdote

Cached in `companies` for 30 days. That table is shared across users
because it holds public data only, and caching it saves real API cost.

Everything here is best-effort. A failure returns None; it never breaks
a page or a scan.
"""

import json
import os
import urllib.error
import urllib.request

from core import db
from core.llm import call_llm

MAX_AGE_DAYS = 30
RATINGS_ACTOR = "thirdwatch/ambitionbox-scraper"
REVIEWS_ACTOR = "getdataforme/ambitionbox-reviews-scraper"
APIFY_RUN = ("https://api.apify.com/v2/acts/{actor}/"
             "run-sync-get-dataset-items?token={token}&timeout=180")


def available():
    return bool(os.environ.get("APIFY_TOKEN"))


def _run_actor(actor, payload):
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        return None
    url = APIFY_RUN.format(actor=actor.replace("/", "~"), token=token)
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read().decode())
        return data if isinstance(data, list) else None
    except (urllib.error.URLError, ValueError, OSError) as e:
        print(f"  [reviews] {actor} failed: {e}")
        return None


def fetch_ratings(company):
    """Headline rating, review count, and the star distribution."""
    items = _run_actor(RATINGS_ACTOR, {"companyName": company,
                                       "search": company, "maxItems": 1})
    if not items:
        return None

    row = items[0] if isinstance(items[0], dict) else {}
    breakdown = row.get("ratingBreakdown") or row.get("ratingDistribution") \
        or row.get("starDistribution") or {}

    return {
        "rating": _num(row.get("rating") or row.get("overallRating")),
        "review_count": _int(row.get("reviewCount")
                             or row.get("totalReviews")),
        "breakdown": breakdown if isinstance(breakdown, dict) else {},
        "categories": row.get("categoryRatings") or {},
    }


def fetch_reviews(company, limit=60):
    """Individual reviews. Pros, cons, and whether they still work there."""
    items = _run_actor(REVIEWS_ACTOR, {"companyName": company,
                                       "search": company, "maxItems": limit})
    if not items:
        return []

    out = []
    for it in items[:limit]:
        if not isinstance(it, dict):
            continue
        out.append({
            "title": it.get("title") or it.get("summary"),
            "pros": it.get("pros") or it.get("likes"),
            "cons": it.get("cons") or it.get("dislikes"),
            "rating": _num(it.get("rating")),
            "date": it.get("date") or it.get("reviewDate")
            or it.get("postedOn"),
            "employment": it.get("employmentStatus") or it.get("employeeType")
            or it.get("currentEmployee"),
            "role": it.get("designation") or it.get("jobTitle"),
        })
    return out


def _num(value):
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------
# The summary
# ------------------------------------------------------------
SUMMARY_SYSTEM = """Summarise employer reviews for a job seeker in India.

CORRECT FOR THE BIAS IN THE SOURCE. Employer review sites skew negative:
people write reviews on the way out, and content employees write nothing.
A 3.2 on AmbitionBox is usually an ordinary company, not a bad one.

Rules you must follow:

1. NEVER describe a rating on an absolute scale. Do not say "low" or
   "poor". Compare to the median for that company's sector in India and
   state the median you are comparing against. Estimate the sector median
   from what you know of Indian employer ratings in that industry.

2. Separate current from former employees. Former employees rate lower
   almost everywhere; say how the two groups differ here and whether the
   gap is bigger or smaller than usual.

3. Weight recent reviews more heavily. A complaint from four years ago
   about a manager who has left is not useful. Say so if the negative
   reviews cluster in the past.

4. Keep RED FLAGS separate from ordinary complaints. A red flag is
   salary paid late, mass layoffs, litigation, a legal or safety issue,
   or systematic misrepresentation at hiring. Long hours, slow
   promotions, bad canteen food and office politics are ordinary
   grumbling and belong in `concerns`, not `red_flags`. Most companies
   have zero red flags — return an empty list rather than reaching.

5. State the review count plainly. Under about 20 reviews is an
   anecdote, and `confidence` must be "low" in that case.

Give each theme a rough frequency in words ("mentioned in most negative
reviews", "a handful of reviews"). Do not invent percentages.

Return ONLY JSON:
{
  "rating": <float>,
  "review_count": <int>,
  "sector_median": <float you are comparing against>,
  "sector_context": "<the rating placed against its sector median, e.g.
                     'slightly above the median for that sector in India (3.2)'>",
  "strengths": ["themes with rough frequency"],
  "concerns": ["themes with rough frequency"],
  "red_flags": ["genuine ones only, or empty"],
  "current_vs_former": "<how sentiment differs between the two groups>",
  "confidence": "high|medium|low"
}"""


def summarize(company, ratings, reviews, sector=None):
    prompt = f"""COMPANY: {company}
SECTOR (may be blank — infer it if so): {sector or 'unknown'}

HEADLINE:
{json.dumps(ratings or {}, indent=2)[:2000]}

INDIVIDUAL REVIEWS ({len(reviews)} of them):
{json.dumps(reviews, indent=2)[:12000]}"""

    return call_llm(SUMMARY_SYSTEM, prompt, max_tokens=1800, as_json=True)


# ------------------------------------------------------------
# Orchestration
# ------------------------------------------------------------
def enrich(conn, company, sector=None, force=False, dry=False):
    """
    Fill in one company's review data, using the cache when it is fresh.

    Returns the stored row, or None if nothing could be fetched. Never
    raises — a review lookup failing must not break a detail page.
    """
    if not company:
        return None
    if not force and db.company_is_fresh(conn, company, MAX_AGE_DAYS):
        return db.get_company(conn, company)
    if dry or not available():
        return db.get_company(conn, company)

    try:
        ratings = fetch_ratings(company)
        reviews = fetch_reviews(company)
        if not ratings and not reviews:
            return db.get_company(conn, company)

        summary = summarize(company, ratings, reviews, sector)

        db.save_company(
            conn, company,
            rating=(summary or {}).get("rating") or (ratings or {}).get("rating"),
            review_count=(summary or {}).get("review_count")
            or (ratings or {}).get("review_count"),
            rating_breakdown=(ratings or {}).get("breakdown") or {},
            review_summary=summary,
            sector_median_rating=(summary or {}).get("sector_median"))
        return db.get_company(conn, company)

    except Exception as e:
        print(f"  [reviews] {company}: {e}")
        return db.get_company(conn, company)


def enrich_from_listing(conn, job):
    """
    Cheap path: the Naukri actor attaches an AmbitionBox rating to each
    listing. Store it so a company has a number even if the full review
    fetch never runs — but do NOT let it overwrite a real summary.
    """
    name = job.get("company")
    rating = job.get("company_rating")
    if not name or rating is None:
        return
    existing = db.get_company(conn, name)
    if existing and existing.get("review_summary"):
        return
    db.save_company(conn, name, rating=_num(rating),
                    review_count=_int(job.get("company_review_count")))


def enrich_top_companies(conn, jobs, limit=10, dry=False):
    """
    After a scan, enrich the companies behind the strongest listings.

    Bounded, because each company is two Apify calls plus an LLM call and
    the user is only going to open a handful of these. The shared cache
    means the second user chasing the same employer pays nothing.
    """
    if dry:
        return 0

    seen, done = set(), 0
    for job in jobs:
        name = job.get("company")
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        if db.company_is_fresh(conn, name, MAX_AGE_DAYS):
            continue
        if enrich(conn, name, dry=dry):
            done += 1
        if done >= limit:
            break
    return done
