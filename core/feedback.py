"""
STAGE 9 — LEARN
The box your sketch didn't have, and no open-source tool has either.

Your diagram ended at "apply". But if you send 20 applications and
get 0 callbacks, something is wrong — and a system that never asks
"did that work?" will confidently repeat the same mistake weekly.

You log outcomes. The system re-weights.
"""

import json
import os
from collections import Counter
from datetime import datetime

from core import dimensions
from core.llm import call_llm

OUTCOMES_PATH = "data/outcomes.json"

VALID = ["applied", "no_response", "rejected", "screening",
         "interview", "offer", "skipped"]


def log(url, status, note=""):
    if status not in VALID:
        raise ValueError(f"status must be one of {VALID}")

    data = _load()
    data.append({
        "url": url,
        "status": status,
        "note": note,
        "at": datetime.now().isoformat(),
    })
    _save(data)
    print(f"→ logged: {status}")


def _load():
    if os.path.exists(OUTCOMES_PATH):
        with open(OUTCOMES_PATH) as f:
            return json.load(f)
    return []


def _save(data):
    os.makedirs("data", exist_ok=True)
    with open(OUTCOMES_PATH, "w") as f:
        json.dump(data, f, indent=2)


def stats():
    data = _load()
    c = Counter(d["status"] for d in data)
    applied = c["applied"] + c["no_response"] + c["rejected"] + \
              c["screening"] + c["interview"] + c["offer"]
    positive = c["screening"] + c["interview"] + c["offer"]
    rate = (positive / applied * 100) if applied else 0
    return {"counts": dict(c), "applied": applied,
            "positive": positive, "callback_rate": round(rate, 1)}


REVIEW_SYSTEM = """Diagnose why a job search is or isn't working.

Be blunt. If the callback rate is bad, name the most likely cause and
say which ONE thing to change. Do not list ten things — that is useless
advice. Rank ruthlessly.

Consider in order of likelihood:
  1. Targeting wrong (roles are out of reach, or wrong roles entirely)
  2. Positioning wrong (CV/angle isn't landing)
  3. Channel wrong (cold applications instead of human contact)
  4. Volume too low to conclude anything yet

If the sample is too small to draw conclusions, SAY THAT rather than
inventing a pattern. Fewer than 15 applications proves nothing.

Return ONLY JSON:
{
  "diagnosis": "<the single most likely cause>",
  "evidence": "<what in the data points there>",
  "change_this_one_thing": "<one specific action>",
  "weight_adjustments": {"<dimension>": <new int>},
  "sample_too_small": <true|false>
}"""


def review(profile):
    data = _load()
    s = stats()

    if s["applied"] < 5:
        print(f"\n  Only {s['applied']} applications logged. "
              f"Nothing to diagnose yet — keep going.\n")
        return None

    prompt = f"""CURRENT SCORING WEIGHTS:
{json.dumps(profile.get('weights', {}), indent=2)}

OUTCOME STATS:
{json.dumps(s, indent=2)}

RAW LOG:
{json.dumps(data[-40:], indent=2)}"""

    r = call_llm(REVIEW_SYSTEM, prompt, max_tokens=1500, as_json=True)

    print("\n" + "=" * 58)
    print("  SEARCH DIAGNOSTIC")
    print("=" * 58)
    print(f"\n  Applied: {s['applied']}   "
          f"Callbacks: {s['positive']}   "
          f"Rate: {s['callback_rate']}%")
    if r.get("sample_too_small"):
        print("\n  Sample still too small to trust. Treat as directional.")
    print(f"\n  Diagnosis: {r.get('diagnosis')}")
    print(f"  Evidence:  {r.get('evidence')}")
    print(f"\n  → CHANGE THIS: {r.get('change_this_one_thing')}")
    print("=" * 58 + "\n")

    return r


def apply_adjustments(profile, review_result, save=True):
    """
    Re-weight the scorer based on what actually got callbacks.

    Adjustments are re-normalised rather than written straight in: the
    reviewer is an LLM and will happily hand back weights that sum to
    140 or push experience_fit to 25, which would quietly undo the one
    constraint the whole tool is built on.
    """
    adj = (review_result or {}).get("weight_adjustments") or {}
    weights = dict(profile.get("weights") or {})

    # Only dimensions this user already has. The reviewer does not get to
    # invent new ones — those arrive with a definition or not at all.
    adj = {d: v for d, v in adj.items() if d in weights}
    if not adj:
        return profile

    weights.update(adj)
    weights, defs, notes = dimensions.normalize(
        weights, profile.get("dimension_definitions"))
    profile["weights"], profile["dimension_definitions"] = weights, defs

    if save:
        with open("data/profile.json", "w") as f:
            json.dump(profile, f, indent=2)

    print(f"→ weights updated: {adj}")
    for n in notes:
        print(f"  [normalised] {n}")
    return profile
