from core import constraints, prefilter, score

WEIGHTS = {"trajectory_fit": 25, "people_process": 25, "experience_fit": 7,
           "compensation": 18, "work_life_balance": 25}


def test_unknown_conditions_do_not_become_bad_fit_or_fake_certainty():
    job = {"description": "Own customer onboarding and team operations. " * 20}
    breakdown = dict.fromkeys(WEIGHTS, 8)
    total, clean = score.evidence_rating(job, {}, breakdown, WEIGHTS)
    assert total == 8
    assert "compensation" not in clean
    assert "work_life_balance" not in clean
    assert job['evaluation']['evidence_coverage'] == 57


def test_specialist_gap_cannot_win_by_title_similarity():
    quote = "Requires payroll expertise and HR certification."
    job = {"description": (quote + " ") * 20}
    total, _ = score.evidence_rating(
        job, {"qualification_gap": True, "qualification_gap_evidence": quote},
        dict.fromkeys(WEIGHTS, 9), WEIGHTS)
    assert total == 5


def test_explicit_kyc_tenure_requires_matching_resume_evidence():
    requirement = "5-8 years of experience in KYC"
    job = {"description": (requirement + ". Manage client onboarding. ") * 20}
    profile = {"resume_text": "Five years in banking. Reduced KYC turnaround."}
    total, _ = score.evidence_rating(job, {"qualification_gap": False},
                                     dict.fromkeys(WEIGHTS, 9), WEIGHTS, profile)
    assert total == 5
    assert job["evaluation"]["qualification_gap_evidence"] == requirement

    proven = {"resume_text": "Led KYC operations for five years."}
    total, _ = score.evidence_rating(dict(job, evaluation={}),
                                     {"qualification_gap": False},
                                     dict.fromkeys(WEIGHTS, 9), WEIGHTS, proven)
    assert total == 9


def test_documentation_specialism_needs_tenure_evidence():
    requirement = "6-10 years of experience in documentation review"
    job = {"description": (requirement + ", account maintenance and related cash products. ") * 20}
    profile = {"resume_text": "Approximately five years in banking. Coordinated KYC turnaround."}
    total, _ = score.evidence_rating(job, {"qualification_gap": False},
                                     dict.fromkeys(WEIGHTS, 9), WEIGHTS, profile)
    assert total == 5
    assert job["evaluation"]["qualification_gap_evidence"] == requirement

    optional = {"description": ("Preferred: " + requirement + ". ") * 20}
    total, _ = score.evidence_rating(optional, {"qualification_gap": False},
                                     dict.fromkeys(WEIGHTS, 9), WEIGHTS, profile)
    assert total == 9


def test_unsupported_gap_flag_does_not_cap_fit():
    job = {"description": "Own customer onboarding and banking operations. " * 20}
    total, _ = score.evidence_rating(
        job, {"qualification_gap": True, "qualification_gap_evidence": "CPA required"},
        dict.fromkeys(WEIGHTS, 8), WEIGHTS)
    assert total == 8
    assert job["evaluation"]["qualification_gap"] is False


def test_work_life_score_requires_verbatim_evidence():
    job = {"description": "Monday to Friday, no weekend shifts. " * 20}
    _, clean = score.evidence_rating(job, {"work_life_evidence": "Monday to Friday, no weekend shifts."}, dict.fromkeys(WEIGHTS, 8), WEIGHTS)
    assert clean['work_life_balance'] == 8


def test_partial_model_response_does_not_inflate_rating():
    total, _ = score.evidence_rating({}, {}, {"trajectory_fit": 10}, WEIGHTS)
    assert total is None


def test_naukri_lacs_salary_is_recognized():
    assert constraints.salary_range({"salary_text": "25-30 Lacs PA"}) == (25, 30)


def test_bounded_description_keeps_requirements_at_end():
    text = 'Responsibilities: ' + 'Manage operations. ' * 500 + 'Mandatory certification required.'
    compact = score.compact_description(text)
    assert len(compact) <= 5000
    assert compact.endswith('Mandatory certification required.')


def test_bounded_description_keeps_requirements_from_middle():
    text = ('Opening overview. ' * 250 + '\nRequirements: seven years banking operations.\n'
            + 'Generic responsibilities. ' * 250)
    compact = score.compact_description(text)
    assert len(compact) <= 5000
    assert 'Requirements: seven years banking operations.' in compact


def test_codex_description_cap_saves_tokens():
    text = 'Opening. ' * 500 + 'Requirements: banking operations required. ' + 'Closing. ' * 500
    compact = score.compact_description(text, 3000)
    assert len(compact) <= 3000
    assert 'Requirements: banking operations required.' in compact


def test_efficient_model_uses_short_ids_and_preserves_real_urls(monkeypatch):
    def answer(system, prompt, **kwargs):
        assert kwargs['model'] == 'gpt-6-luna'
        assert 'https://example.com' not in prompt
        return [{"url": "0", "breakdown": dict.fromkeys(WEIGHTS, 7)}], {}
    monkeypatch.setattr(score, 'call_llm_usage', answer)
    profile = {"weights": WEIGHTS, "dimension_definitions": {k: k for k in WEIGHTS},
               "_scoring": {"efficient": True, "model": "gpt-6-luna"}}
    rows = score.score_jobs([{"url": "https://example.com/job", "title": "Ops",
                             "company": "Bank", "description": "Manage customer operations. " * 30}], profile)
    assert len(rows) == 1
    assert rows[0]['url'] == 'https://example.com/job'


def test_local_scoring_spends_no_ai_tokens(monkeypatch):
    monkeypatch.setattr(score, 'call_llm_usage',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("API called")))
    profile = {"target": {"role_family": "Operations Manager"},
               "resume_text": "banking operations customer service process improvement",
               "weights": WEIGHTS,
               "dimension_definitions": {k: k for k in WEIGHTS},
               "_scoring": {"provider": "local", "model": "local-evidence-v1"}}
    rows = score.score_jobs([{"url": "https://example.com/job",
                              "title": "Operations Manager", "company": "Bank",
                              "description": "Lead customer service and process improvement. " * 20}], profile)
    assert len(rows) == 1
    assert rows[0]["evaluation"]["rating_method"] == "preliminary-resume-match-v1"


def test_codex_provider_scores_one_bounded_pass(monkeypatch):
    from core import codex_scorer

    def answer(system, listings, dimension_ids, model):
        assert model == "gpt-6-luna"
        assert len(listings) == 1
        return [{"url": "0", "breakdown": dict.fromkeys(WEIGHTS, 7),
                 "qualification_gap": False, "qualification_gap_evidence": None,
                 "work_life_evidence": None, "verdict": "Good match.",
                 "risk": "Pay unknown.", "evidence": "Banking operations.",
                 "angle": "Operations leadership.", "stretch": False,
                 "track": "both"}]

    monkeypatch.setattr(codex_scorer, "score", answer)
    profile = {"weights": WEIGHTS,
               "dimension_definitions": {k: k for k in WEIGHTS},
               "_scoring": {"provider": "codex", "model": "gpt-6-luna"}}
    rows = score.score_jobs([{"url": "https://example.com/job", "title": "Ops",
                              "company": "Bank", "description": "Operations. " * 50}], profile)
    assert len(rows) == 1
    assert rows[0]["score"] == 7


def test_cross_source_duplicates_prefer_official_listing():
    jobs = [
        {"url": "https://linkedin.com/1", "title": "Manager Operations",
         "company": "HSBC", "location": "Bengaluru, Karnataka, India",
         "source": "linkedin", "description": "Copied description" * 50},
        {"url": "https://hsbc.example/1", "title": "Manager Operations",
         "company": "HSBC", "location": "Bangalore Urban, Karnataka, India",
         "source": "eightfold", "description": "Official description" * 30},
    ]
    result = prefilter.dedupe_roles(jobs)
    assert len(result) == 1
    assert result[0]["url"] == "https://hsbc.example/1"


def test_unknown_locations_are_not_unsafely_deduplicated():
    jobs = [
        {"url": "https://example.com/1", "title": "Operations Manager", "company": "Bank"},
        {"url": "https://example.com/2", "title": "Operations Manager", "company": "Bank"},
    ]
    assert len(prefilter.dedupe_roles(jobs)) == 2
