"""
PHASE 0 CHECKPOINT — two profiles with different dimension sets
both score correctly, and neither inherits the other's vocabulary.

The bug this guards against: a fixed `ai_product_depth` dimension
scores zero on every listing for a brand marketer, dragging their
whole board down for a reason that has nothing to do with them.
"""

import json

from core import dimensions, intake, prefilter, score


# ------------------------------------------------------------
# The compiler names its own dimensions
# ------------------------------------------------------------
def test_two_profiles_get_disjoint_generated_dimensions(
        ai_pm_profile, brand_profile):
    a = set(dimensions.generated_ids(ai_pm_profile["weights"]))
    b = set(dimensions.generated_ids(brand_profile["weights"]))

    assert a == {"ai_product_depth", "domain_leverage"}
    assert b == {"creative_ownership", "brand_scale"}
    assert not (a & b), "generated dimensions leaked between users"


def test_universal_dimensions_are_present_in_both(ai_pm_profile, brand_profile):
    for p in (ai_pm_profile, brand_profile):
        for d in dimensions.UNIVERSAL_IDS:
            assert d in p["weights"]


def test_weights_sum_to_100_and_experience_fit_stays_under_10():
    raw = {"trajectory_fit": 40, "creative_ownership": 35,
           "brand_scale": 25, "company_quality": 20, "experience_fit": 30}
    defs = {"creative_ownership": "owns the brief", "brand_scale": "reach"}

    w, _, notes = dimensions.normalize(raw, defs)

    assert sum(w.values()) == 100
    assert w["experience_fit"] < 10
    assert any("experience_fit" in n for n in notes)


def test_generated_dimension_without_a_definition_is_dropped():
    raw = {"trajectory_fit": 40, "vibe": 30,
           "company_quality": 22, "experience_fit": 8}

    w, _, notes = dimensions.normalize(raw, {})

    assert "vibe" not in w, "an undefined dimension is unscoreable"
    assert any("vibe" in n for n in notes)
    assert sum(w.values()) == 100


def test_more_than_three_generated_dimensions_are_trimmed():
    raw = {"trajectory_fit": 20, "company_quality": 10, "experience_fit": 8,
           "a": 20, "b": 18, "c": 14, "d": 10}
    defs = {k: f"definition of {k}" for k in "abcd"}

    w, _, _ = dimensions.normalize(raw, defs)

    assert len(dimensions.generated_ids(w)) == dimensions.MAX_GENERATED
    assert "d" not in w, "the weakest generated dimension should go first"
    assert sum(w.values()) == 100


def test_normalize_survives_a_useless_compiler_output():
    w, _, _ = dimensions.normalize({}, {})

    assert sum(w.values()) == 100
    assert set(dimensions.UNIVERSAL_IDS) <= set(w)


def test_finalize_profile_records_what_it_changed():
    raw = {"weights": {"trajectory_fit": 60, "experience_fit": 40},
           "dimension_definitions": {}}

    p = intake.finalize_profile(raw)

    assert sum(p["weights"].values()) == 100
    assert p["weights"]["experience_fit"] < 10
    assert any(d.startswith("[compiler]") for d in p["dropped"]), \
        "a profile that silently differs from the answers is the failure mode"


# ------------------------------------------------------------
# The scorer reads dimensions from the profile, not from itself
# ------------------------------------------------------------
def test_scoring_prompt_contains_only_this_users_dimensions(
        ai_pm_profile, brand_profile):
    brand_prompt = score._system(brand_profile)
    ai_prompt = score._system(ai_pm_profile)

    assert "creative_ownership" in brand_prompt
    assert "ai_product_depth" not in brand_prompt, \
        "brand marketer was scored on someone else's dimension"
    assert "ai_product_depth" in ai_prompt
    assert "creative_ownership" not in ai_prompt


def test_scoring_prompt_carries_the_definitions(brand_profile):
    prompt = score._system(brand_profile)
    assert "own creative direction" in prompt, \
        "without definitions the scorer is guessing"
    assert "consumer-visible" in prompt


def test_the_cached_prefix_is_byte_stable_across_calls(brand_profile):
    """Caching is a byte-exact prefix match — a dict that serialises
    differently between batches would silently never cache."""
    assert score._system(brand_profile) == score._system(brand_profile)


def test_the_listings_stay_out_of_the_cached_prefix(brand_profile):
    """Volatile content after the breakpoint, or nothing ever caches."""
    system = score._system(brand_profile)
    assert "LISTINGS TO SCORE" not in system

    prompt = score._prompt([{"url": "u1", "title": "T", "company": "X"}])
    assert "LISTINGS TO SCORE" in prompt
    assert "creative_ownership" not in prompt


def _fake_llm(breakdowns):
    """Stand in for the API. Returns (results, usage) per listing."""
    def _call(system, prompt, **kw):
        urls = [l["url"] for l in json.loads(
            prompt.split("LISTINGS TO SCORE:\n")[1])]
        return ([{"url": u, "breakdown": breakdowns[u], "verdict": "v",
                  "stretch": True, "angle": "a", "risk": "r"} for u in urls],
                {})
    return _call


def test_both_profiles_score_without_touching_each_others_dimensions(
        monkeypatch, ai_pm_profile, brand_profile):
    job = lambda: [{"url": "u1", "title": "Manager", "company": "X",
                    "description": "d"}]

    monkeypatch.setattr(score, "call_llm_usage", _fake_llm({"u1": {
        "trajectory_fit": 8, "ai_product_depth": 7,
        "domain_leverage": 9, "company_quality": 6, "experience_fit": 3}}))
    ai_out = score.score_jobs(job(), ai_pm_profile)

    monkeypatch.setattr(score, "call_llm_usage", _fake_llm({"u1": {
        "trajectory_fit": 8, "creative_ownership": 7,
        "brand_scale": 9, "company_quality": 6, "experience_fit": 3}}))
    brand_out = score.score_jobs(job(), brand_profile)

    assert len(ai_out) == len(brand_out) == 1
    assert set(ai_out[0]["score_breakdown"]) == set(ai_pm_profile["weights"])
    assert set(brand_out[0]["score_breakdown"]) == set(brand_profile["weights"])
    # Same shape of scores, different vocabulary -> same total.
    assert ai_out[0]["score"] == brand_out[0]["score"]


def test_a_dimension_the_profile_does_not_have_is_ignored(
        monkeypatch, brand_profile):
    monkeypatch.setattr(score, "call_llm_usage", _fake_llm({"u1": {
        "trajectory_fit": 8, "creative_ownership": 7, "brand_scale": 9,
        "company_quality": 6, "experience_fit": 3, "ai_product_depth": 1}}))

    out = score.score_jobs([{"url": "u1", "title": "T", "company": "X"}],
                           brand_profile)

    assert "ai_product_depth" not in out[0]["score_breakdown"]


def test_experience_fit_cannot_sink_a_strong_stretch_role(brand_profile):
    """The whole philosophy, as arithmetic."""
    w = brand_profile["weights"]
    strong_stretch = {"trajectory_fit": 9, "creative_ownership": 9,
                      "brand_scale": 8, "company_quality": 8,
                      "experience_fit": 1}

    assert dimensions.weighted_total(strong_stretch, w) >= 8.0


def test_weakest_dimension_is_reported_for_the_reasons_against_section(
        brand_profile):
    breakdown = {"trajectory_fit": 9, "creative_ownership": 3,
                 "brand_scale": 8, "company_quality": 7, "experience_fit": 6}

    assert dimensions.weakest(breakdown, brand_profile["weights"]) \
        == "creative_ownership"


def test_a_low_experience_fit_is_never_a_reason_against(brand_profile):
    """The philosophy, guarded. A stretch role scores low on
    experience_fit by definition — reporting that as a weakness argues
    against the thing the tool exists to surface."""
    breakdown = {"trajectory_fit": 9, "creative_ownership": 8,
                 "brand_scale": 7, "company_quality": 8, "experience_fit": 2}

    assert dimensions.weakest(breakdown, brand_profile["weights"]) \
        == "brand_scale"


def test_weakest_is_none_when_only_experience_fit_was_scored():
    assert dimensions.weakest({"experience_fit": 1}) is None


# ------------------------------------------------------------
# The prefilter blocks only what this user's profile blocks
# ------------------------------------------------------------
def test_prefilter_does_not_block_a_word_from_another_users_profile(
        brand_profile):
    """The old global list killed anything containing 'sales'."""
    jobs = [{"url": "u1", "title": "Brand Manager - Sales Enablement",
             "company": "X", "description": "brand"}]

    assert len(prefilter.prefilter(jobs, brand_profile)) == 1


def test_prefilter_applies_this_users_own_exclusions(brand_profile):
    jobs = [{"url": "u1", "title": "Brand Manager", "company": "X",
             "description": "heavy telecalling required"}]

    assert prefilter.prefilter(jobs, brand_profile) == []


def test_prefilter_keeps_senior_stretch_titles(brand_profile):
    """`Head of`, `Principal`, `Director` used to be dropped outright —
    those are the stretch roles the tool exists to surface."""
    jobs = [{"url": "u1", "title": "Head of Brand Marketing",
             "company": "X", "description": "lead the brand"},
            {"url": "u2", "title": "Director - Brand", "company": "Y",
             "description": "own brand strategy"}]

    assert len(prefilter.prefilter(jobs, brand_profile)) == 2


def test_prefilter_years_ceiling_only_applies_when_the_profile_sets_one(
        ai_pm_profile, brand_profile):
    jobs = [{"url": "u1", "title": "Product Manager", "company": "X",
             "description": "requires 15+ years of experience"}]

    # ai_pm_profile sets max_years_required=8, brand_profile sets none.
    assert prefilter.prefilter(jobs, ai_pm_profile) == []
    assert len(prefilter.prefilter(
        [{"url": "u1", "title": "Brand Manager", "company": "X",
          "description": "requires 15+ years of experience"}],
        brand_profile)) == 1
