"""
THE PAID TIER — Apify, and the spend ceiling that keeps it honest.

Naukri is the only thing here that cannot be had free: measured from a
residential Indian IP it still answers scrapers with 406 "recaptcha
required". LinkedIn is also here because the actor bills a 150-result
floor per run, which forces a different shape from the free path — one
run per scan, never one per term.

Everything is pay-per-event, so this module carries its own cost model:
term_cost, estimate_cost and affordable_terms decide what is affordable
BEFORE any money is spent.
"""

import json
import os
import re
import urllib.error
import urllib.request

from core.sources_free import _now


# ------------------------------------------------------------
# C. Apify — the PRIMARY Naukri path
#
# Naukri's bot protection changes monthly and returned 406 recaptcha
# from a cloud IP during testing. A maintained actor at ~$7/month beats
# a free scraper that silently returns zero rows on a Tuesday morning.
# ------------------------------------------------------------
APIFY_RUN = ("https://api.apify.com/v2/acts/{actor}/"
             "run-sync-get-dataset-items?token={token}&timeout=300")

# Naukri filters by numeric city id, not by name — passing "Mumbai"
# fails the whole run. Only the metros this tool actually targets are
# mapped; an unrecognised location is dropped rather than guessed at,
# which searches all of India instead of the wrong city.
_NAUKRI_CITY_IDS = {
    "mumbai": "134", "navi mumbai": "138", "thane": "135",
    "bengaluru": "97", "bangalore": "97",
    "delhi": "382", "new delhi": "6", "noida": "220", "gurgaon": "122",
    "hyderabad": "17", "pune": "139", "chennai": "183",
    "kolkata": "232", "ahmedabad": "51",
}


def _naukri_cities(locations):
    out = []
    for loc in locations or []:
        key = str(loc).split(",")[0].strip().lower()
        city_id = _NAUKRI_CITY_IDS.get(key)
        if city_id and city_id not in out:
            out.append(city_id)
    return out


_TAG = re.compile(r"<[^>]+>")


def _from_naukri_item(item):
    """
    Map one dataset row to the shape the rest of the pipeline expects.

    The actor nests the job under `jobDetails` rather than returning it
    flat, so a top-level field lookup silently dropped every row — 50
    listings in, 0 out, no error. Descriptions come back as HTML and are
    stripped here; the scorer reads them as text.
    """
    if not isinstance(item, dict):
        return None
    job = item.get("jobDetails") if isinstance(item.get("jobDetails"), dict) \
        else item

    url = job.get("staticUrl") or job.get("jdURL")
    title = job.get("title") or job.get("jobRole")
    if not url or not title:
        return None

    company = (job.get("companyDetail") or {}).get("name") \
        or job.get("staticCompanyName")
    locations = job.get("locations") or []
    location = ", ".join(
        str(l.get("label") or l.get("name") or l) if isinstance(l, dict)
        else str(l) for l in locations[:3]) or None

    description = _TAG.sub(" ", job.get("description") or "")
    description = " ".join(description.split())
    if not description:
        description = job.get("shortDescription") or ""

    ab = job.get("ambitionBoxData") or {}
    return {
        "title": title,
        "company": company,
        "location": location,
        "url": url,
        "description": description[:4000],
        "source": "naukri",
        # This actor attaches AmbitionBox ratings to the listing, which
        # is why it is preferred — Naukri and the review data in one
        # call. See core/reviews.py.
        "company_rating": ab.get("AggregateRating") or ab.get("rating"),
        "company_review_count": ab.get("ReviewsCount")
        or ab.get("reviewsCount"),
        "found_at": _now(),
    }


def apify_available():
    return bool(os.environ.get("APIFY_TOKEN"))


def term_cost(cfg, term, backfilled=()):
    """
    What one search costs, which depends on whether it is the deep first
    pass for this keyword or an ordinary daily one.
    """
    prices = cfg.get("prices") or {}
    init = float(prices.get("run_init_usd", 0.01))
    listing = float(prices.get("standard_job_usd", 0.005))
    detail = float(prices.get("detailed_job_usd", 0.01))
    first_time = term not in set(backfilled or ())
    per_run = int(cfg.get("backfill_results_wanted", 50) if first_time
                  else cfg.get("results_wanted", 20))
    unit = listing + (detail if cfg.get("fetch_details") else 0.0)
    return init + per_run * unit


def estimate_cost(cfg, terms, backfilled=()):
    """
    What this tier is about to cost, before it costs it.

    Accepts a term list or a plain count — a count is treated as that
    many ordinary daily searches, which is what the ceiling tests and
    the old call sites mean by it.

    The unit prices live in config because they are the actor's, not
    ours, and they change. Getting this wrong is how a $5 credit
    disappeared a quarter of the way into a single scan without one line
    of output saying so.
    """
    if isinstance(terms, int):
        seen = ["_"] * terms
        return sum(term_cost(cfg, t, seen) for t in seen)
    return sum(term_cost(cfg, t, backfilled) for t in terms)


def affordable_terms(cfg, terms, backfilled=()):
    """
    Trim the term list to what the per-scan ceiling allows.

    Returns (terms, note). A ceiling of 0 or None means no limit, which
    is the old behaviour and is how this went wrong.

    The trim matters most on the first few scans, when every keyword is
    still owed its deep seeding pass and each one costs several times a
    daily search. Rather than refuse, it takes as many as it can afford
    and leaves the rest for the next scan — the rotation brings them
    back around.
    """
    ceiling = cfg.get("max_spend_per_scan_usd")
    if not ceiling or not terms:
        return terms, None
    ceiling = float(ceiling)

    affordable, running = [], 0.0
    for term in terms:
        cost = term_cost(cfg, term, backfilled)
        if running + cost > ceiling:
            break
        affordable.append(term)
        running += cost

    if len(affordable) == len(terms):
        return affordable, None
    if not affordable:
        return [], (f"Naukri skipped — the next search alone would cost "
                    f"${term_cost(cfg, terms[0], backfilled):.2f}, over the "
                    f"${ceiling:.2f} per-scan ceiling")
    return affordable, (
        f"Naukri trimmed to {len(affordable)} of {len(terms)} searches to "
        f"stay under the ${ceiling:.2f} ceiling; the rest come next scan")


BACKFILL_KEY = "naukri_backfilled_terms"


def _naukri_payload(cfg, term, locations, filters, backfilled=()):
    """
    One search, told as precisely as Naukri will let us.

    Every filter here is applied on THEIR side, and the actor bills per
    result it hands back — so a filter is not a nicety, it is the
    cheapest line of code in this file. Filtering after the fact means
    paying for everything you then throw away, which is what the 97%
    prefilter rejection rate was.

    Note what is deliberately NOT filtered: experience and salary. This
    tool scores stretch roles ABOVE safe ones and weights experience_fit
    under 10% on purpose. Filtering to the candidate's current level
    would exclude exactly the roles it exists to find, and filtering on
    salary drops every listing that says "not disclosed", which on
    Naukri is most of the good ones.
    """
    filters = filters or {}

    # SEED ONCE, THEN DELTAS.
    #
    # A keyword this account has never searched has no history in the
    # pool, so the first run for it opens the window wide and takes a
    # deep slice — everything still live, not just today. Every run
    # after that asks only for what was posted since, because dedup
    # happens AFTER billing: a 7-day window scanned daily buys the same
    # listing seven times and throws six away.
    #
    # The deep pass is the expensive one, so it is per keyword and it
    # happens once. The rotation and the spend ceiling between them
    # spread it over however many scans it takes to afford.
    first_time = term not in set(backfilled or ())
    payload = {
        "keyword": term,
        "maxJobs": int(cfg.get("backfill_results_wanted", 50) if first_time
                       else cfg.get("results_wanted", 20)),
        # Phase 1 buys titles, not descriptions. The full text costs
        # triple and the shortlister only reads the title anyway; see
        # core/semantic.py. Descriptions are bought later, for survivors.
        "fetchDetails": bool(cfg.get("fetch_details", False)),
        "freshness": str(cfg.get("backfill_freshness", "30") if first_time
                         else cfg.get("freshness", "1")),
        "sortBy": "date",
    }

    cities = _naukri_cities(locations)
    if cities:
        payload["cities"] = cities

    # Consultant postings are largely duplicated and reposted bait.
    # Excluding them is free and loses nothing.
    if cfg.get("company_jobs_only", True):
        payload["postedBy"] = ["1"]

    # These come from the user's own compiled profile — see
    # config/sources.yml for how to override them by hand.
    for ours, theirs in (("department", "department"),
                         ("role_category", "roleCategory"),
                         ("work_mode", "workMode"),
                         ("industry", "industry"),
                         ("company_type", "companyType")):
        value = filters.get(ours)
        if value:
            payload[theirs] = [str(v) for v in value]
    return payload


def from_apify(cfg, search_terms, locations, filters=None):
    """Naukri via a maintained actor. Returns [] and says why on failure."""
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        return []

    actor = (cfg.get("naukri_actor") or "").replace("/", "~")
    if not actor:
        print("  [apify] no naukri_actor configured — skipped")
        return []
    url = APIFY_RUN.format(actor=actor, token=token)
    backfilled = set(cfg.get("backfilled_terms") or ())
    # Terms whose deep first pass completed in THIS run, so the caller
    # can remember them and never pay for a deep pass on them again.
    cfg["_seeded_terms"] = []
    rows = []

    for term in search_terms:
        payload = _naukri_payload(cfg, term, locations, filters, backfilled)
        seeding = term not in backfilled
        if seeding:
            print(f"  [apify] seeding '{term}' — first search for this "
                  f"keyword, {payload['maxJobs']} jobs back "
                  f"{payload['freshness']} days")
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                items = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            # The body carries which field the actor rejected. Without it
            # a schema mistake looks identical to the actor being down.
            detail = ""
            try:
                detail = e.read().decode()[:300]
            except Exception:                        # noqa: BLE001
                pass
            print(f"  [apify] '{term}' HTTP {e.code}: {detail}")
            continue
        except (urllib.error.URLError, ValueError, OSError) as e:
            print(f"  [apify] '{term}' failed: {e}")
            continue

        # The run came back, so this keyword is seeded — every future
        # scan asks it only for what is new. Recorded even when the
        # actor returned nothing, because "nothing live for this
        # keyword" is a real answer and re-paying for it deeply every
        # day is the exact waste this exists to stop.
        if seeding:
            cfg["_seeded_terms"].append(term)

        for it in items if isinstance(items, list) else []:
            row = _from_naukri_item(it)
            if row:
                rows.append(row)

    print(f"  [apify] {len(rows)} listings from Naukri")
    return rows


# ------------------------------------------------------------
# C2. LinkedIn via Apify
#
# Worth having despite the ATS boards covering much of the same ground:
# LinkedIn postings carry the recruiter, the applicant count, and
# Easy Apply, and some employers post there and nowhere else.
#
# THE COST SHAPE IS THE OPPOSITE OF NAUKRI'S, and getting this backwards
# is expensive. Naukri bills purely per result, so narrow searches are
# cheap and you want many of them. This actor bills per result too, but
# with a floor: "Minimum is 150 results when using pay-per-result
# billing". One run costs at least 150 results whether you wanted 150 or
# 20 — so eighteen narrow keyword runs would cost eighteen floors.
#
# Hence: ONE run per scan carrying every keyword. The actor pairs each
# keyword with each location internally, so a single run and a single
# floor covers the lot, and the free semantic shortlister does the
# precision work afterwards.
# ------------------------------------------------------------
LINKEDIN_BACKFILL_KEY = "linkedin_backfilled_terms"

# LinkedIn's own windows, in the actor's spelling.
_LI_WINDOW = {"1": "r86400", "7": "r604800", "30": "r2592000"}

# Filters LinkedIn applies at the source, so non-matching jobs are never
# returned and never billed. Everything else the actor offers —
# jobFunction, industry, company size, agency exclusion — is applied
# AFTER collection: the docs say plainly "jobs are still collected and
# counted toward billing". Those are quality filters, not savings, and
# putting them in the same config block without saying so is how someone
# later assumes they are saving money.
_LI_FREE_FILTERS = ("jobType", "workType", "filterUnder10Applicants",
                    "filterEasyApply", "companyInclude")


def linkedin_cost(cfg, seeding):
    prices = cfg.get("prices") or {}
    start = float(prices.get("run_start_usd", 0.005))
    per = float(prices.get("result_usd", 0.0007))
    items = int(cfg.get("backfill_max_items", 500) if seeding
                else cfg.get("max_items", 150))
    # The floor is charged whether or not that many jobs come back.
    return start + max(items, int(cfg.get("billing_floor", 150))) * per


def _linkedin_payload(cfg, terms, locations, filters, seeding):
    filters = filters or {}
    window = "30" if seeding else str(cfg.get("freshness", "1"))
    payload = {
        "keyword": list(terms),
        "locations": [l for l in (locations or []) if l],
        "publishedAt": _LI_WINDOW.get(window, _LI_WINDOW["1"]),
        "maxItems": int(cfg.get("backfill_max_items", 500) if seeding
                        else cfg.get("max_items", 150)),
        # The actor's own dedup. Cheaper than ours because it happens
        # before the row is written and billed.
        "saveOnlyUniqueItems": True,
        # Extra company-page requests per job. Off: the scorer never
        # reads them and the shortlister only wants the title.
        "enrichCompanyData": False,
    }

    # Deliberately absent, for the same reason as Naukri: experienceLevel
    # and salaryBase. This tool ranks stretch roles above safe ones, so
    # filtering to someone's current level deletes what it exists to
    # find — and LinkedIn's salary filter is documented as unreliable
    # under their new AI search anyway.
    for key in _LI_FREE_FILTERS:
        if filters.get(key) not in (None, "", [], False):
            payload[key] = filters[key]

    # Applied after collection, so these cost nothing to add and save
    # nothing either. They exist to keep junk off the board.
    for key in ("jobFunctionInclude", "jobIndustryInclude",
                "excludeRecruitingAgencies", "jobTitleExclude",
                "companyExclude", "subLocationExclude"):
        if filters.get(key) not in (None, "", [], False):
            payload[key] = filters[key]
    return payload


def _from_linkedin_item(item):
    if not isinstance(item, dict):
        return None
    url = (item.get("link") or item.get("url") or item.get("jobUrl")
           or item.get("applyUrl"))
    title = item.get("title") or item.get("jobTitle")
    if not url or not title:
        return None

    company = item.get("companyName") or item.get("company")
    if isinstance(company, dict):
        company = company.get("name")
    location = item.get("location") or item.get("jobLocation")
    if isinstance(location, dict):
        location = location.get("name") or location.get("city")

    description = _TAG.sub(" ", str(item.get("descriptionText")
                                    or item.get("description") or ""))
    return {
        "title": title,
        "company": company,
        "location": location,
        "url": url,
        "description": " ".join(description.split())[:4000],
        "source": "linkedin",
        # Only present on LinkedIn, and genuinely useful when deciding
        # where to spend an afternoon: a role with nine applicants is a
        # different proposition from one with nine hundred.
        "applicants": item.get("applicantsCount") or item.get("applicants"),
        "easy_apply": item.get("easyApply"),
        "found_at": _now(),
    }


def from_linkedin(cfg, search_terms, locations, filters=None, seeding=False):
    """One run, every keyword. Returns [] and says why on failure."""
    token = os.environ.get("APIFY_TOKEN")
    if not token or not search_terms:
        return []

    actor = (cfg.get("actor") or "").replace("/", "~")
    if not actor:
        print("  [linkedin] no actor configured — skipped")
        return []

    payload = _linkedin_payload(cfg, search_terms, locations, filters, seeding)
    req = urllib.request.Request(
        APIFY_RUN.format(actor=actor, token=token),
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            items = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:                            # noqa: BLE001
            pass
        print(f"  [linkedin] HTTP {e.code}: {detail}")
        return []
    except (urllib.error.URLError, ValueError, OSError) as e:
        print(f"  [linkedin] failed: {e}")
        return []

    rows = []
    for it in items if isinstance(items, list) else []:
        # Filters the actor applies after collection mark the row rather
        # than dropping it. Honour the mark; we already paid either way.
        if it.get("dynamicFilterMatch") is False:
            continue
        row = _from_linkedin_item(it)
        if row:
            rows.append(row)

    print(f"  [linkedin] {len(rows)} listings from {len(search_terms)} "
          f"keyword(s) in one run")
    return rows
