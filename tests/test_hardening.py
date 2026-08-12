"""
ADVERSARIAL TESTS — the inputs a real run will actually produce.

Nothing here is a happy path. The pipeline eats LLM output, scraped
HTML, and hand-edited CSV, none of which are trustworthy:

  - an LLM returns malformed JSON, invents fields, or omits them
  - a scraped listing has no title, no company, or a title full of
    angle brackets
  - a compiler hands back weights that are negative, NaN, or 400
  - a background scan writes while a request reads

Anything that reaches a user's screen or their Telegram has to survive
all of it without crashing and without corrupting another user's data.
"""

import json
import threading

import pytest

from core import (board, db, dimensions, intake, legacy, notify, prefilter,
                  scheduler, score, session)
from core.scheduler import slots_between


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "hard.db"))
    yield c
    c.close()


@pytest.fixture
def user(conn):
    uid, _ = session.create_account(conn, "alice", "pw")
    return uid


# ============================================================
# The weight normaliser — the compiler is an LLM
# ============================================================
@pytest.mark.parametrize("weights", [
    {"trajectory_fit": -50, "company_quality": 10, "experience_fit": 5},
    {"trajectory_fit": float("nan"), "company_quality": 10},
    {"trajectory_fit": float("inf"), "company_quality": 10},
    {"trajectory_fit": 1e12, "company_quality": 1},
    {"trajectory_fit": "thirty", "company_quality": 10},
    {"trajectory_fit": None, "company_quality": 10},
    {"": 50, "   ": 20, "company_quality": 30},
    {"Trajectory Fit": 40, "TRAJECTORY_FIT": 40},
    {"trajectory_fit": 0, "company_quality": 0, "experience_fit": 0},
])
def test_normalize_always_produces_a_usable_weight_set(weights):
    w, defs, notes = dimensions.normalize(weights, {})

    assert sum(w.values()) == 100, f"{weights} -> {w} does not sum to 100"
    assert all(isinstance(v, int) for v in w.values())
    assert all(v >= 0 for v in w.values()), "a negative weight survived"
    assert w["experience_fit"] < 10
    assert set(dimensions.UNIVERSAL_IDS) <= set(w)


def test_normalize_rejects_nan_rather_than_propagating_it():
    """NaN poisons every comparison it touches, including the sort that
    picks which dimensions to keep."""
    w, _, _ = dimensions.normalize(
        {"trajectory_fit": float("nan"), "spark": float("nan"),
         "company_quality": 10}, {"spark": "a definition"})

    assert all(v == v for v in w.values()), "NaN reached the final weights"
    assert sum(w.values()) == 100


def test_normalize_handles_a_non_dict():
    for junk in (None, [], "weights", 42):
        w, _, _ = dimensions.normalize(junk, junk)
        assert sum(w.values()) == 100


def test_weighted_total_ignores_dimensions_the_profile_does_not_have():
    total = dimensions.weighted_total(
        {"trajectory_fit": 8, "ghost": 10},
        {"trajectory_fit": 100})
    assert total == 8.0


def test_weighted_total_survives_junk_scores():
    assert dimensions.weighted_total({"a": "high"}, {"a": 50}) is None
    assert dimensions.weighted_total({}, {"a": 50}) is None
    assert dimensions.weighted_total({"a": 5}, {}) is None


def test_finalize_profile_survives_junk_from_the_compiler():
    for junk in (None, {}, {"weights": None}, {"target": "a string"},
                 {"hard_filters": None}):
        p = intake.finalize_profile(junk)
        assert sum(p["weights"].values()) == 100
        assert isinstance(p["hard_filters"], dict)
        assert isinstance(p["angles"], list)


# ============================================================
# The scorer — the LLM will return the wrong shape
# ============================================================
def _profile():
    return {"target": {"role_family": "X"}, "angles": [],
            "weights": {"trajectory_fit": 60, "company_quality": 32,
                        "experience_fit": 8},
            "dimension_definitions": {}}


def _jobs(n=2):
    return [{"url": f"u{i}", "title": f"T{i}", "company": "C",
             "description": "d"} for i in range(n)]


@pytest.mark.parametrize("payload", [
    "not json at all",
    {"not": "a list"},
    [],
    [{"no_url": 1}],
    [{"url": "u0"}],                                   # no breakdown
    [{"url": "u0", "breakdown": None}],
    [{"url": "u0", "breakdown": {"trajectory_fit": "high"}}],
    [{"url": "u0", "breakdown": {}}],
    [{"url": "unknown-url", "breakdown": {"trajectory_fit": 8}}],
    [None, 42, "x"],
])
def test_the_scorer_never_crashes_on_bad_llm_output(monkeypatch, payload):
    def _call(system, prompt, **kw):
        if isinstance(payload, str):
            raise ValueError("could not parse")
        return payload, {}

    monkeypatch.setattr(score, "call_llm_usage", _call)
    out = score.score_jobs(_jobs(), _profile())

    assert isinstance(out, list)
    for j in out:
        assert isinstance(j["score"], float)


def test_a_failed_batch_does_not_lose_the_other_batches(monkeypatch):
    calls = {"n": 0}

    def _call(system, prompt, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("rate limited")
        urls = [l["url"] for l in
                json.loads(prompt.split("LISTINGS TO SCORE:\n")[1])]
        return ([{"url": u, "breakdown": {"trajectory_fit": 7},
                  "verdict": "v"} for u in urls], {})

    monkeypatch.setattr(score, "call_llm_usage", _call)
    out = score.score_jobs(_jobs(4), _profile(), batch_size=2)

    assert len(out) == 2, "a failed batch took the healthy one with it"


def test_scores_are_clamped_to_the_visible_range(monkeypatch):
    """An LLM handed a 1-10 scale will occasionally return 47 or -3.
    A 47 renders as a 47 on the board and breaks every comparison."""
    monkeypatch.setattr(score, "call_llm_usage", lambda s, p, **kw: ([
        {"url": "u0", "breakdown": {"trajectory_fit": 47,
                                    "company_quality": -3,
                                    "experience_fit": 5}}], {}))

    out = score.score_jobs(_jobs(1), _profile())

    assert out, "the listing was dropped entirely"
    assert 1.0 <= out[0]["score"] <= 10.0
    assert all(1.0 <= v <= 10.0 for v in out[0]["score_breakdown"].values())


# ============================================================
# Hostile strings — scraped titles are not trusted input
# ============================================================
HOSTILE = 'Dev <script>alert(1)</script> & "C++" <b>'


def test_telegram_messages_escape_hostile_titles(conn, user, monkeypatch):
    """notify sends with parse_mode=HTML. An unescaped `<` makes
    Telegram reject the whole message with a 400, so the user silently
    gets no digest at all."""
    db.set_telegram_chat_id(conn, user, "chat-1")
    sent = []
    monkeypatch.setattr(notify, "_send",
                        lambda text, chat_id=None, dry=False:
                        sent.append(text) or True)

    notify.send_scan_summary(db.get_user(conn, user),
                             [{"score": 8.0, "title": HOSTILE,
                               "company": "A & B", "stretch": False}],
                             "http://localhost")

    body = sent[0]
    assert "<script>" not in body, "unescaped markup reached Telegram"
    assert "&lt;script&gt;" in body
    assert "&amp;" in body


def test_the_digest_also_escapes(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_send",
                        lambda text, chat_id=None, dry=False:
                        sent.append(text) or True)

    notify.send_digest([{"score": 9.0, "title": HOSTILE, "company": "A & B",
                         "url": "https://x/?a=1&b=2", "verdict": "<i>v</i>",
                         "angle": "5 > 3", "location": None}], {})

    assert "<script>" not in sent[0]
    assert "&lt;script&gt;" in sent[0]


def test_html_templates_escape_hostile_job_content(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from web.app import create_app

    monkeypatch.setenv("CAREERLOOP_SECRET", "test-secret")
    path = str(tmp_path / "xss.db")
    c = db.connect(path)
    uid, _ = session.create_account(c, "alice", "pw")
    c.execute("UPDATE users SET password_hash=?, salt=? WHERE id=?",
              (*session.hash_password("pw"), uid))
    c.commit()
    db.save_profile(c, uid, _profile())
    jid = db.upsert_job(c, {"url": "https://x/1", "title": HOSTILE,
                            "company": HOSTILE, "description": HOSTILE,
                            "found_at": db.now()})
    db.upsert_user_job(c, uid, jid, {"score": 8.0, "verdict": HOSTILE,
                                     "angle": HOSTILE, "risk": HOSTILE,
                                     "score_breakdown": {"trajectory_fit": 8}})
    c.close()

    with TestClient(create_app(boot_scan=False, db_path=path)) as client:
        client.post("/login", data={"username": "alice", "password": "pw"})
        for path_ in ("/", "/jobs", f"/jobs/{jid}"):
            html = client.get(path_).text
            assert "<script>alert(1)</script>" not in html, \
                f"{path_} rendered raw markup from a scraped listing"


# ============================================================
# Missing data — scrapers return incomplete rows
# ============================================================
def test_a_listing_with_no_title_or_company_does_not_break_anything(conn,
                                                                    user):
    jid = db.upsert_job(conn, {"url": "https://x/1", "title": None,
                               "company": None, "found_at": db.now()})
    db.upsert_user_job(conn, user, jid, {"score": 7.0})

    rows = db.list_user_jobs(conn, user)
    assert len(rows) == 1
    assert board.build_board(conn, user)["columns"][0]["shown"] == 1


def test_a_job_with_no_url_is_rejected_not_stored(conn):
    assert db.upsert_job(conn, {"title": "T"}) is None
    assert db.upsert_job(conn, {"url": None, "title": "T"}) is None
    assert db.upsert_job(conn, {"url": "", "title": "T"}) is None
    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 0


def test_an_unscored_job_does_not_render_as_a_zero(conn, user):
    """A NULL score is 'not scored', which is not the same claim as 0.0."""
    jid = db.upsert_job(conn, {"url": "https://x/1", "title": "T",
                               "found_at": db.now()})
    db.upsert_user_job(conn, user, jid, {"score": None})

    dist = board.distribution(conn, user)
    assert dist["count"] == 0, "an unscored job was counted in the spread"


def test_the_detail_page_survives_a_user_with_no_profile(tmp_path,
                                                         monkeypatch):
    from fastapi.testclient import TestClient

    from web.app import create_app

    monkeypatch.setenv("CAREERLOOP_SECRET", "test-secret")
    path = str(tmp_path / "noprofile.db")
    c = db.connect(path)
    uid, _ = session.create_account(c, "alice", "pw")
    jid = db.upsert_job(c, {"url": "https://x/1", "title": "T",
                            "company": "C", "found_at": db.now()})
    db.upsert_user_job(c, uid, jid, {"score": 7.0,
                                     "score_breakdown": {"mystery": 5}})
    c.close()

    with TestClient(create_app(boot_scan=False, db_path=path)) as client:
        client.post("/login", data={"username": "alice", "password": "pw"})
        assert client.get("/").status_code == 200
        assert client.get(f"/jobs/{jid}").status_code == 200


def test_prefilter_survives_an_empty_profile():
    jobs = [{"url": "u", "title": "Manager", "description": "d"}]
    for p in ({}, {"target": {}}, {"hard_filters": {}}, {"target": None}):
        assert prefilter.prefilter(jobs, p) == []


def test_prefilter_survives_listings_full_of_nones():
    jobs = [{"url": "u1", "title": None, "description": None,
             "location": None},
            {"url": "u2"}]
    profile = {"target": {"role_family": "Manager"},
               "hard_filters": {"max_years_required": 5}}
    assert prefilter.prefilter(jobs, profile) == []


# ============================================================
# The boot scheduler
# ============================================================
def test_slots_between_with_reversed_arguments_returns_nothing():
    from datetime import datetime, timedelta
    now = datetime.now(db.IST)
    assert slots_between(now, now - timedelta(days=2)) == []


def test_slots_between_over_a_long_outage_is_bounded():
    """A laptop closed for a year must not build a 730-element list and
    must still only trigger one scan."""
    from datetime import datetime, timedelta
    now = datetime.now(db.IST)
    missed = slots_between(now - timedelta(days=365), now)
    assert len(missed) > 0
    assert len(missed) <= 40, "unbounded slot accumulation"


def test_a_run_with_a_corrupt_finish_time_does_not_wedge_the_scanner(conn,
                                                                     user):
    run_id = db.start_run(conn, user, "catchup")
    conn.execute("UPDATE runs SET status='ok', finished_at='not-a-date' "
                 "WHERE id=?", (run_id,))
    conn.commit()

    decision = scheduler.Scanner().decide(conn)
    assert decision["due"] is True


def test_the_scan_lock_admits_exactly_one_of_ten_racing_starts(tmp_path):
    s = scheduler.Scanner(db_path=str(tmp_path / "race.db"))
    s._run = lambda kind: None          # never let it finish

    results, barrier = [], threading.Barrier(10)

    def go():
        barrier.wait()
        results.append(s.start("manual", override=True)[0])

    threads = [threading.Thread(target=go) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 1, f"{sum(results)} scans started at once"


# ============================================================
# Concurrency — the scan writes while the portal reads
# ============================================================
def test_a_background_write_does_not_block_a_read(tmp_path):
    path = str(tmp_path / "concurrent.db")
    setup = db.connect(path)
    uid, _ = session.create_account(setup, "alice", "pw")
    setup.close()

    errors = []

    def writer():
        c = db.connect(path)
        try:
            for i in range(60):
                jid = db.upsert_job(c, {"url": f"https://x/{i}",
                                        "title": f"T{i}",
                                        "found_at": db.now()})
                db.upsert_user_job(c, uid, jid, {"score": 7.0})
        except Exception as e:      # noqa: BLE001 - recording it is the test
            errors.append(f"writer: {e}")
        finally:
            c.close()

    def reader():
        c = db.connect(path)
        try:
            for _ in range(60):
                db.list_user_jobs(c, uid)
                board.build_board(c, uid)
        except Exception as e:      # noqa: BLE001
            errors.append(f"reader: {e}")
        finally:
            c.close()

    threads = [threading.Thread(target=writer),
               threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors


# ============================================================
# Legacy import
# ============================================================
def test_migration_survives_corrupt_and_hostile_json(conn, user, tmp_path):
    (tmp_path / "profile.json").write_text("{not json")
    (tmp_path / "latest.json").write_text(json.dumps(
        ["a string", 42, None, {"no_url": True},
         {"url": "https://x/1", "title": "T", "score": "high"}]))
    (tmp_path / "outcomes.json").write_text(json.dumps(
        [{"url": "https://x/1", "status": "made_up_status"},
         {"no_url": 1}, "junk"]))

    report = legacy.migrate_json(conn, user, data_dir=str(tmp_path),
                                 verbose=False)

    assert report["profile"] is False
    assert db.list_user_jobs(conn, user) is not None


def test_migration_is_idempotent(conn, user, tmp_path):
    (tmp_path / "latest.json").write_text(json.dumps(
        [{"url": "https://x/1", "title": "T", "company": "C", "score": 8.0}]))

    legacy.migrate_json(conn, user, data_dir=str(tmp_path), verbose=False)
    legacy.migrate_json(conn, user, data_dir=str(tmp_path), verbose=False)

    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 1
    assert len(db.list_user_jobs(conn, user)) == 1


# ============================================================
# Export
# ============================================================
def test_export_survives_none_and_nested_values(conn, user):
    from web import export

    jid = db.upsert_job(conn, {"url": "https://x/1", "title": None,
                               "company": HOSTILE, "found_at": db.now()})
    db.upsert_user_job(conn, user, jid, {
        "score": None, "verdict": None,
        "score_breakdown": {"trajectory_fit": 8}})

    blob, media, ext = export.build(db.list_user_jobs(conn, user), _profile())
    assert len(blob) > 0
    assert ext in ("xlsx", "csv")


def test_export_handles_an_empty_result_set():
    from web import export
    blob, _, _ = export.build([], _profile())
    assert len(blob) > 0


# ============================================================
# Session edge cases
# ============================================================
def test_a_cookie_for_a_deleted_user_is_rejected(conn):
    uid, _ = session.create_account(conn, "ghost", "pw")
    token = session.issue_session(uid)
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()

    assert session.user_from_cookie(conn, token) is None


@pytest.mark.parametrize("token", [
    "", None, ".", "..", "a.b", "!!!.!!!", "x" * 5000,
    "-1:0.deadbeef", "eyJhIjoxfQ.eyJiIjoyfQ",
])
def test_junk_cookies_are_rejected_without_raising(token):
    assert session.read_session(token) is None


def test_a_password_with_unicode_and_nulls_round_trips():
    for pw in ["pässwörd", "日本語のパスワード", "a\x00b", " " * 50, "🔑" * 10]:
        h, salt = session.hash_password(pw)
        assert session.verify_password(pw, h, salt)
        assert not session.verify_password(pw + "x", h, salt)
