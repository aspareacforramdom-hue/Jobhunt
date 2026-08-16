"""
SPEND CONTROLS — the tests that stand between this tool and a dead credit.

Context, because it is the whole reason this file exists: a metered scan
on the original settings cost about $20.82 against a $5/month plan. It
died roughly a quarter of the way through the first run, mid-search, and
nothing anywhere said so. The board just looked like a quiet market.

The waste was not one bug. It was five defaults, each individually
defensible:

  18 searches (every keyword, every scan, no rotation)
   x 50 jobs   (a floor the actor's own schema does not impose)
   x full descriptions on all of them ($0.005 -> $0.015 a job)
   x a 7-day window re-bought daily, because dedup happens after billing
   + the top 10 companies' reviews enriched PER USER, at $0.05 a start
     fee plus $0.009 a review, 60 reviews deep, for employers nobody had
     opened

Every test below pins one of those five back down.
"""

import pytest
import yaml

from core import db, discover, scheduler

CFG = yaml.safe_load(open("config/sources.yml", encoding="utf-8"))


@pytest.fixture
def apify():
    return CFG["apify"]


# ============================================================
# The estimate, and the ceiling that uses it
# ============================================================
def test_the_shipped_config_costs_a_fraction_of_the_old_one(apify):
    old = {"prices": apify["prices"], "results_wanted": 50,
           "fetch_details": True}

    assert discover.estimate_cost(old, 18) == pytest.approx(13.68, abs=0.01)
    assert discover.estimate_cost(apify, 6) < 1.0


def test_a_scan_cannot_exceed_the_ceiling_however_many_terms_exist(apify):
    """The failure was unbounded spend, so the bound is the test."""
    terms = [f"term{i}" for i in range(200)]

    kept, note = discover.affordable_terms(apify, terms)

    assert discover.estimate_cost(apify, len(kept)) <= \
        apify["max_spend_per_scan_usd"]
    assert note and "ceiling" in note


def test_an_impossibly_expensive_search_is_skipped_not_attempted():
    cfg = {"prices": {"run_init_usd": 5.0, "standard_job_usd": 1.0,
                      "detailed_job_usd": 1.0},
           "results_wanted": 50, "max_spend_per_scan_usd": 1.0}

    kept, note = discover.affordable_terms(cfg, ["a", "b"])

    assert kept == []
    assert "over the" in note


def test_no_ceiling_configured_means_no_trimming():
    """Absence of a ceiling must not silently become a ceiling of zero."""
    cfg = {"prices": {}, "results_wanted": 20}
    assert discover.affordable_terms(cfg, ["a", "b"])[0] == ["a", "b"]


# ============================================================
# What is actually asked of the paid search
# ============================================================
def test_the_search_buys_titles_not_descriptions(apify):
    """
    Descriptions cost triple and the shortlister only reads titles.
    They are bought later, for survivors.
    """
    payload = discover._naukri_payload(apify, "Product Manager", [], {})
    assert payload["fetchDetails"] is False


def test_a_daily_scan_does_not_re_buy_the_same_week_of_jobs(apify):
    """
    Dedup happens after billing. A 7-day window scanned daily buys each
    listing up to seven times before throwing six of them away.
    """
    assert apify["freshness"] == "1"


def test_consultant_reposts_are_excluded_at_the_source(apify):
    payload = discover._naukri_payload(apify, "Product Manager", [], {})
    assert payload["postedBy"] == ["1"]


def test_profile_filters_are_pushed_to_naukri_not_applied_after(apify):
    """
    The actor bills per result handed back, so filtering on their side
    is the difference between paying for 20 jobs and paying for 900.
    """
    payload = discover._naukri_payload(
        apify, "Product Manager", ["Bengaluru, India"],
        {"department": [10], "role_category": [1028],
         "work_mode": ["remote"]})

    assert payload["department"] == ["10"]
    assert payload["roleCategory"] == ["1028"]
    assert payload["workMode"] == ["remote"]
    assert payload["cities"] == ["97"]


def test_experience_and_salary_are_never_filtered_on(apify):
    """
    Not an oversight — a deliberate refusal.

    This tool weights experience_fit under 10% because stretch roles are
    the point. Filtering to the candidate's current experience would
    delete exactly the roles it exists to find, and a salary filter drops
    every "not disclosed" listing, which on Naukri is most of the good
    ones. Cheaper is not the only axis.
    """
    payload = discover._naukri_payload(
        apify, "Product Manager", [],
        {"experience": ["5"], "salary_range": ["25to50"]})

    assert "experience" not in payload
    assert "salaryRange" not in payload


# ============================================================
# Rotation — nobody's search gets permanently starved
# ============================================================
def test_every_search_term_is_used_within_a_few_scans(tmp_path):
    conn = db.connect(str(tmp_path / "rot.db"))
    try:
        terms = [f"t{i}" for i in range(18)]
        seen = set()
        for _ in range(3):
            seen |= set(scheduler._rotate_terms(conn, terms, 6))
        assert seen == set(terms)
    finally:
        conn.close()


def test_the_rotation_position_survives_a_restart(tmp_path):
    path = str(tmp_path / "rot2.db")
    terms = [f"t{i}" for i in range(10)]

    conn = db.connect(path)
    first = scheduler._rotate_terms(conn, terms, 4)
    conn.close()

    conn = db.connect(path)          # a fresh process would do this
    second = scheduler._rotate_terms(conn, terms, 4)
    conn.close()

    assert first != second, "a restart must not replay the same slice"


def test_no_cap_searches_everything(tmp_path):
    conn = db.connect(str(tmp_path / "rot3.db"))
    try:
        assert len(scheduler._rotate_terms(conn, ["a", "b", "c"], 0)) == 3
    finally:
        conn.close()


# ============================================================
# Seed once, then deltas — the fix for buying the same job daily
# ============================================================
def test_a_keywords_first_search_reaches_back_and_the_rest_do_not(apify):
    """
    A keyword with no history in the pool needs one deep pass. After
    that, asking for anything but "since last time" means re-buying
    listings we already own, because dedup happens after billing.
    """
    first = discover._naukri_payload(apify, "Product Manager", [], {},
                                     backfilled=[])
    later = discover._naukri_payload(apify, "Product Manager", [], {},
                                     backfilled=["Product Manager"])

    assert int(first["freshness"]) > int(later["freshness"])
    assert first["maxJobs"] > later["maxJobs"]


def test_the_deep_pass_is_priced_higher_so_the_ceiling_can_spread_it(apify):
    seed = discover.term_cost(apify, "x", backfilled=[])
    daily = discover.term_cost(apify, "x", backfilled=["x"])
    assert seed > daily


def test_seeding_is_spread_across_scans_rather_than_refused(apify):
    """
    Eighteen first-time keywords cost more than one scan's ceiling. The
    ceiling must take as many as it can afford and leave the rest for
    next time, not skip the tier entirely.
    """
    terms = [f"t{i}" for i in range(18)]

    taken, note = discover.affordable_terms(apify, terms, backfilled=[])

    assert 0 < len(taken) < len(terms)
    assert discover.estimate_cost(apify, taken, []) <= \
        apify["max_spend_per_scan_usd"]
    assert "next scan" in note


def test_a_seeded_keyword_is_never_deep_searched_again(tmp_path,
                                                       monkeypatch):
    """
    The whole value of the deep pass is that it happens once. Forgetting
    it across a restart would re-buy a month of history for every
    keyword — the most expensive thing this tool can do.
    """
    monkeypatch.setenv("APIFY_TOKEN", "x")
    path = str(tmp_path / "seed.db")

    sent = []

    class _Resp:
        def read(self):
            return b"[]"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout=None, **kw):
        import json as _json
        sent.append(_json.loads(req.data.decode()))
        return _Resp()

    monkeypatch.setattr(discover.urllib.request, "urlopen", fake_open)

    cfg = {"naukri_actor": "a/b", "results_wanted": 20,
           "backfill_results_wanted": 50, "freshness": "1",
           "backfill_freshness": "30"}

    conn = db.connect(path)
    try:
        cfg["backfilled_terms"] = scheduler._seeded_terms(conn)
        discover.from_apify(cfg, ["Product Manager"], [], {})
        scheduler._remember_seeded(conn, cfg["_seeded_terms"])
        assert sent[-1]["maxJobs"] == 50
    finally:
        conn.close()

    conn = db.connect(path)              # a restart
    try:
        cfg["backfilled_terms"] = scheduler._seeded_terms(conn)
        discover.from_apify(cfg, ["Product Manager"], [], {})
        assert sent[-1]["maxJobs"] == 20, \
            "a restart re-bought the keyword's whole history"
        assert sent[-1]["freshness"] == "1"
    finally:
        conn.close()


def test_a_keyword_with_no_results_still_counts_as_seeded(tmp_path,
                                                          monkeypatch):
    """
    "Nothing live for this keyword" is a real answer. Re-asking for it
    deeply every single day is exactly the waste this prevents.
    """
    monkeypatch.setenv("APIFY_TOKEN", "x")

    class _Empty:
        def read(self):
            return b"[]"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(discover.urllib.request, "urlopen",
                        lambda *a, **k: _Empty())
    cfg = {"naukri_actor": "a/b", "backfilled_terms": []}

    discover.from_apify(cfg, ["Obscure Role"], [], {})

    assert cfg["_seeded_terms"] == ["Obscure Role"]


# ============================================================
# LinkedIn — the same discipline, the opposite cost shape
# ============================================================
LI = CFG["linkedin"]


def test_one_run_carries_every_keyword():
    """
    THE point of the LinkedIn design, and the opposite of Naukri's.

    Naukri bills purely per result, so narrow searches are cheap and you
    want many. This actor bills per result with a floor — "minimum 150
    results when using pay-per-result billing" — so every run costs at
    least 150 results whether you needed 150 or 20. Eighteen narrow
    keyword runs would pay eighteen floors for the same jobs.
    """
    payload = discover._linkedin_payload(
        LI, ["Product Manager", "Brand Manager", "Data Engineer"],
        ["Bengaluru"], {}, seeding=False)

    assert payload["keyword"] == ["Product Manager", "Brand Manager",
                                  "Data Engineer"]
    assert discover.linkedin_cost(LI, False) < \
        3 * discover.linkedin_cost(LI, False) / 1.5, \
        "one run must beat one-run-per-keyword"


def test_linkedin_seeds_deep_once_then_asks_only_for_new():
    seed = discover._linkedin_payload(LI, ["pm"], [], {}, seeding=True)
    daily = discover._linkedin_payload(LI, ["pm"], [], {}, seeding=False)

    assert seed["publishedAt"] == "r2592000"      # 30 days
    assert daily["publishedAt"] == "r86400"       # 24 hours
    assert seed["maxItems"] > daily["maxItems"]
    assert discover.linkedin_cost(LI, True) > discover.linkedin_cost(LI, False)


def test_asking_below_the_billing_floor_saves_nothing():
    """A run is charged the floor regardless, so pretending otherwise in
    the estimate would under-report every scan."""
    cfg = dict(LI, max_items=10, billing_floor=150)
    assert discover.linkedin_cost(cfg, False) == \
        pytest.approx(LI["prices"]["run_start_usd"]
                      + 150 * LI["prices"]["result_usd"])


def test_experience_and_salary_are_refused_here_too():
    """Same refusal as Naukri: stretch roles are the product."""
    payload = discover._linkedin_payload(
        LI, ["pm"], [], {"experienceLevel": ["entry-level"],
                         "salaryBase": "100000"}, seeding=False)

    assert "experienceLevel" not in payload
    assert "salaryBase" not in payload


def test_rows_the_actor_marked_as_non_matching_are_dropped(monkeypatch):
    """
    The actor's own quality filters mark rows rather than dropping them
    — we are billed either way — so the mark has to be honoured here or
    agency spam reaches the board despite being filtered.
    """
    monkeypatch.setenv("APIFY_TOKEN", "x")
    import json as _json

    class _Resp:
        def read(self):
            return _json.dumps([
                {"title": "Product Manager", "companyName": "Acme",
                 "link": "https://li/1"},
                {"title": "Recruiter", "companyName": "Staffing",
                 "link": "https://li/2", "dynamicFilterMatch": False},
            ]).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(discover.urllib.request, "urlopen",
                        lambda *a, **k: _Resp())

    rows = discover.from_linkedin(dict(LI, actor="a/b"), ["pm"], [], {})

    assert [r["company"] for r in rows] == ["Acme"]


def test_linkedin_keeps_what_only_linkedin_has(monkeypatch):
    """Applicant count and Easy Apply are the reason to pay for this at
    all — a role with seven applicants is a different proposition."""
    monkeypatch.setenv("APIFY_TOKEN", "x")
    import json as _json

    class _Resp:
        def read(self):
            return _json.dumps([{"title": "PM", "companyName": "Acme",
                                 "link": "https://li/1",
                                 "applicantsCount": 7, "easyApply": True}]
                               ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(discover.urllib.request, "urlopen",
                        lambda *a, **k: _Resp())

    row = discover.from_linkedin(dict(LI, actor="a/b"), ["pm"], [], {})[0]

    assert row["applicants"] == 7
    assert row["easy_apply"] is True
    assert row["source"] == "linkedin"


# ============================================================
# Reviews: the most expensive thing that ran unasked
# ============================================================
def test_reviews_do_not_run_during_a_scan_by_default():
    assert CFG["reviews"]["enrich_during_scan"] is False


def test_we_stop_buying_reviews_the_model_never_reads():
    """
    summarize() truncates to 12,000 characters. 60 reviews is roughly
    double that, so half of every company's reviews were bought and
    discarded unread at $0.009 each.
    """
    from core import reviews
    assert reviews.REVIEWS_PER_COMPANY <= 20


def test_the_expensive_reviews_actor_is_skipped_when_there_is_nothing(
        tmp_path, monkeypatch):
    """
    The reviews actor charges a $0.05 start fee — ten times the ratings
    actor — before returning a single row. If the cheap ratings call
    already came back empty, paying to confirm it was pure waste.
    """
    from core import reviews
    conn = db.connect(str(tmp_path / "rev.db"))
    try:
        monkeypatch.setenv("APIFY_TOKEN", "x")
        monkeypatch.setattr(reviews, "available", lambda: True)
        monkeypatch.setattr(reviews, "fetch_ratings", lambda c: None)
        called = []
        monkeypatch.setattr(reviews, "fetch_reviews",
                            lambda c, limit=20: called.append(c) or [])

        reviews.enrich(conn, "Nobody Ltd")

        assert called == [], "paid for reviews after ratings came back empty"
    finally:
        conn.close()
