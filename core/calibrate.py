"""
STAGE 2 — CALIBRATE
Your sketch's "internet -> general idea" box, with teeth.

Two jobs:
  1. Generate REAL search terms from what the market actually calls
     this role in India (not what you guess it's called)
  2. Tell you flatly if your target is unrealistic

The second one is the point. A tool that humours you is worse
than no tool.
"""

import json
import os

from core.llm import WEB_SEARCH_TOOL, call_llm

CALIB_PATH = "data/calibration.json"

SYSTEM = """You are a blunt career market analyst for the INDIAN job market.

Research the target role and return an honest calibration.

BE HARSH. If the person's target is unrealistic for their background,
say so plainly with numbers. Do not soften it. A person who wastes
six months applying to roles they cannot get is harmed more by your
politeness than by your bluntness.

Specifically check:
  - What is this role ACTUALLY called in Indian job postings?
  - What CTC range is real at their experience level, in India?
  - What do JDs actually require vs what the person has?
  - Which Indian companies genuinely hire for this?
  - What is the realistic gap, and what closes it fastest?

Return ONLY JSON:
{
  "real_titles": ["what Indian postings actually call this"],
  "search_terms": ["6-10 optimised search strings for job portals"],
  "ctc_reality": {
    "range_lpa": "X-Y",
    "their_realistic_band": "X-Y",
    "verdict": "<blunt sentence on whether their expectation holds>"
  },
  "common_requirements": ["what JDs keep asking for"],
  "their_gaps": ["what they're missing, ranked by how much it blocks them"],
  "fastest_gap_closers": ["concrete, specific, doable in <90 days"],
  "hiring_companies": ["real Indian companies hiring for this now"],
  "honest_verdict": "<2-3 sentences. No cushioning. Is this achievable in their timeline or not?>",
  "confidence": "<high|medium|low> — how good was the data you found"
}"""


def calibrate(profile, force=False):
    if os.path.exists(CALIB_PATH) and not force:
        with open(CALIB_PATH, encoding="utf-8") as f:
            return json.load(f)

    prompt = f"""Research the Indian job market for this person.

THEIR TARGET:
{json.dumps(profile.get('target', {}), indent=2)}

THEIR BACKGROUND / ANGLES:
{json.dumps(profile.get('angles', []), indent=2)}

THEIR HARD CONSTRAINTS:
{json.dumps(profile.get('hard_filters', {}), indent=2)}

Search the web for current Indian market data. Then calibrate."""

    result = call_llm(SYSTEM, prompt, max_tokens=4000,
                      as_json=True, tools=WEB_SEARCH_TOOL)

    os.makedirs("data", exist_ok=True)
    with open(CALIB_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    return result


def attach_search_terms(profile, calibration, limit=8):
    """
    Search terms belong to a person, not to config/sources.yml.

    They are the market's own words for the role that person is chasing,
    so a shared global list would search for one user's career on behalf
    of all three. The scraper runs the union of everyone's terms and
    stores each listing once; only the scoring is per-user.
    """
    terms = [t for t in (calibration or {}).get("search_terms", []) if t]
    if not terms:
        terms = [t for t in (calibration or {}).get("real_titles", []) if t]
    if not terms:
        target = profile.get("target", {}) or {}
        terms = [t for t in ([target.get("role_family")]
                             + list(target.get("alt_titles") or [])) if t]
    leverage = profile.get("leverage_target") or {}
    secondary = [t for t in [leverage.get("role_family")]
                 + list(leverage.get("alt_titles") or []) if t]
    # Interleave so a long ideal-target list cannot exhaust the cap before
    # the second target gets even one search term.
    combined = []
    for i in range(max(len(terms), len(secondary))):
        for group in (terms, secondary):
            if i < len(group) and group[i] not in combined:
                combined.append(group[i])
    profile["search_terms"] = combined[:limit]
    return profile


def print_calibration(c):
    print("\n" + "=" * 58)
    print("  MARKET REALITY CHECK")
    print("=" * 58)
    print(f"\n  Real titles: {', '.join(c.get('real_titles', [])[:5])}")

    ctc = c.get("ctc_reality", {})
    print(f"\n  Market CTC:      {ctc.get('range_lpa')} LPA")
    print(f"  Realistic for you: {ctc.get('their_realistic_band')} LPA")
    print(f"  → {ctc.get('verdict')}")

    print("\n  Your gaps (ranked):")
    for g in c.get("their_gaps", [])[:5]:
        print(f"    - {g}")

    print("\n  Fastest closers (<90 days):")
    for f_ in c.get("fastest_gap_closers", [])[:4]:
        print(f"    - {f_}")

    print(f"\n  VERDICT: {c.get('honest_verdict')}")
    print(f"  (data confidence: {c.get('confidence')})")
    print("=" * 58 + "\n")
