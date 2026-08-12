"""
PHASE 0 — the scoring vocabulary, per user.

The scorer used to have five fixed dimensions. One of them
(`ai_product_depth`) is meaningless for anyone not chasing AI roles:
every listing scores zero and the whole board skews. So dimensions are
now DATA, not code.

Three dimensions are universal because they are true of every career
move in any field. The rest are invented by the compiler from that
person's own intake, and each one arrives with a definition — without
the definition the scorer is guessing.

Nothing in this file names a role, an industry, or a skill.
"""

import math
import re

# ------------------------------------------------------------
# The three that apply to everyone, whatever they're moving into
# ------------------------------------------------------------
UNIVERSAL = {
    "trajectory_fit":
        "Does this role move the candidate toward the target they stated, "
        "rather than sideways or back into what they already do? A role that "
        "is a plausible next step toward the target scores high even if they "
        "would have to grow into it.",
    "company_quality":
        "Would working here compound the candidate's career — the "
        "organisation's credibility, trajectory, and what the name signals "
        "on a CV two years from now. Judge the employer, not the perks.",
    "experience_fit":
        "Could the candidate plausibly clear this role's bar today. "
        "Deliberately weighted low: a gap here is a stretch, not a "
        "disqualification, and stretch is the point of this tool.",
}

UNIVERSAL_IDS = tuple(UNIVERSAL)

# experience_fit stays weighted under 10 — the founding constraint.
EXPERIENCE_FIT_CAP = 9
EXPERIENCE_FIT_DEFAULT = 8

# How many dimensions the compiler is allowed to invent.
MIN_GENERATED = 2
MAX_GENERATED = 3

# Used only when a profile has no usable weights at all.
FALLBACK_WEIGHTS = {
    "trajectory_fit": 60,
    "company_quality": 32,
    "experience_fit": EXPERIENCE_FIT_DEFAULT,
}


def slug(name):
    """`Creative Ownership` -> `creative_ownership`. Ids must be stable."""
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower())
    return s.strip("_")


def label(dim_id):
    """`creative_ownership` -> `Creative ownership`. For the UI only."""
    return (dim_id or "").replace("_", " ").strip().capitalize()


def generated_ids(weights):
    """Everything the compiler invented, in weight order."""
    gen = [d for d in weights if d not in UNIVERSAL]
    gen.sort(key=lambda d: -weights[d])
    return gen


def ordered_ids(weights):
    """Stable display order: trajectory, then generated, then the rest."""
    out = ["trajectory_fit"] if "trajectory_fit" in weights else []
    out += generated_ids(weights)
    out += [d for d in ("company_quality", "experience_fit") if d in weights]
    return out + [d for d in weights if d not in out]


def normalize(weights, definitions):
    """
    Take whatever the LLM produced and make it safe to score with.

    Enforces, in order:
      - ids are slugs
      - generated dimensions without a definition are dropped (unscoreable)
      - at most MAX_GENERATED generated dimensions survive, highest weight first
      - all three universal dimensions are present
      - experience_fit is under 10
      - the whole thing sums to exactly 100

    Returns (weights, definitions, notes) — notes explains every change,
    which the portal shows so a user can see why their profile looks
    different from what they answered.
    """
    notes = []

    # The compiler is an LLM, so `weights` may not even be a mapping.
    weights = weights if isinstance(weights, dict) else {}
    definitions = definitions if isinstance(definitions, dict) else {}

    clean_w, clean_d = {}, {}
    for raw_id, val in weights.items():
        d = slug(raw_id)
        if not d:
            continue
        try:
            val = float(val)
        except (TypeError, ValueError):
            notes.append(f"dropped '{raw_id}': weight was not a number")
            continue
        # inf and NaN both survive float(). inf then poisons the rescale
        # (inf/inf is NaN) and NaN loses every comparison it takes part
        # in, so the sort that picks which dimensions to keep goes
        # arbitrary. Reject both here rather than downstream.
        if not math.isfinite(val):
            notes.append(f"dropped '{raw_id}': weight was not a finite number")
            continue
        clean_w[d] = max(0.0, val)

    for raw_id, text in definitions.items():
        d = slug(raw_id)
        if d and isinstance(text, str) and text.strip():
            clean_d[d] = text.strip()

    # A generated dimension with no definition cannot be scored consistently
    # across batches, so it is worse than not having it at all.
    for d in [d for d in clean_w if d not in UNIVERSAL and d not in clean_d]:
        del clean_w[d]
        notes.append(f"dropped '{d}': no definition, so the scorer "
                     f"could not apply it consistently")

    gen = generated_ids(clean_w)
    for d in gen[MAX_GENERATED:]:
        del clean_w[d]
        clean_d.pop(d, None)
        notes.append(f"dropped '{d}': more than {MAX_GENERATED} generated "
                     f"dimensions dilutes all of them")
    gen = gen[:MAX_GENERATED]

    for d in UNIVERSAL_IDS:
        if d not in clean_w:
            clean_w[d] = (EXPERIENCE_FIT_DEFAULT if d == "experience_fit"
                          else FALLBACK_WEIGHTS.get(d, 10))
            notes.append(f"added '{d}': it applies to every career move")

    if clean_w.get("experience_fit", 0) > EXPERIENCE_FIT_CAP:
        notes.append(
            f"capped 'experience_fit' {clean_w['experience_fit']:.0f} -> "
            f"{EXPERIENCE_FIT_CAP}: stretch roles are the goal, not a risk")
        clean_w["experience_fit"] = EXPERIENCE_FIT_CAP

    if not gen:
        notes.append("no user-specific dimensions were generated — scoring "
                     "falls back to the three universal ones")

    return _rescale(clean_w, notes), clean_d, notes


def _rescale(weights, notes):
    """Sum to exactly 100, holding experience_fit at its capped value."""
    fixed = min(float(weights.get("experience_fit", EXPERIENCE_FIT_DEFAULT)),
                EXPERIENCE_FIT_CAP)
    others = {d: v for d, v in weights.items() if d != "experience_fit"}
    pool = 100.0 - fixed
    total = sum(others.values())

    if total <= 0:
        others = {d: v for d, v in FALLBACK_WEIGHTS.items()
                  if d != "experience_fit"}
        total = sum(others.values())
        notes.append("all non-experience weights were zero — reset to default")

    scaled = {d: v / total * pool for d, v in others.items()}
    out = {d: int(round(v)) for d, v in scaled.items()}
    out["experience_fit"] = int(round(fixed))

    # Rounding leaves a remainder of a point or two. Give it to the
    # heaviest dimension so the sum is exactly 100.
    drift = 100 - sum(out.values())
    if drift:
        heaviest = max((d for d in out if d != "experience_fit"),
                       key=lambda d: out[d], default=None)
        if heaviest:
            out[heaviest] += drift

    return {d: out[d] for d in ordered_ids(out)}


def definitions_for(profile):
    """Every dimension the scorer needs to know about, with its meaning."""
    weights = profile.get("weights") or {}
    defs = dict(profile.get("dimension_definitions") or {})
    merged = {}
    for d in ordered_ids(weights):
        merged[d] = defs.get(d) or UNIVERSAL.get(d) or label(d)
    return merged


def weakest(breakdown, weights=None, exclude=("experience_fit",)):
    """
    The dimension a listing scores worst on. The detail page is required
    to show this — a UI that only shows strengths produces bad
    applications. Ties break toward the heavier weight, because a weak
    score on something the user said matters is the more useful warning.

    experience_fit is excluded by default, and that exclusion is the
    whole philosophy in one line: a low experience_fit means the role is
    a stretch, which this tool treats as the goal. Listing it as a reason
    against would argue the exact opposite of what the stretch badge two
    inches above it says.
    """
    scores = {d: v for d, v in (breakdown or {}).items()
              if isinstance(v, (int, float)) and d not in (exclude or ())}
    if not scores:
        return None
    weights = weights or {}
    return min(scores, key=lambda d: (scores[d], -weights.get(d, 0)))


def weighted_total(breakdown, weights):
    """
    The score is derived from the breakdown, not asserted separately.

    This is what makes the weights real: if experience_fit is 8% of the
    profile it can only ever move the total by 8%, so a candidate who
    cannot do the job yet is not punished into invisibility.
    """
    used = {d: float(breakdown[d]) for d in weights
            if isinstance(breakdown.get(d), (int, float))}
    if not used:
        return None
    total_w = sum(weights[d] for d in used)
    if total_w <= 0:
        return None
    return round(sum(used[d] * weights[d] for d in used) / total_w, 1)
