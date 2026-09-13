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


def test_a_title_naming_the_role_outranks_one_that_merely_shares_words():
    """Counting loose words scored 'Associate Manager - Risk' as high as
    'Senior Brand Manager' for a brand search, so the free filter spent
    the scoring budget on the wrong roles."""
    profile = {"target": {"role_family": "Brand Manager",
                          "alt_titles": ["Associate Brand Manager"]},
               "hard_filters": {}, "search_terms": ["Brand Manager"]}
    jobs = [{"url": "u1", "title": "Associate Manager - Risk & Compliance",
             "description": "d"},
            {"url": "u2", "title": "Senior Brand Manager", "description": "d"}]

    kept = prefilter.prefilter(jobs, profile)

    assert kept[0]["title"] == "Senior Brand Manager", \
        "a title that names the role must rank above one sharing stray words"
    assert kept[0]["_prefilter_signal"] > kept[1]["_prefilter_signal"]


def test_a_phrase_match_scores_full_marks(brand_profile):
    phrases, tokens = prefilter._wanted(brand_profile)

    assert prefilter._title_signal("Senior Brand Manager, D2C",
                                   phrases, tokens) == 1.0
    assert prefilter._title_signal("Associate Manager - Payroll",
                                   phrases, tokens) < 1.0


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


# ============================================================
# The form and the importer must not drift apart
# ============================================================
def test_every_generated_question_matches_back(tmp_path):
    """
    THE failure this whole generator exists to prevent. Answers are
    paired to the schema BY THE QUESTION TEXT, so one retyped word means
    that answer is dropped and the profile compiles without it — no
    error, just a worse job search.
    """
    import json

    import yaml

    from core import formgen
    from web.intake_routes import match_answers

    schema = yaml.safe_load(
        open("config/profile_schema.yml", encoding="utf-8"))
    script = formgen.apps_script(schema)
    questions = json.loads(
        script.split("var QUESTIONS = ", 1)[1].split(";\n\nfunction", 1)[0])

    # exactly the CSV Google Forms would export from that form
    row = {q["ask"]: f"answer-{q['id']}" for q in questions}
    row["Timestamp"] = "2026/09/13 10:00:00"

    answers, unmatched = match_answers(schema, row)

    assert unmatched == []
    assert set(answers) == {d["id"] for d in schema["dimensions"]}


def test_both_tracks_survive_into_the_answers():
    """The point of the rewrite: a dream job AND the best-paid job she
    could take today, kept apart rather than averaged into one target."""
    import yaml

    schema = yaml.safe_load(
        open("config/profile_schema.yml", encoding="utf-8"))
    tracks = {d.get("track") for d in schema["dimensions"]}

    assert "ideal" in tracks
    assert "leverage" in tracks


def test_the_script_is_balanced_javascript():
    """A stray brace means she pastes it in and gets a syntax error with
    no idea what to do."""
    import yaml

    from core import formgen

    script = formgen.apps_script(
        yaml.safe_load(open("config/profile_schema.yml", encoding="utf-8")))

    assert script.count("{") == script.count("}")
    assert script.count("(") == script.count(")")
    assert "function buildForm()" in script


# ============================================================
# The form WILL drift, and the profile must survive it
# ============================================================
def test_help_text_glued_into_a_title_still_matches():
    """
    Measured on the first real form, built by Gemini from the generated
    prompt: 19 of 22 questions came back character-perfect and three had
    the help text appended INTO the title. Under exact matching those
    three were dropped in silence — and they were location, min_ctc and
    notice_reality, which is to say the city, the salary floor and the
    notice period. The profile would have compiled with no hard filters
    at all and nobody would have been told.
    """
    import yaml

    from web.intake_routes import match_answers

    schema = yaml.safe_load(
        open("config/profile_schema.yml", encoding="utf-8"))
    row = {}
    for d in schema["dimensions"]:
        ask = " ".join(d["ask"].split())
        if d["id"] in ("location", "min_ctc", "notice_reality"):
            ask += " (help text: we have: 25 — change it if that's wrong)"
        row[ask] = "an answer"
    row["Timestamp"] = "2026/09/13 10:00:00"

    answers, unmatched = match_answers(schema, row)

    assert unmatched == []
    assert set(answers) == {d["id"] for d in schema["dimensions"]}


def test_smart_quotes_and_dashes_do_not_break_the_match():
    """Google and Word both rewrite apostrophes. "don't" and "don't" are
    the same question to a person and different bytes to a dict."""
    import yaml

    from web.intake_routes import match_answers

    schema = yaml.safe_load(
        open("config/profile_schema.yml", encoding="utf-8"))
    row = {}
    for d in schema["dimensions"]:
        ask = " ".join(d["ask"].split())
        row[ask.replace("'", "\u2019").replace(" - ", " \u2014 ")] = "x"

    answers, _ = match_answers(schema, row)

    assert set(answers) == {d["id"] for d in schema["dimensions"]}


def test_a_genuinely_different_question_is_still_reported():
    """Loose matching must not become guessing. A question nobody asked
    has to surface as unmatched, not be assigned to the nearest thing."""
    import yaml

    from web.intake_routes import match_answers

    schema = yaml.safe_load(
        open("config/profile_schema.yml", encoding="utf-8"))

    answers, unmatched = match_answers(
        schema, {"What is your favourite colour?": "blue"})

    assert answers == {}
    assert unmatched == ["What is your favourite colour?"]


def test_the_prompt_tells_the_model_where_help_text_goes():
    """The generator caused the bug above by writing the hint as a
    parenthetical on the question line."""
    import yaml

    from core import formgen

    schema = yaml.safe_load(
        open("config/profile_schema.yml", encoding="utf-8"))
    prompt = formgen.gemini_prompt(schema, known={"min_ctc": "25"})

    assert "DESCRIPTION FIELD" in prompt
    assert "NOT be appended to the question title" in prompt


# ============================================================
# The quiz, and the two targets it infers
# ============================================================
def test_the_form_never_asks_her_to_name_the_job():
    """
    The point of the rewrite. Most people cannot name the job that fits
    them — working it out is what they are here for — so asking up front
    only helps the people who already knew. The compiler infers it from
    what they enjoy and what drains them instead.
    """
    import yaml

    schema = yaml.safe_load(
        open("config/profile_schema.yml", encoding="utf-8"))
    asks = " ".join(d["ask"].lower() for d in schema["dimensions"])

    assert "what role do you want to be doing" not in asks
    assert "95% of" not in asks, "that is an interview question"


def test_it_is_mostly_ticking_boxes():
    """A form that takes effort is a form she abandons halfway."""
    import yaml

    schema = yaml.safe_load(
        open("config/profile_schema.yml", encoding="utf-8"))
    dims = schema["dimensions"]
    typing = [d for d in dims if d.get("type") == "text"]

    assert len(typing) / len(dims) < 0.3, "too much typing"
    assert len(dims) >= 25, "too thin to infer a career from"


def test_what_drains_her_is_a_filter_not_a_preference():
    """
    The most reliable answers in the form. People flatter themselves
    about strengths and are brutally accurate about what they hate — so
    someone who ticked "having to sell or chase targets" must never be
    shown a role with a quota, however well it pays.
    """
    import yaml

    schema = yaml.safe_load(
        open("config/profile_schema.yml", encoding="utf-8"))
    by_id = {d["id"]: d for d in schema["dimensions"]}

    assert by_id["drains_you"]["compiles_to"] == "filter"
    assert by_id["worst_boss"]["compiles_to"] == "filter"
    assert by_id["leverage_refuse"]["compiles_to"] == "filter"


def test_both_targets_survive_being_saved(tmp_path):
    """
    They did not. save_profile writes a fixed column list, so
    leverage_target, track_weighting and inference_note were computed by
    the compiler, returned, and dropped on the floor at the moment of
    saving — no error, just half the feature quietly missing.
    """
    from core import db, session

    conn = db.connect(str(tmp_path / "p.db"))
    uid, _ = session.create_account(conn, "wife", "pw")
    db.save_profile(conn, uid, {
        "target": {"role_family": "Process Excellence"},
        "leverage_target": {"role_family": "Operations Analyst"},
        "track_weighting": 40,
        "inference_note": "why we think so",
        "weights": {"trajectory_fit": 100}, "hard_filters": {},
        "dimension_definitions": {}, "angles": []})

    got = db.load_profile(conn, uid)

    assert got["leverage_target"]["role_family"] == "Operations Analyst"
    assert got["track_weighting"] == 40
    assert got["inference_note"] == "why we think so"
    conn.close()


def test_the_compiler_is_told_to_name_real_searchable_titles():
    """"Something creative" is not a job search. The inferred titles
    become search terms on Naukri and LinkedIn, so they have to be
    titles that actually appear there."""
    from core.intake import COMPILE_SYSTEM

    assert "searchable" in COMPILE_SYSTEM.lower()
    assert "leverage_target" in COMPILE_SYSTEM
    assert "inference_note" in COMPILE_SYSTEM
