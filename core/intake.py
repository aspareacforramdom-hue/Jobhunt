"""
STAGE 0 — INTAKE (your sketch's questionnaire + LOOP)
STAGE 1 — COMPILE (soft answers -> hard filters, weights, angles)

The loop is adaptive: it asks about whatever dimension is most
ambiguous, not a fixed script. It stops. That's the part your
sketch was missing.
"""

import json
import os

import yaml

from core import dimensions
from core.llm import call_llm

PROFILE_PATH = "data/profile.json"


# ------------------------------------------------------------
# Resume parsing — extracts what it can, flags what it can't
# ------------------------------------------------------------
RESUME_SYSTEM = """Extract a structured profile from this CV.

Also identify what the CV does NOT tell you about the person's
FUTURE intent — a CV is a record of the past, and this system
scores against the future.

Return ONLY JSON:
{
  "current_role": "...",
  "years_experience": <number>,
  "hard_skills": [...],
  "domains": [...],
  "shipped_things": ["concrete things they built or ran"],
  "implied_trajectory": "where this CV seems to be heading",
  "ambiguous": ["dimension ids from the schema that the CV cannot answer"]
}"""


def parse_resume(cv_text, schema):
    dim_ids = [d["id"] for d in schema["dimensions"]]
    prompt = (f"SCHEMA DIMENSIONS: {dim_ids}\n\n"
              f"CV:\n{cv_text[:8000]}")
    return call_llm(RESUME_SYSTEM, prompt, max_tokens=1500, as_json=True)


# ------------------------------------------------------------
# Adaptive question selection
# ------------------------------------------------------------
NEXT_Q_SYSTEM = """You are running an adaptive intake for a job-matching system.

Pick the SINGLE next question that would most reduce uncertainty
about what job this person actually needs. Target the dimension
where you know least and where knowing would most change the search.

Do not ask what the CV already answers.
Do not ask filler. Every question must change the output.

Return ONLY JSON:
{
  "dimension_id": "...",
  "question": "<rephrased in plain, warm language>",
  "options": ["...", "..."],
  "why_this_one": "<one line: what this unlocks>"
}"""


def next_question(schema, parsed_cv, answers_so_far):
    unresolved = [d for d in schema["dimensions"]
                  if d["id"] not in answers_so_far]
    if not unresolved:
        return None
    if len(answers_so_far) >= schema["loop"]["max_questions"]:
        return None

    prompt = f"""CV EXTRACT:
{json.dumps(parsed_cv, indent=2)}

ANSWERED ALREADY:
{json.dumps(answers_so_far, indent=2)}

STILL UNRESOLVED:
{json.dumps(unresolved, indent=2)}"""

    return call_llm(NEXT_Q_SYSTEM, prompt, max_tokens=600, as_json=True)


# ------------------------------------------------------------
# THE COMPILER — the discipline that makes soft answers useful
# ------------------------------------------------------------
COMPILE_SYSTEM = """Compile a person's raw intake answers into a machine-usable
search profile.

THE RULE: every answer must become exactly one of —
  1. a HARD FILTER   (binary reject, no judgement)
  2. a SCORE WEIGHT  (0-100, must sum to 100 across all weights)
  3. an ANGLE        (narrative fuel for the application)

If an answer becomes none of these, DROP IT. Do not invent a use
for vague input. Dropping is correct behaviour.

YOU MUST NAME THE SCORING DIMENSIONS YOURSELF.

Three dimensions are fixed because they apply to every career move:
  trajectory_fit   — does this role move them toward their stated target
  company_quality  — will this employer compound their career
  experience_fit   — could they clear the bar today

On top of those, invent 2 or 3 dimensions that capture what actually
separates a good role from a bad one FOR THIS PERSON. Derive them from
their target and their answers — not from a menu. Someone moving into
brand work and someone moving into infrastructure work should end up
with completely different dimensions, and neither should inherit the
other's. Name them in snake_case.

Every invented dimension needs a one-line definition telling a scorer
exactly what evidence in a job description makes it high or low. A
dimension a scorer cannot apply from a job description is useless —
do not invent one you cannot define concretely.

Weight experience_fit LOW (under 10). This person is moving into a
new field — stretch roles are the goal, not a risk. All weights must
sum to 100.

exclude_keywords are THIS person's exclusions only. A word that would
be noise for them may be the core of someone else's search, so never
add a generic blocklist — only terms their own answers rule out.

Return ONLY JSON:
{
  "target": {
    "role_family": "...",
    "alt_titles": [...],
    "seniority": "...",
    "industries": [...],
    "locations": [...]
  },
  "hard_filters": {
    "min_ctc_lpa": <number or null>,
    "locations": [...],
    "exclude_keywords": [...],
    "exclude_titles": ["title words this person specifically rules out"],
    "max_years_required": <int, or null if they set no ceiling>
  },
  "weights": {
    "trajectory_fit": <int>,
    "experience_fit": <int under 10>,
    "company_quality": <int>,
    "<dimension you invented>": <int>,
    "<dimension you invented>": <int>
  },
  "dimension_definitions": {
    "<dimension you invented>": "one line telling the scorer what to look for"
  },
  "angles": ["concrete lines usable in a cover letter"],
  "dropped": ["answers that compiled to nothing, and why"]
}"""


def compile_profile(parsed_cv, answers, save=True):
    prompt = f"""CV EXTRACT:
{json.dumps(parsed_cv, indent=2)}

INTAKE ANSWERS:
{json.dumps(answers, indent=2)}"""

    profile = call_llm(COMPILE_SYSTEM, prompt, max_tokens=2500, as_json=True)
    profile = finalize_profile(profile)

    if save:
        os.makedirs("data", exist_ok=True)
        with open(PROFILE_PATH, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2)
    return profile


def finalize_profile(profile):
    """
    Make a raw compiler output safe to score with.

    The LLM names the dimensions; this decides whether they are usable.
    Anything it changes is appended to `dropped`, because a profile that
    silently differs from what the person answered is the failure mode
    this tool exists to avoid.
    """
    profile = dict(profile) if isinstance(profile, dict) else {}

    weights, defs, notes = dimensions.normalize(
        profile.get("weights"), profile.get("dimension_definitions"))

    profile["weights"] = weights
    profile["dimension_definitions"] = defs

    # Every field below is read positionally elsewhere (`target.locations`,
    # `angles` iterated, `hard_filters` indexed). The compiler is an LLM and
    # will occasionally hand back a string where a dict belongs, so coerce
    # the type rather than trusting it — a wrong-typed field crashes intake
    # for that user and leaves them with no profile at all.
    def _typed(key, kind, fallback):
        value = profile.get(key)
        if isinstance(value, kind):
            return value
        if value not in (None, "", [], {}):
            notes.append(f"discarded '{key}': expected "
                         f"{kind.__name__}, got {type(value).__name__}")
        return fallback

    profile["target"] = _typed("target", dict, {})
    profile["angles"] = _typed("angles", list, [])
    profile["search_terms"] = _typed("search_terms", list, [])

    hf = _typed("hard_filters", dict, {})
    profile["hard_filters"] = hf
    hf.setdefault("min_ctc_lpa", None)
    locations = profile["target"].get("locations")
    hf.setdefault("locations", locations if isinstance(locations, list) else [])
    hf.setdefault("exclude_keywords", [])
    hf.setdefault("exclude_titles", [])
    hf.setdefault("max_years_required", None)

    dropped = list(profile.get("dropped") or [])
    profile["dropped"] = dropped + [f"[compiler] {n}" for n in notes]
    return profile


def load_profile():
    if os.path.exists(PROFILE_PATH):
        with open(PROFILE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return None


# ------------------------------------------------------------
# CLI driver
# ------------------------------------------------------------
def run_intake(cv_path):
    with open("config/profile_schema.yml", encoding="utf-8") as f:
        schema = yaml.safe_load(f)
    with open(cv_path, encoding="utf-8", errors="replace") as f:
        cv_text = f.read()

    print("→ Reading your CV...")
    parsed = parse_resume(cv_text, schema)
    print(f"  Current: {parsed.get('current_role')}")
    print(f"  Trajectory the CV implies: {parsed.get('implied_trajectory')}\n")

    answers = {}
    while True:
        q = next_question(schema, parsed, answers)
        if not q:
            break
        print(f"\n{q['question']}")
        if q.get("options"):
            for i, o in enumerate(q["options"], 1):
                print(f"  {i}. {o}")
        ans = input("> ").strip()
        if q.get("options") and ans.isdigit():
            idx = int(ans) - 1
            if 0 <= idx < len(q["options"]):
                ans = q["options"][idx]
        answers[q["dimension_id"]] = ans

    print("\n→ Compiling your answers into search constraints...")
    profile = compile_profile(parsed, answers)

    # Keep the CV itself, not only what was distilled from it. Scoring
    # and tailoring both read it later; without it they work from the
    # handful of sentences above, which is how the tailor ended up
    # inventing "existing" CV bullets.
    profile["resume_text"] = cv_text
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)

    print_dimensions(profile)

    if profile.get("dropped"):
        print("\n  Dropped as unusable (this is correct, not a bug):")
        for d in profile["dropped"]:
            print(f"    - {d}")

    print(f"\n→ Profile saved to {PROFILE_PATH}")
    return profile


def print_dimensions(profile):
    """Show what the compiler decided to score this person on."""
    weights = profile.get("weights", {})
    defs = dimensions.definitions_for(profile)
    gen = set(dimensions.generated_ids(weights))

    print("\n  Scoring dimensions for this profile:")
    for d in dimensions.ordered_ids(weights):
        tag = "generated" if d in gen else "universal"
        print(f"    {weights[d]:>3}%  {dimensions.label(d):<24} ({tag})")
        if d in gen:
            print(f"          {defs[d]}")
