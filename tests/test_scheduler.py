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
    """3h is a floor, not a trigger. A slot must also have passed."""
    now = datetime.now(db.IST)
    # Land in a window with no slot crossing: 4h ago, same day, and
    # skip the assertion entirely if a slot genuinely fell in between.
    finished = now - timedelta(hours=4)
    if slots_between(finished, now):
        pytest.skip("a real slot fell inside the window at this clock time")

    _completed_run(conn, user, finished)
    decision = Scanner().decide(conn)

    assert decision["due"] is False
    assert "no slot has passed" in decision["reason"]


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
    assert db.list_user_jobs(conn, ids["bob"]) == []

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
    assert db.recent_runs(conn, uid)[0]["status"] == "ok"
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
