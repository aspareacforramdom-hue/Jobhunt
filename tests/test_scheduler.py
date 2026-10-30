"""
PHASE 6 CHECKPOINT — the boot scan fires when a slot has passed, and
the debounce holds.

The failure this guards against is expensive in both directions: a scan
that never fires leaves three stale boards, and a scan that fires on
every restart burns API credit six times during one debugging session.
"""

from datetime import datetime, timedelta

import pytest

from core import db, scheduler, session
from core.scheduler import Scanner, slots_between


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "sched.db"))
    yield c
    c.close()


@pytest.fixture
def user(conn):
    uid, _ = session.create_account(conn, "alice", "pw")
    return uid


def _completed_run(conn, user_id, finished):
    """A successful run that finished at a given moment."""
    run_id = db.start_run(conn, user_id, "catchup")
    conn.execute("UPDATE runs SET status='ok', finished_at=?, jobs_found=3 "
                 "WHERE id=?", (finished.isoformat(), run_id))
    conn.commit()
    return run_id


# ------------------------------------------------------------
# Slot arithmetic
# ------------------------------------------------------------
def test_slots_that_passed_overnight_are_counted():
    start = datetime(2026, 8, 6, 8, 0, tzinfo=db.IST)     # after 07:00
    end = datetime(2026, 8, 7, 9, 0, tzinfo=db.IST)       # after next 07:00

    missed = slots_between(start, end)

    # 6th 18:00 and 7th 07:00
    assert len(missed) == 2


def test_no_slot_between_two_close_times():
    start = datetime(2026, 8, 7, 8, 0, tzinfo=db.IST)
    end = datetime(2026, 8, 7, 11, 0, tzinfo=db.IST)

    assert slots_between(start, end) == []


# ------------------------------------------------------------
# The decision
# ------------------------------------------------------------
def test_a_first_boot_with_no_history_scans(conn, user):
    decision = Scanner().decide(conn)

    assert decision["due"] is True
    assert "no successful scan" in decision["reason"]


def test_two_missed_slots_produce_one_scan_not_two(conn, user):
    """Boot at 23:00 having last scanned yesterday morning."""
    _completed_run(conn, user, datetime.now(db.IST) - timedelta(days=1,
                                                                hours=2))
    decision = Scanner().decide(conn)

    assert decision["due"] is True
    assert "catch-up scan" in decision["reason"]
    # One decision object, one scan. Missed slots never queue up.
    assert decision["reason"].count("running one") == 1


def test_a_restart_inside_the_debounce_window_does_not_scan(conn, user):
    _completed_run(conn, user, datetime.now(db.IST) - timedelta(minutes=30))

    decision = Scanner().decide(conn)

    assert decision["due"] is False
    assert "minimum gap" in decision["reason"]


def test_six_debug_restarts_produce_at_most_one_scan(conn, user):
    """The scan itself sets the timestamp that blocks the next five."""
    _completed_run(conn, user, datetime.now(db.IST) - timedelta(minutes=5))

    assert [Scanner().decide(conn)["due"] for _ in range(6)] == [False] * 6


def test_past_the_debounce_but_before_a_slot_still_does_not_scan(conn, user):
    """
    3h is a floor, not a trigger. A slot must also have passed.

    Pinned to a fixed clock: the state being tested only exists at
    certain hours of a real day, so this used to skip itself whenever a
    07:00 or 18:00 slot happened to fall in the last four hours — about
    a third of the day, and precisely the hours when the slot logic is
    doing something.
    """
    now = datetime(2026, 5, 20, 13, 0, tzinfo=db.IST)   # 07:00 done, 18:00 to come
    finished = now - timedelta(hours=4)                 # 09:00, no slot between
    assert slots_between(finished, now) == []

    _completed_run(conn, user, finished)
    decision = Scanner().decide(conn, now=now)

    assert decision["due"] is False
    assert "no slot has passed" in decision["reason"]


def test_a_slot_crossed_since_the_last_scan_does_trigger(conn, user):
    """The other half of the same rule, on the same fixed clock."""
    now = datetime(2026, 5, 20, 20, 0, tzinfo=db.IST)   # past the 18:00 slot
    finished = now - timedelta(hours=4)                 # 16:00, before it

    _completed_run(conn, user, finished)
    decision = Scanner().decide(conn, now=now)

    assert decision["due"] is True
    assert "slot(s) missed" in decision["reason"]


def test_cooldown_reports_time_remaining(conn, user):
    _completed_run(conn, user, datetime.now(db.IST) - timedelta(hours=1))

    left = Scanner().cooldown_remaining(conn)

    assert 0 < left <= 2 * 3600 + 60


# ------------------------------------------------------------
# Locking and override
# ------------------------------------------------------------
def test_a_running_scan_blocks_a_second_one(tmp_path):
    s = Scanner(db_path=str(tmp_path / "lock.db"))
    s._state["running"] = True

    started, why = s.start("manual", override=True)

    assert started is False
    assert "already running" in why


def test_the_manual_button_can_override_the_cooldown(tmp_path, monkeypatch):
    path = str(tmp_path / "override.db")
    c = db.connect(path)
    uid, _ = session.create_account(c, "alice", "pw")
    _completed_run(c, uid, datetime.now(db.IST) - timedelta(minutes=10))
    c.close()

    s = Scanner(db_path=path)
    # Without an override the cooldown holds...
    assert s.start("manual", override=False)[0] is False

    # ...with one it runs. It is the user's money.
    monkeypatch.setattr(s, "_run", lambda kind: None)
    assert s.start("manual", override=True)[0] is True


# ------------------------------------------------------------
# Multi-user behaviour
# ------------------------------------------------------------
def test_one_users_scoring_failure_does_not_stop_the_others(tmp_path,
                                                            monkeypatch):
    path = str(tmp_path / "multi.db")
    conn = db.connect(path)
    ids = {}
    for name in ("alice", "bob", "carol"):
        uid, _ = session.create_account(conn, name, "pw")
        ids[name] = uid
        db.save_profile(conn, uid, {
            "target": {"role_family": "Manager", "alt_titles": []},
            "hard_filters": {}, "search_terms": ["manager"],
            "weights": {"trajectory_fit": 60, "company_quality": 32,
                        "experience_fit": 8},
            "dimension_definitions": {}})

    listings = [{"url": "https://x.test/1", "title": "Manager",
                 "company": "Acme", "description": "manager role",
                 "source": "greenhouse", "found_at": db.now()}]

    monkeypatch.setattr(scheduler.discover, "discover_all",
                        lambda sources, dry=False: (list(listings), []))
    monkeypatch.setattr(scheduler.notify, "send_scan_summary",
                        lambda *a, **kw: True)

    def _score(jobs, profile, **kw):
        if profile.get("_boom"):
            raise RuntimeError("scoring blew up")
        return [dict(j, score=7.0, score_breakdown={"trajectory_fit": 7},
                     verdict="v", angle="a", stretch=False, risk=None)
                for j in jobs]

    monkeypatch.setattr(scheduler.score, "score_jobs", _score)

    original = db.load_profile

    def _load(c, uid):
        p = original(c, uid)
        if p and uid == ids["bob"]:
            p["_boom"] = True
        return p

    monkeypatch.setattr(scheduler.db, "load_profile", _load)

    Scanner(db_path=path)._scan(conn, "catchup")

    assert len(db.list_user_jobs(conn, ids["alice"])) == 1
    assert len(db.list_user_jobs(conn, ids["carol"])) == 1
    pending = db.list_user_jobs(conn, ids["bob"])
    assert len(pending) == 1
    assert pending[0]["score"] is not None
    assert pending[0]["evaluation"]["rating_provider"] == "local"

    bob_run = db.recent_runs(conn, ids["bob"])[0]
    assert bob_run["status"] == "error"
    assert "scoring blew up" in bob_run["error_text"]
    assert db.recent_runs(conn, ids["alice"])[0]["status"] == "ok"
    conn.close()


def test_the_scrape_runs_the_union_of_every_users_search_terms(tmp_path,
                                                               monkeypatch):
    path = str(tmp_path / "union.db")
    conn = db.connect(path)
    for name, terms in [("alice", ["Brand Manager"]),
                        ("bob", ["AI Product Manager"])]:
        uid, _ = session.create_account(conn, name, "pw")
        db.save_profile(conn, uid, {
            "target": {}, "hard_filters": {"locations": ["Mumbai"]},
            "search_terms": terms,
            "weights": {"trajectory_fit": 60, "company_quality": 32,
                        "experience_fit": 8}})

    seen = {}
    monkeypatch.setattr(scheduler.discover, "discover_all",
                        lambda sources, dry=False:
                        (seen.update(sources["jobspy"]) or ([], [])))

    Scanner(db_path=path, dry=True)._scan(conn, "catchup")

    assert seen["search_terms"] == ["AI Product Manager", "Brand Manager"]
    assert seen["locations"] == ["Mumbai"]
    conn.close()


def test_dry_mode_scores_nothing_and_sends_nothing(tmp_path, monkeypatch):
    path = str(tmp_path / "dry.db")
    conn = db.connect(path)
    uid, _ = session.create_account(conn, "alice", "pw")
    db.save_profile(conn, uid, {
        "target": {"role_family": "Manager"}, "hard_filters": {},
        "search_terms": ["manager"],
        "weights": {"trajectory_fit": 60, "company_quality": 32,
                    "experience_fit": 8}})

    monkeypatch.setattr(scheduler.discover, "discover_all",
                        lambda sources, dry=False: ([
                            {"url": "https://x.test/1", "title": "Manager",
                             "company": "Acme", "description": "manager",
                             "found_at": db.now()}], []))

    def _boom(*a, **kw):
        raise AssertionError("--dry must make zero LLM calls")

    monkeypatch.setattr(scheduler.score, "score_jobs", _boom)
    monkeypatch.setattr(scheduler.notify, "send_scan_summary", _boom)

    Scanner(db_path=path, dry=True)._scan(conn, "catchup")

    assert db.list_user_jobs(conn, uid) == []

    # Recorded, so the run is visible — but not as a success. A dry run
    # that satisfied last_successful_run started the 3-hour cooldown and
    # blocked the real scan it was supposed to be rehearsing.
    assert db.recent_runs(conn, uid)[0]["status"] == "dry"
    assert db.last_successful_run(conn, uid) is None
    assert Scanner(db_path=path).cooldown_remaining(conn) == 0
    conn.close()


def test_a_job_already_scored_for_a_user_is_not_paid_for_twice(tmp_path,
                                                               monkeypatch):
    path = str(tmp_path / "rescore.db")
    conn = db.connect(path)
    uid, _ = session.create_account(conn, "alice", "pw")
    db.save_profile(conn, uid, {
        "target": {"role_family": "Manager", "alt_titles": []},
        "hard_filters": {}, "search_terms": ["manager"],
        "weights": {"trajectory_fit": 60, "company_quality": 32,
                    "experience_fit": 8}})

    job = {"url": "https://x.test/1", "title": "Manager", "company": "Acme",
           "description": "manager", "found_at": db.now()}
    job_id = db.upsert_job(conn, job)
    db.upsert_user_job(conn, uid, job_id, {"score": 8.0})

    scored_batches = []
    monkeypatch.setattr(scheduler.score, "score_jobs",
                        lambda jobs, profile, **kw:
                        scored_batches.append(jobs) or [])
    monkeypatch.setattr(scheduler.notify, "send_scan_summary",
                        lambda *a, **kw: True)

    Scanner(db_path=path)._score_for_user(
        conn, db.get_user(conn, uid), db.load_profile(conn, uid),
        [job], {job["url"]: job_id}, "manual")

    assert scored_batches == [[]], "already-scored job was sent to the LLM again"
    conn.close()


@pytest.mark.parametrize("kind", ["manual", "wide", "discover"])
def test_normal_scan_includes_thousand_boards_and_other_sources(conn, user, tmp_path, monkeypatch, kind):
    from core import source_registry
    db.save_profile(conn, user, {"search_terms": ["operations"], "hard_filters": {}})
    cfg = tmp_path / "sources.yml"
    cfg.write_text("career_registry:\n  enabled: true\n", encoding="utf-8")
    scanner = Scanner(sources_path=str(cfg))
    calls = []
    monkeypatch.setattr(scheduler, "_merge_discovered_boards", lambda *a: None)
    monkeypatch.setattr(scheduler.discover, "discover_all", lambda sources, dry=False: (calls.append(("discovery", dry)) or [], []))
    def batch(connection, limit, pages, progress):
        calls.append(("boards", limit))
        progress(1000, 1000, 0)
        return [], []
    monkeypatch.setattr(source_registry, "scan_batch", batch)
    monkeypatch.setattr(scanner, "_score_for_user", lambda *a: calls.append(("scoring", a[-1])))
    monkeypatch.setattr(scheduler.linkcheck, "sweep", lambda *a, **kw: (0, 0))
    scanner._scan(conn, kind)
    assert ("discovery", kind == "discover") in calls
    assert ("boards", 1000) in calls
    assert any(c[0] == "scoring" for c in calls)
    if kind == "wide":
        assert ("scoring", "manual") in calls


def test_saved_rating_never_calls_discovery_or_scrapers(conn, user, tmp_path, monkeypatch):
    db.save_profile(conn, user, {"search_terms": ["operations"], "hard_filters": {}})
    job_id = db.upsert_job(conn, {"url": "https://example.com/1", "title": "Operations Manager"})
    cfg = tmp_path / "sources.yml"
    cfg.write_text("career_registry:\n  enabled: true\n", encoding="utf-8")
    scanner = Scanner(sources_path=str(cfg))
    def forbidden(*a, **kw):
        pytest.fail("saved-job rating triggered discovery")
    monkeypatch.setattr(scheduler.discover, "discover_all", forbidden)
    monkeypatch.setattr(scheduler, "_merge_discovered_boards", forbidden)
    calls = []
    monkeypatch.setattr(scanner, "_score_for_user", lambda *a: calls.append(a))
    scanner._scan(conn, "score")
    assert len(calls) == 1
    assert calls[0][3][0]["id"] == job_id
    assert calls[0][-1] == "score"


def test_every_saved_job_gets_local_resume_match_without_ai(conn, user, monkeypatch):
    profile = {"target": {"role_family": "Operations Manager"},
               "search_terms": ["Operations Manager"],
               "resume_text": "Banking operations customer service leadership",
               "hard_filters": {"locations": ["Bengaluru"]},
               "weights": {"trajectory_fit": 50, "people_process": 30,
                           "experience_fit": 20}}
    db.save_profile(conn, user, profile)
    rows = [
        {"url": "https://example.com/1", "title": "Operations Manager",
         "location": "Bengaluru", "description": "Lead banking operations and customer service. " * 20},
        {"url": "https://example.com/2", "title": "Operations Manager",
         "location": "Mumbai", "description": "Lead banking operations. " * 20},
        {"url": "https://example.com/3", "title": "Software Developer",
         "location": "Bengaluru", "description": "Develop software. " * 20},
    ]
    ids = {row["url"]: db.upsert_job(conn, row) for row in rows}
    monkeypatch.setattr(scheduler.prefilter, "prefilter", lambda items, p, **kw: [
        j for j in items if j["url"].endswith("/1")])
    monkeypatch.setattr(scheduler.score, "score_jobs", lambda *a, **kw:
                        pytest.fail("discovery spent AI tokens"))
    Scanner()._score_for_user(conn, db.get_user(conn, user), db.load_profile(conn, user),
                              rows, ids, "discover")
    matched = db.list_user_jobs(conn, user, exclude_ignored=False)
    assert len(matched) == 3
    assert all(j["score"] is not None for j in matched)
    assert all(j["evaluation"]["rating_provider"] == "local" for j in matched)
    assert sum(j["evaluation"]["eligible"] for j in matched) == 1
    assert db.recent_runs(conn, user)[0]["status"] == "discovery"
