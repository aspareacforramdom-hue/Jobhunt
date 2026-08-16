"""
COMPANY DISCOVERY — who to watch, worked out rather than typed in.

THE PROBLEM THIS SOLVES

`config/sources.yml` holds a hand-written list of company slugs. Someone
typed seventeen of them once. Every user gets the same seventeen, so the
data engineer and the brand marketer watch an identical set of employers
that matches neither of them particularly well. Adding a company means
editing YAML, which means it never happens.

HOW IT WORKS

Two steps, and only the first one costs anything:

  1. One LLM call turns a person's compiled profile — target role,
     industries, cities, the kind of company they said they want — into
     a list of employer names. One call, per user, per month.

  2. Every name is probed against the five free, no-auth ATS APIs. This
     is the part that makes the feature practical: it costs nothing, it
     is exact rather than a guess, and it runs in parallel. A company
     either answers with live jobs or it does not.

Measured on eighteen Indian tech employers, probing found:

    swiggy, freshworks   -> smartrecruiters
    postman, groww, phonepe -> greenhouse
    cred, meesho, zeta   -> lever
    atlan, navi          -> ashby

Eight of eighteen, no key, no cost. Swiggy is the instructive one: it was
already in the config as a PAID Firecrawl career-page scrape returning
zero listings on every run, while publishing its whole board on a free
API nobody had checked.

WHAT IT DOES NOT DO

It does not run during a scan. Discovery is a monthly command, because
the answer changes on the timescale of a company adopting a new ATS.
"""

import json
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from core import db
from core.llm import call_llm

PROBE_WORKERS = 12

# The five platforms with public, no-auth job APIs. The probe asks each
# one whether this slug is a real board with live roles on it.
PROBES = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "smartrecruiters":
        "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
    "workable": "https://www.workable.com/api/accounts/{slug}?details=true",
}

# Legal suffixes and filler that never appear in a board slug.
_NOISE = re.compile(
    r"\b(private|pvt|limited|ltd|inc|incorporated|corp|corporation|llp|"
    r"plc|technologies|technology|labs|india|group|holdings|company)\b",
    re.I)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slug_variants(name):
    """
    The two or three spellings a board slug is actually likely to use.

    Deliberately few. Each extra variant multiplies the probe count by
    the number of platforms, and the marginal ones almost never hit —
    "tataconsumerproductslimited" is not anybody's board name.
    """
    base = _NOISE.sub(" ", str(name or "").lower())
    words = [w for w in _NON_ALNUM.split(base) if w]
    if not words:
        return []

    joined = "".join(words)
    out = [joined]
    if len(words) > 1:
        out.append("-".join(words))
        # Many Indian firms are known by their first word alone
        # (Swiggy Technologies -> swiggy).
        if len(words[0]) > 3:
            out.append(words[0])
    seen, unique = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            unique.append(s)
    return unique[:3]


def _count(platform, payload):
    """How many live roles this board is advertising, or 0."""
    if platform == "smartrecruiters":
        return int(payload.get("totalFound") or 0) \
            if isinstance(payload, dict) else 0
    if platform == "lever":
        return len(payload) if isinstance(payload, list) else 0
    if not isinstance(payload, dict):
        return 0
    return len(payload.get("jobs") or [])


def probe_slug(slug, timeout=12):
    """
    Ask all five platforms about one slug. Returns [(platform, jobs)].

    Free, so the only budget being spent is time. Errors are answers
    here — a 404 means "not this platform", which is exactly what we
    wanted to know.
    """
    found = []
    for platform, template in PROBES.items():
        try:
            req = urllib.request.Request(
                template.format(slug=slug),
                headers={"User-Agent": "careerloop/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read().decode())
        except (urllib.error.URLError, ValueError, OSError):
            continue
        jobs = _count(platform, payload)
        if jobs:
            found.append((platform, jobs))
    return found


def resolve(names, workers=PROBE_WORKERS, timeout=12):
    """
    Company names in, confirmed boards out.

    Returns [{name, platform, slug, jobs}], one entry per board that
    answered with live roles. A company on two platforms yields two
    entries; that happens, and both are worth fetching.
    """
    tasks = [(name, slug) for name in names for slug in slug_variants(name)]
    if not tasks:
        return []

    results, claimed = [], set()
    with ThreadPoolExecutor(max_workers=min(workers, len(tasks))) as pool:
        probed = pool.map(lambda t: (t, probe_slug(t[1], timeout)), tasks)
        for (name, slug), hits in probed:
            for platform, jobs in hits:
                key = (platform, slug)
                if key in claimed:
                    continue
                claimed.add(key)
                results.append({"name": name, "platform": platform,
                                "slug": slug, "jobs": jobs})
    results.sort(key=lambda r: -r["jobs"])
    return results


# ------------------------------------------------------------
# Where the candidate names come from
# ------------------------------------------------------------
SYSTEM = """You name employers a specific candidate should be watching.

Return ONLY a JSON array of company names, no other text.

Rules:
- Real, currently operating employers. No invented names.
- Match the candidate's target ROLE, INDUSTRIES and CITIES. A company
  that does not hire for their function is wasted.
- Favour employers that actually publish jobs on a public careers page.
- Use the name the company is commonly known by, not its legal name:
  "Swiggy", not "Bundl Technologies Private Limited". This matters —
  the name is turned into a job-board address.
- Spread across sizes: some large, some mid, some early-stage. Do not
  return only the five most famous names in the country.
- No staffing agencies, consultancies-for-hire, or job boards."""


def candidates_from_profile(profile, limit=50):
    """
    Ask for employer names matching this person. One LLM call.

    Falls back to nothing rather than guessing: a bad list wastes probe
    time and, worse, fills someone's board with roles from companies
    that were never a fit.
    """
    target = (profile or {}).get("target") or {}
    hard = (profile or {}).get("hard_filters") or {}
    brief = {
        "role_family": target.get("role_family"),
        "alt_titles": (target.get("alt_titles") or [])[:6],
        "seniority": target.get("seniority"),
        "industries": (target.get("industries") or [])[:6],
        "cities": (hard.get("locations") or [])[:6],
    }
    if not brief["role_family"] and not brief["alt_titles"]:
        return []

    prompt = (f"CANDIDATE:\n{json.dumps(brief, indent=2)}\n\n"
              f"Name up to {limit} employers this person should watch.")
    try:
        names = call_llm(SYSTEM, prompt, max_tokens=1500, as_json=True)
    except Exception as e:                                  # noqa: BLE001
        print(f"  [companies] could not generate candidates: {e}")
        return []

    if not isinstance(names, list):
        return []
    return [str(n).strip() for n in names
            if isinstance(n, (str, int)) and str(n).strip()][:limit]


# ------------------------------------------------------------
# Orchestration
# ------------------------------------------------------------
def discover_for_user(conn, user_id, profile, limit=50, extra_names=()):
    """
    Find and record the boards worth watching for one person.

    Boards are stored globally, not per user: the scrape is shared, so a
    company one person's profile surfaced is fetched once and scored for
    everybody. `found_for` records who it came from, which is the only
    part that is really per-user.
    """
    names = list(extra_names) or candidates_from_profile(profile, limit)
    if not names:
        print("  [companies] no candidate employers to probe")
        return []

    print(f"  [companies] probing {len(names)} employers across "
          f"{len(PROBES)} platforms (free)...")
    boards = resolve(names)
    for b in boards:
        db.save_job_board(conn, b["platform"], b["slug"], b["name"],
                          b["jobs"], user_id)
    print(f"  [companies] {len(boards)} live boards found")
    return boards
