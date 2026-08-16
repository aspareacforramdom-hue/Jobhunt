"""
THE LOOP THAT WAS NEVER CLOSED, and the two efficiency fixes beside it.

core/feedback.py has existed since the beginning with a good idea in it:
log what happened, re-weight the scorer. It read `data/outcomes.json` —
the single-user JSON era — and nothing in the portal ever called it. So
every "I've applied", every rejection and every interview the board
recorded was discarded, and the scorer behaved identically in week ten
and week one.

Also here: the concurrency change to scoring, because the obvious way to
parallelise it would have destroyed the prompt cache and INCREASED the
bill; and the Workday fetcher, which is the largest free coverage gap.
"""

import time

import pytest

from core import db, discover, feedback, score, session


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "learn.db"))
    yield c
    c.close()


@pytest.fixture
def user(conn):
    uid, _ = session.create_account(conn, "alice", "pw")
    db.save_profile(conn, uid, {
        "target": {}, "hard_filters": {},
        "weights": {"trajectory_fit": 60, "company_quality": 32,
                    "experience_fit": 8},
        "dimension_definitions": {}})
    return uid


def _board(conn, uid, plan):
    n = 0
    for status, count in plan:
        for _ in range(count):
            n += 1
            jid = db.upsert_job(conn, {"url": f"https://x/{n}",
                                       "title": f"T{n}", "company": "C",
                                       "found_at": db.now()})
            db.upsert_user_job(conn, uid, jid, {"score": 7.0})
            if status != "new":
                db.set_status(conn, uid, jid, status)
    return n


# ============================================================
# Outcomes now come from the board
# ============================================================
def test_what_the_board_recorded_finally_reaches_the_reviewer(conn, user):
    _board(conn, user, [("applied", 4), ("rejected", 3),
                        ("no_response", 2), ("interview", 1)])

    s = feedback.stats_from(feedback.outcomes_for_user(conn, user))

    assert s["applied"] == 10
    assert s["positive"] == 1
    assert s["callback_rate"] == 10.0


def test_intentions_are_not_counted_as_applications(conn, user):
    """
    "Interested", "prepared" and "ignored" are states of mind. Counting
    them would make the callback rate look catastrophic and send the
    diagnosis chasing a problem that does not exist.
    """
    _board(conn, user, [("applied", 2), ("interested", 9),
                        ("ignored", 7), ("new", 5)])

    s = feedback.stats_from(feedback.outcomes_for_user(conn, user))

    assert s["applied"] == 2
    assert set(s["counts"]) == {"applied"}


def test_an_explicitly_logged_outcome_beats_the_board_status(conn, user):
    jid = db.upsert_job(conn, {"url": "https://x/1", "title": "T",
                               "company": "C", "found_at": db.now()})
    db.upsert_user_job(conn, user, jid, {"score": 7.0})
    db.set_status(conn, user, jid, "applied")
    db.record_outcome(conn, user, jid, "offer", "signed")

    got = feedback.outcomes_for_user(conn, user)

    assert [o["status"] for o in got] == ["offer"]


def test_outcomes_are_per_user(conn, user):
    other, _ = session.create_account(conn, "bob", "pw")
    _board(conn, user, [("applied", 3)])

    assert feedback.outcomes_for_user(conn, other) == []


def test_a_thin_sample_refuses_to_diagnose(conn, user, monkeypatch):
    """Four applications prove nothing, and inventing a pattern from
    them is worse than saying so."""
    monkeypatch.setattr(feedback, "call_llm",
                        lambda *a, **k: pytest.fail("should not ask"))
    _board(conn, user, [("applied", 4)])

    assert feedback.review({}, data=feedback.outcomes_for_user(conn, user)) \
        is None


def test_re_weighting_cannot_undo_the_stretch_principle(conn, user):
    """
    The reviewer is an LLM and will happily return weights summing to
    140, or push experience_fit up to 25 — which would quietly reverse
    the one rule the whole product is built on.
    """
    profile = db.load_profile(conn, user)

    feedback.apply_adjustments(
        profile, {"weight_adjustments": {"experience_fit": 40,
                                         "trajectory_fit": 100,
                                         "invented_dimension": 30}},
        save=False)

    assert sum(profile["weights"].values()) == 100
    assert profile["weights"]["experience_fit"] < 10
    assert "invented_dimension" not in profile["weights"]


# ============================================================
# Concurrency that does not break the prompt cache
# ============================================================
def _fake_llm(record, delay=0.05):
    import json as _json

    def call(system, prompt, max_tokens=None, as_json=False, tools=None,
             cache_system=False):
        record.append(time.perf_counter())
        time.sleep(delay)
        urls = [l["url"] for l in
                _json.loads(prompt.split("LISTINGS TO SCORE:")[1])]
        return ([{"url": u, "breakdown": {"trajectory_fit": 8},
                  "verdict": "v", "angle": "a"} for u in urls],
                {"cache_read_input_tokens": 10})
    return call


PROFILE = {"target": {"role_family": "PM"},
           "weights": {"trajectory_fit": 100},
           "dimension_definitions": {}, "angles": []}


def test_the_first_batch_runs_alone_so_the_cache_can_be_written(monkeypatch):
    """
    THE reason this is not a plain thread pool.

    A cache entry only becomes readable once the response that wrote it
    starts coming back. Fire every batch at once and each one pays full
    price for the same system block and writes it again — the naive
    parallelisation makes the bill go UP.
    """
    starts = []
    monkeypatch.setattr(score, "call_llm_usage", _fake_llm(starts))
    jobs = [{"url": f"u{i}", "title": "PM", "company": "C",
             "description": "d"} for i in range(32)]      # 4 batches

    score.score_jobs(jobs, PROFILE, workers=4)

    assert len(starts) == 4
    gap = starts[1] - starts[0]
    spread = max(starts[1:]) - min(starts[1:])
    assert gap >= 0.04, "batch 2 started before batch 1 wrote the cache"
    assert spread < gap, "batches 2..n did not run concurrently"


def test_every_listing_is_still_scored_when_batches_run_together(monkeypatch):
    monkeypatch.setattr(score, "call_llm_usage", _fake_llm([]))
    jobs = [{"url": f"u{i}", "title": "PM", "company": "C",
             "description": "d"} for i in range(40)]

    out = score.score_jobs(jobs, PROFILE, workers=4)

    assert len(out) == 40
    assert [j["score"] for j in out] == sorted(
        (j["score"] for j in out), reverse=True)


def test_one_failing_batch_does_not_lose_the_others(monkeypatch):
    calls = []

    def flaky(system, prompt, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("429 from the API")
        import json as _json
        urls = [l["url"] for l in
                _json.loads(prompt.split("LISTINGS TO SCORE:")[1])]
        return ([{"url": u, "breakdown": {"trajectory_fit": 7}}
                 for u in urls], {})

    monkeypatch.setattr(score, "call_llm_usage", flaky)
    jobs = [{"url": f"u{i}", "title": "PM", "company": "C",
             "description": "d"} for i in range(24)]      # 3 batches

    out = score.score_jobs(jobs, PROFILE, workers=3)

    assert 0 < len(out) <= 16, "a failed batch should cost only its own 8"


# ============================================================
# Workday — the biggest free source that was missing
# ============================================================
def test_a_workday_target_needs_a_tenant_and_a_site():
    """Unlike the other four platforms a slug alone is not enough, and
    guessing the site name would silently return nothing."""
    assert discover.from_workday("justatenant") == []
    assert discover.from_workday("") == []


def test_workday_listings_are_mapped_to_openable_urls(monkeypatch):
    import json as _json

    class _Resp:
        def read(self):
            return _json.dumps({"total": 1, "jobPostings": [
                {"title": "Staff Engineer", "locationsText": "Bengaluru",
                 "externalPath": "/job/Bengaluru/Staff-Engineer_JR123"},
                {"title": "No path here"},
            ]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(discover.urllib.request, "urlopen",
                        lambda *a, **k: _Resp())

    rows = discover.from_workday("acme/AcmeCareers/wd5")

    assert len(rows) == 1, "a posting with no path cannot be opened"
    assert rows[0]["url"] == ("https://acme.wd5.myworkdayjobs.com/en-US/"
                             "AcmeCareers/job/Bengaluru/Staff-Engineer_JR123")
    assert rows[0]["source"] == "workday"


# ============================================================
# Instahyre — the free India-first source
# ============================================================
def test_instahyre_listings_are_mapped_and_deduped(monkeypatch):
    """
    The feed pages, and pages overlap. A duplicate reaching the pool is
    a listing someone is shown twice and, worse, scored twice.
    """
    import json as _json
    page = {"objects": [
        {"title": "Backend Engineer", "public_url": "https://i/1",
         "employer": {"company_name": "Acme"},
         "locations": "Bangalore,Pune", "keywords": "python, sql"},
        {"title": "No URL here", "employer": {"company_name": "Acme"}},
    ]}

    class _Resp:
        def read(self):
            return _json.dumps(page).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(discover.urllib.request, "urlopen",
                        lambda *a, **k: _Resp())

    rows = discover.from_instahyre(max_pages=3)

    assert len(rows) == 1, "the same page thrice must not yield three rows"
    assert rows[0]["company"] == "Acme"
    assert rows[0]["location"] == "Bangalore, Pune"
    assert rows[0]["source"] == "instahyre"


def test_instahyre_stops_cleanly_when_the_feed_fails(monkeypatch):
    """A source that is free is still a source that can be down, and it
    must not take the rest of the scan with it."""
    def boom(*a, **k):
        raise OSError("connection reset")

    monkeypatch.setattr(discover.urllib.request, "urlopen", boom)

    assert discover.from_instahyre(max_pages=3) == []
