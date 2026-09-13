"""
LAYER 1 — DISCOVERY
Pulls raw listings from Indian aggregators + direct ATS boards.
Returns a flat list of dicts. No scoring here.

The fetchers themselves live in sources_free.py and sources_apify.py.
This file is the orchestrator: it decides which tiers run, in what
order, under what budget, and collapses the duplicates they produce.
Every fetcher is re-exported below, so `discover.from_greenhouse` and
friends resolve exactly as they always did.
"""

import re
import urllib.request                      # noqa: F401  (see below)
from concurrent.futures import ThreadPoolExecutor, as_completed

# urllib.request is imported but never called here. It stays because the
# tests silence the network with
#     monkeypatch.setattr(discover.urllib.request, "urlopen", ...)
# which reaches the shared stdlib module THROUGH this name. Drop the
# import and those tests stop patching anything and start making real
# HTTP calls, which is the one failure mode a test suite must not have.

from core import firecrawl
from core.sources_free import (           # noqa: F401  (re-exported)
    _clean, _get_json, _now, from_ashby, from_greenhouse, from_instahyre,
    from_jobspy, from_lever, from_smartrecruiters, from_workable,
    from_workday)
from core.sources_apify import (          # noqa: F401  (re-exported)
    APIFY_RUN, BACKFILL_KEY, LINKEDIN_BACKFILL_KEY, affordable_terms,
    apify_available, estimate_cost, from_apify, from_linkedin,
    linkedin_cost, term_cost)


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
            + [(from_ashby, s) for s in ats.get("ashby", []) or []]
            + [(from_smartrecruiters, s)
               for s in ats.get("smartrecruiters", []) or []]
            + [(from_workable, s) for s in ats.get("workable", []) or []]
            + [(from_workday, s) for s in ats.get("workday", []) or []])
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
# Duplicates
# ------------------------------------------------------------
_PUNCT = re.compile(r"[^a-z0-9 ]")
_BRACKET = re.compile(r"\(.*?\)|\[.*?\]")


def _key(job):
    """Title, company AND location, normalised.

    Location is in the key deliberately. Measured on the live corpus,
    31 groups shared a title and company; most were one role genuinely
    open in two cities —

        Group Product Manager  mindtickle  Pune / Bengaluru

    — and collapsing those would hide a job in the city the user
    actually wants. Only an exact match on all three is a real
    duplicate, and those exist too: Paytm had three reqs posted twice
    on the same Lever board, same title, same city, different id.
    """
    bits = []
    for field in ("title", "company", "location"):
        v = _BRACKET.sub(" ", (job.get(field) or "").lower())
        bits.append(re.sub(r"\s+", " ", _PUNCT.sub(" ", v)).strip())
    return tuple(bits)


def collapse_duplicates(rows):
    """Keep one row per (title, company, location); return (rows, dropped).

    The survivor is the one with the longest description, because that
    is what the scorer reads — an Indeed copy with three paragraphs is
    better raw material than the bare ATS row for the same job. A row
    missing a title or company is never collapsed; the key would be
    mostly empty and would swallow unrelated listings.
    """
    best, order = {}, []
    for job in rows:
        key = _key(job)
        if not key[0] or not key[1]:
            order.append(job)
            continue
        seen = best.get(key)
        if seen is None:
            best[key] = job
            order.append(job)
        elif len(job.get("description") or "") > len(seen.get("description") or ""):
            order[order.index(seen)] = job
            best[key] = job
    return order, len(rows) - len(order)


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

    # Tier 1b: Instahyre — free, no key, and India-specific, which the
    # ATS boards are not. Reaches employers listed nowhere in the config.
    insta = sources.get("instahyre", {})
    # Explicit opt-in, like every other tier here. Defaulting this to ON
    # when the key is simply absent meant any caller passing a partial
    # sources dict reached out to the internet without asking — a unit
    # test caught it by fetching 280 real listings mid-suite. The shipped
    # config turns it on; a config that never mentions it does not.
    if insta.get("enabled") is True:
        before = len(rows)
        rows += from_instahyre(int(insta.get("max_pages", 8)))
        notes.append(f"Instahyre: {len(rows) - before} listings")

    apify_cfg = sources.get("apify", {})
    terms = sources.get("jobspy", {}).get("search_terms", [])
    locs = sources.get("jobspy", {}).get("locations", [])

    # Every keyword-driven tier below is gated on `terms`. With none,
    # they all quietly declined to run and the scan reported an ordinary
    # ATS-only result — a scan that covered a fraction of the market and
    # said nothing about it.
    if not terms:
        notes.append("no search terms on any profile — keyword sources "
                     "(Naukri, LinkedIn, Indeed, open web) all skipped. "
                     "Run `run.py calibrate`.")

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

    # One budget, shared. Two paid tiers each policing their own ceiling
    # means the scan can quietly cost twice what the ceiling says.
    spent = 0.0

    li_cfg = sources.get("linkedin", {})
    if li_cfg.get("enabled") and terms and not dry:
        if apify_available():
            li_terms = li_cfg.get("search_terms") or terms
            seeding = not (li_cfg.get("backfilled_terms") or [])
            cost = linkedin_cost(li_cfg, seeding)
            budget = float(li_cfg.get("max_spend_per_scan_usd") or 0) or None
            if budget and cost > budget:
                note = (f"LinkedIn skipped — one run costs ${cost:.2f}, over "
                        f"the ${budget:.2f} ceiling")
                print(f"  [linkedin] {note}")
                notes.append(note)
            else:
                print(f"→ Tier 2.7: LinkedIn via Apify "
                      f"({'first deep run' if seeding else 'daily'}, "
                      f"{len(li_terms)} keywords, ~${cost:.2f})...")
                before = len(rows)
                before = len(rows)
                rows += from_linkedin(li_cfg, li_terms, locs,
                                      sources.get("linkedin_filters"), seeding)
                spent += cost
                # Only a run that actually came back counts as seeded.
                # Marking them regardless meant a network error during
                # the one deep pass lost it permanently: every later run
                # then asked a 24-hour window for a keyword whose 30-day
                # history had never been fetched. Naukri gets this right
                # by recording inside its loop, after the response.
                if len(rows) > before:
                    li_cfg["_seeded_terms"] = list(li_terms)
                notes.append(f"LinkedIn: {len(rows) - before} listings, "
                             f"~${cost:.2f}")
        else:
            notes.append("APIFY_TOKEN not set — LinkedIn skipped")

    naukri_ok = False
    if apify_cfg.get("enabled") and terms and not dry:
        if apify_available():
            # The paid tier searches a rotating slice, not everything —
            # the scheduler puts this scan's share on the config. See
            # core/scheduler.py: _rotate_terms.
            seeded = apify_cfg.get("backfilled_terms") or []
            # Whatever LinkedIn already took comes off this tier's
            # allowance, so the two together respect one number.
            local = dict(apify_cfg)
            if local.get("max_spend_per_scan_usd"):
                local["max_spend_per_scan_usd"] = max(
                    0.0, float(local["max_spend_per_scan_usd"]) - spent)
            paid_terms, trim_note = affordable_terms(
                local, apify_cfg.get("search_terms") or terms, seeded)
            if trim_note:
                print(f"  [apify] {trim_note}")
                notes.append(trim_note)
            if paid_terms:
                estimate = estimate_cost(apify_cfg, paid_terms, seeded)
                new = [t for t in paid_terms if t not in set(seeded)]
                shape = (f"{len(new)} first-time" if len(new) == len(paid_terms)
                         else f"{len(new)} first-time, "
                              f"{len(paid_terms) - len(new)} daily"
                         if new else f"{len(paid_terms)} daily")
                print(f"→ Tier 3: Naukri via Apify "
                      f"({shape}, ~${estimate:.2f})...")
                before = len(rows)
                rows += from_apify(apify_cfg, paid_terms, locs,
                                   sources.get("naukri_filters"))
                naukri_ok = len(rows) > before
                notes.append(f"Naukri via Apify: {len(rows) - before} "
                             f"listings, ~${estimate:.2f}")
        else:
            notes.append("APIFY_TOKEN not set — Naukri skipped, "
                         "running on free sources only")
    elif dry:
        notes.append("dry run — paid sources skipped")

    if sources.get("jobspy", {}).get("enabled") and terms:
        print("→ Tier 2: JobSpy (LinkedIn/Indeed)...")
        before = len(rows)
        cfg = dict(sources["jobspy"])
        if (not naukri_ok and cfg.get("try_naukri_free")
                and "naukri" not in cfg.get("sites", [])):
            # The free Naukri fallback, now opt-in and off by default.
            # It was written on the theory that only cloud IPs get
            # blocked; measured from a residential Indian IP it still
            # returns 406 "recaptcha required", so leaving it on spent
            # ~1.3s per search being refused on every single scan.
            cfg["sites"] = list(cfg.get("sites", [])) + ["naukri"]
        rows += from_jobspy(cfg)
        notes.append(f"JobSpy: {len(rows) - before} listings")

    rows, dropped = collapse_duplicates(rows)
    if dropped:
        print(f"  [dedupe] {dropped} duplicate postings collapsed")
    print(f"→ {len(rows)} raw listings found")
    return rows, notes
