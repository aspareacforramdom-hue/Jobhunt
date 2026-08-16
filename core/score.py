"""
LAYER 3 — SCORING
The core differentiator: scores against WHERE YOU'RE GOING,
not where you've been. experience_fit is weighted under 10%.

The dimensions are not fixed. They come from the user's own compiled
profile — three universal ones plus whatever their intake generated —
so two people chasing different careers are scored on different things
and neither inherits the other's vocabulary. See core/dimensions.py.

Batches jobs into one LLM call to keep cost near-zero.
"""

import json
import math

from core import dimensions
from core.llm import call_llm_usage

SYSTEM = """You score job listings for a candidate moving INTO a new field.

CRITICAL: Score against the candidate's TARGET trajectory, not their
current resume. A stretch role they can plausibly grow into scores
HIGHER than a safe role that keeps them where they are.

You will be given the exact scoring dimensions for THIS candidate, each
with a definition. Score every listing on every dimension, 1.0-10.0,
using only the definition given. Do not invent dimensions, do not skip
dimensions, and do not import ideas from other career paths — a
dimension means precisely what its definition says here.

Be harsh and honest. Most listings are mediocre. Do not inflate.
An 8+ on a dimension should be rare. Where a listing makes a claim the
description does not actually evidence, score the evidence, not the claim.

Every listing also needs a `risk`: the strongest honest reason this
person might NOT want to apply. If there genuinely is no meaningful
reason, return null — but reach for null rarely.

Where the candidate's RÉSUMÉ is given, judge against what it actually
evidences, not against what the listing asks for in the abstract. Their
`angles` are a summary someone wrote about them; the résumé is the
record. When the two disagree, believe the résumé.

Also return `evidence`: the single strongest concrete thing in their
résumé that this specific employer would care about — a shipped
product, a number, a named system. Quote or closely paraphrase the
résumé; do not generalise it into a claim they did not make. If the
résumé shows nothing relevant, return null, and let that be visible.

Return ONLY a JSON array. No preamble, no markdown fences.
Each element:
{
  "url": "<echo back exactly>",
  "breakdown": {"<each dimension id given to you>": <float 1.0-10.0>},
  "verdict": "<one blunt sentence — why this does or doesn't move them forward>",
  "stretch": <true if above their current level>,
  "angle": "<the single strongest line for their application note>",
  "risk": "<the strongest honest reason against applying, or null>",
  "evidence": "<the strongest concrete thing in their résumé this
                employer would care about, or null>"
}"""

# A résumé is a few thousand characters. Sent whole, in the cached
# system block, it is paid for once per scan and read back by every
# batch after that at a tenth of the price — so the marginal cost of
# giving the scorer the whole document instead of a summary is close to
# nothing. Truncated only to bound a pathological upload.
RESUME_CHARS = 6000


def _system(profile):
    """
    The half of the prompt that is identical for every batch in a scan.

    It lives in the system block, ahead of the listings, so it can be
    cached once and read back by the remaining batches. A scan scores
    ~200 candidates eight at a time, which means this text would
    otherwise be re-sent and re-billed twenty-five times per user.
    Key order is fixed — caching is a byte-exact prefix match, so a
    dict that serialised differently between calls would silently miss.
    """
    weights = profile.get("weights") or {}
    defs = dimensions.definitions_for(profile)
    spec = {d: {"weight_pct": weights.get(d), "means": defs[d]} for d in defs}

    # The résumé belongs HERE, inside the cached prefix, and not in the
    # per-batch message: it is identical for every batch in a scan, so
    # putting it here costs one cache write and then ~a tenth of the
    # price on every batch after. In the volatile half it would be
    # re-billed at full price twenty-five times per user per scan.
    resume = (profile.get("resume_text") or "").strip()
    resume_block = (
        f"\n\nTHEIR RÉSUMÉ — the record, and the thing to judge against:\n"
        f"{resume[:RESUME_CHARS]}"
        if resume else
        "\n\nTHEIR RÉSUMÉ: not provided. Score on the target profile "
        "alone, return null for `evidence`, and do not guess at what "
        "their experience might contain.")

    return f"""{SYSTEM}

CANDIDATE TARGET PROFILE:
{json.dumps(profile.get('target', {}), indent=2, sort_keys=True)}

THEIR UNFAIR ADVANTAGE:
{json.dumps(profile.get('angles', []), indent=2)}{resume_block}

SCORE EVERY LISTING ON EXACTLY THESE DIMENSIONS.
The weights show what matters to this person. Note how low
experience_fit is — stretch is good, not a risk:
{json.dumps(spec, indent=2, sort_keys=True)}"""


def _prompt(listings):
    """Only the volatile half. Must stay after the cache breakpoint."""
    return f"""LISTINGS TO SCORE:
{json.dumps(listings, indent=2)}"""


SCALE_MIN, SCALE_MAX = 1.0, 10.0


def _clean_breakdown(raw, dim_ids):
    """
    Keep only dimensions this profile knows about, on the stated scale.

    A model handed a 1-10 scale will occasionally answer 47, or -3, or
    "high". An out-of-range number is worse than a missing one: it
    renders as-is on the board and silently dominates the weighted
    average, so every other listing looks worse than it is. Clamp rather
    than drop — the model's ordering is still information.
    """
    if not isinstance(raw, dict):
        return {}

    out = {}
    for d, v in raw.items():
        if d not in dim_ids or isinstance(v, bool):
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):
            continue
        out[d] = min(SCALE_MAX, max(SCALE_MIN, v))
    return out


def score_jobs(jobs, profile, batch_size=8):
    """
    Returns every job it managed to score, highest first, each with a
    per-dimension `score_breakdown` and a `score` derived from it.

    No minimum is applied here. Filtering is the caller's decision —
    the portal sets a relative bar from the day's actual distribution,
    which it cannot do if the scorer has already thrown work away.
    """
    if not jobs:
        return []

    weights = profile.get("weights") or dimensions.FALLBACK_WEIGHTS
    dim_ids = list(dimensions.definitions_for(profile))
    system = _system(profile)
    scored = []
    billing = {"cache_read": 0, "cache_write": 0, "fresh": 0}

    for i in range(0, len(jobs), batch_size):
        chunk = jobs[i:i + batch_size]

        listings = [{
            "url": j["url"],
            "title": j["title"],
            "company": j["company"],
            "location": j.get("location"),
            "description": (j.get("description") or "")[:1500],
        } for j in chunk]

        try:
            results, usage = call_llm_usage(
                system, _prompt(listings), max_tokens=4000, as_json=True,
                cache_system=True)
            billing["cache_read"] += usage.get("cache_read_input_tokens") or 0
            billing["cache_write"] += usage.get(
                "cache_creation_input_tokens") or 0
            billing["fresh"] += usage.get("input_tokens") or 0
            by_url = {r["url"]: r for r in results if isinstance(r, dict)}

            for j in chunk:
                r = by_url.get(j["url"])
                if not r:
                    continue

                raw = r.get("breakdown")
                breakdown = _clean_breakdown(raw, dim_ids)
                total = dimensions.weighted_total(breakdown, weights)
                if total is None:
                    continue

                j.update({
                    "score": total,
                    "score_breakdown": breakdown,
                    "verdict": r.get("verdict", ""),
                    "stretch": bool(r.get("stretch")),
                    "angle": r.get("angle", ""),
                    "risk": r.get("risk") or None,
                    "evidence": r.get("evidence") or None,
                    "weakest": dimensions.weakest(breakdown, weights),
                })
                scored.append(j)
        except Exception as e:
            print(f"  [score] batch {i//batch_size} failed: {e}")

    scored.sort(key=lambda x: -x["score"])
    print(f"→ scored {len(scored)} of {len(jobs)} "
          f"on {len(dim_ids)} dimensions")
    _report_billing(billing)
    return scored


def _report_billing(billing):
    """
    Say plainly whether the prompt cache engaged.

    Caching declines silently below a model-dependent minimum prefix
    length — no error, just full price on every batch. A zero read count
    after the first batch is the only reliable signal, so it is printed
    rather than left for someone to notice on an invoice.
    """
    if not any(billing.values()):
        return
    read, write = billing["cache_read"], billing["cache_write"]
    total = read + write + billing["fresh"]

    if read:
        print(f"   prompt cache: {read:,} of {total:,} input tokens read "
              f"from cache (~90% cheaper)")
    elif write:
        print(f"   prompt cache: written but never read — only one batch ran")
    else:
        print("   prompt cache: NOT engaging. The shared prefix is likely "
              "below this model's minimum cacheable length; scoring still "
              "works, it is just paying full price per batch.")


# The score distribution used to be computed here as well, over the
# in-memory list a scan had just produced. It is gone: the board reads it
# from the database instead (core/board.py), per user and including runs
# older than today, which is the only version anything actually rendered.
