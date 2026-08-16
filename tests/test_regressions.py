"""
REGRESSIONS — one test per bug that was actually in this codebase.

Every case below is a defect that shipped, was found by reading the
code, and is fixed. They live together because what they have in common
is not a layer but a failure mode: each one was silent. Nothing raised,
nothing logged, and the wrong thing appeared on someone's screen — a
date a day off, a scan that quietly did nothing, another user's verdict
on your listing.

That is the class of bug a test suite has to hold down, because the
running system will not complain about any of them.
"""

import json
import urllib.error

import pytest
from fastapi.testclient import TestClient

from core import db, discover, llm, publish, scheduler, session
from web.app import create_app
from web.render import safe_url


# ============================================================
# Timestamps — scraped listings vs. everything else
# ============================================================
def test_scraped_timestamps_are_utc_like_every_other_row():
    """
    discover wrote `datetime.now().isoformat()`: local time, no offset.

    db.parse_ts treats a naive value as UTC, so a listing scraped at
    21:00 IST was read back as 15:30 UTC the NEXT day and the board
    printed tomorrow's date on it.
    """
    ts = db.parse_ts(discover._now())
    assert ts is not None and ts.tzinfo is not None
    drift = abs((ts - db.parse_ts(db.now())).total_seconds())
    assert drift < 5, "scraped and stored timestamps disagree"


def test_the_found_date_filter_compares_like_with_like():
    """
    The `days` filter is a string comparison against a UTC bound. A local
    naive timestamp sorts against it by accident most of the day and
    wrongly around the date boundary, so both sides must be the same
    shape: a listing found now is inside "the last day" and outside "the
    last zero days".
    """
    from web.jobs import _since
    now = discover._now()
    assert now > _since(1), "a listing found now is not in the last 24h"
    assert now <= _since(0), "a listing found now is somehow in the future"


# ============================================================
# JobSpy config — user-edited YAML
# ============================================================
def test_a_config_missing_keys_does_not_take_down_the_scan(monkeypatch):
    """cfg["hours_old"] raised KeyError out of discover_all, which killed
    the ATS tier that had already succeeded."""
    seen = {}

    def fake_scrape(**kw):
        seen.update(kw)
        raise RuntimeError("stop here — the call shape is the assertion")

    monkeypatch.setitem(__import__("sys").modules, "jobspy",
                        type("m", (), {"scrape_jobs": staticmethod(fake_scrape)}))
    rows = discover.from_jobspy({"search_terms": ["pm"], "locations": ["Pune"]})
    assert rows == []
    assert seen["hours_old"] and seen["country_indeed"]


def test_no_locations_still_runs_one_search(monkeypatch):
    """An empty location list skipped the inner loop entirely: zero rows,
    no error, indistinguishable from a quiet market."""
    calls = []
    monkeypatch.setitem(
        __import__("sys").modules, "jobspy",
        type("m", (), {"scrape_jobs": staticmethod(
            lambda **kw: calls.append(kw) or (_ for _ in ()).throw(
                RuntimeError("no network in tests")))}))
    discover.from_jobspy({"search_terms": ["pm"], "locations": []})
    assert len(calls) == 1 and calls[0]["location"] is None


# ============================================================
# The LLM caller — a scan is ~25 sequential calls
# ============================================================
class _Resp:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, body=b'{"error":{"message":"nope"}}'):
    import io
    return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(body))


def test_a_rate_limit_is_retried_not_lost(monkeypatch):
    """One 429 used to drop a whole batch of eight listings, silently."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    attempts = []

    def fake_open(req, timeout=None):
        attempts.append(1)
        if len(attempts) < 3:
            raise _http_error(429)
        return _Resp({"content": [{"type": "text", "text": "ok"}],
                      "usage": {"input_tokens": 1}})

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_open)
    text, usage = llm.call_llm_usage("s", "p")
    assert text == "ok" and len(attempts) == 3


def test_a_bad_request_fails_immediately_with_the_reason(monkeypatch):
    """400 is a bug in the request. Retrying spends the money twice, and
    `HTTP Error 400: Bad Request` names none of the twelve causes."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    calls = []

    def fake_open(req, timeout=None):
        calls.append(1)
        raise _http_error(400, b'{"error":{"message":"max_tokens too large"}}')

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_open)
    with pytest.raises(llm.LLMError) as err:
        llm.call_llm_usage("s", "p")
    assert len(calls) == 1
    assert "max_tokens too large" in str(err.value)


def test_an_empty_reply_says_so_instead_of_a_json_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(
        llm.urllib.request, "urlopen",
        lambda req, timeout=None: _Resp({"content": [], "usage": {},
                                         "stop_reason": "refusal"}))
    with pytest.raises(llm.LLMError) as err:
        llm.call_llm_usage("s", "p", as_json=True)
    assert "no text" in str(err.value)


def test_a_missing_key_is_reported_before_any_request(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(llm.LLMError):
        llm.call_llm_usage("s", "p")


# ============================================================
# Link safety — every URL here is scraped or LLM-invented
# ============================================================
@pytest.mark.parametrize("evil", [
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "  javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
])
def test_a_hostile_scheme_never_becomes_a_link(evil):
    assert safe_url(evil) == ""
    assert publish._href(evil) == ""

    page = publish.render("X", {}, [{"title": "T", "company": "C",
                                     "url": evil, "score": 8.0,
                                     "score_breakdown": {}}],
                          {"count": 0})
    assert "javascript:" not in page.lower()
    assert "Open the listing" not in page


@pytest.mark.parametrize("good", ["https://x.test/1", "http://x.test/1"])
def test_ordinary_links_are_untouched(good):
    assert safe_url(good) == good
    assert publish._href(good) == good


# ============================================================
# Redirects — a raw exception is not a user-facing message
# ============================================================
def test_a_multi_line_error_becomes_one_readable_flash(tmp_path, monkeypatch):
    """
    Not a security fix, and worth saying so: Starlette percent-encodes
    the Location value, so a newline was never header injection, and
    Jinja escapes the flash, so it was never XSS.

    What it was, was unreadable — a 4KB API error body pasted whole into
    the URL and rendered as one run-on line with %0A in it. The message
    is capped and flattened; this pins that, and pins that the redirect
    still lands on the job it failed on.
    """
    monkeypatch.setenv("CAREERLOOP_SECRET", "test-secret")
    path = str(tmp_path / "r.db")
    conn = db.connect(path)
    uid, _ = session.create_account(conn, "alice", "pw")
    db.save_profile(conn, uid, {"target": {}, "hard_filters": {},
                                "weights": {"trajectory_fit": 100},
                                "dimension_definitions": {}})
    jid = db.upsert_job(conn, {"url": "https://x/1", "title": "T",
                               "company": "C", "found_at": db.now()})
    db.upsert_user_job(conn, uid, jid, {"score": 8.0})
    conn.close()

    from core import tailor
    monkeypatch.setattr(tailor, "prepare_application", lambda j, p: (_ for _ in ()).throw(
        RuntimeError("HTTP 400:\n{\"error\": \"bad\"}\nline three")))

    with TestClient(create_app(boot_scan=False, db_path=path)) as c:
        c.post("/login", data={"username": "alice", "password": "pw"})
        r = c.post(f"/jobs/{jid}/prepare", follow_redirects=False)

    assert r.status_code == 303
    location = r.headers["location"]
    assert "\n" not in location and "%0A" not in location
    assert len(location) < 400
    assert location.startswith(f"/jobs/{jid}?msg=")


# ============================================================
# Storage — connect() runs on every single request
# ============================================================
def test_an_already_migrated_database_is_not_written_to_on_connect(tmp_path):
    """
    init_db did CREATE TABLE IF NOT EXISTS plus a commit every time,
    taking a write lock to discover there was nothing to write — while
    the boot scan held that lock.
    """
    path = str(tmp_path / "m.db")
    db.connect(path).close()

    conn = db.get_conn(path)
    statements = []
    conn.set_trace_callback(statements.append)
    db.init_db(conn)
    conn.set_trace_callback(None)
    conn.close()

    written = [s for s in statements
               if s.strip().split()[0].upper() in ("CREATE", "INSERT",
                                                   "UPDATE", "COMMIT")]
    assert written == [], f"connect() still writes: {written}"


def test_migrations_still_run_on_a_fresh_database(tmp_path):
    conn = db.connect(str(tmp_path / "fresh.db"))
    try:
        version = conn.execute(
            "SELECT MAX(version) v FROM schema_version").fetchone()["v"]
        assert version == len(db.MIGRATIONS)
        conn.execute("SELECT COUNT(*) FROM user_jobs")     # schema is real
    finally:
        conn.close()


# ============================================================
# Isolation — the scrape is shared, the opinions are not
# ============================================================
def test_one_users_score_never_rides_along_on_anothers_listing(tmp_path,
                                                               monkeypatch):
    """
    The scrape is shared and score_jobs writes its verdict onto the dicts
    it is given, so passing the originals meant the second user's board
    inherited the first user's score and angle for any listing their own
    batch failed to score.
    """
    conn = db.connect(str(tmp_path / "iso.db"))
    a, _ = session.create_account(conn, "alice", "pw")
    b, _ = session.create_account(conn, "bob", "pw")
    profile = {"target": {"role_family": "Manager"},
               "hard_filters": {}, "weights": {"trajectory_fit": 100},
               "dimension_definitions": {}, "search_terms": ["Manager"]}
    db.save_profile(conn, a, profile)
    db.save_profile(conn, b, profile)

    fresh = [{"url": "https://x/1", "title": "Manager", "company": "Acme",
              "description": "d", "found_at": db.now()}]
    job_ids = {"https://x/1": db.upsert_job(conn, fresh[0])}

    scored_for = {}

    def fake_score(jobs, prof):
        # Alice's batch succeeds; Bob's returns nothing, as a failed
        # batch does. Bob must end up with no row, not Alice's row.
        who = len(scored_for)
        scored_for[who] = jobs
        if who == 0:
            jobs[0].update({"score": 9.9, "verdict": "alice only",
                            "angle": "alice angle", "score_breakdown": {}})
            return jobs
        return []

    monkeypatch.setattr(scheduler.score, "score_jobs", fake_score)
    monkeypatch.setattr(scheduler.reviews, "enrich_from_listing",
                        lambda *a, **k: None)
    monkeypatch.setattr(scheduler.reviews, "enrich_top_companies",
                        lambda *a, **k: 0)
    monkeypatch.setattr(scheduler.notify, "send_scan_summary",
                        lambda *a, **k: False)
    monkeypatch.setattr(scheduler.firecrawl, "enrich_descriptions",
                        lambda jobs, cfg, dry=False: jobs)

    scanner = scheduler.Scanner(db_path=str(tmp_path / "iso.db"))
    for user in db.all_users(conn):
        scanner._score_for_user(conn, user, profile, fresh, job_ids, "test")

    assert db.list_user_jobs(conn, a)[0]["verdict"] == "alice only"
    assert db.list_user_jobs(conn, b) == []
    assert "score" not in fresh[0], "the shared scrape was mutated"
    conn.close()


# ============================================================
# Employer reviews — the feature that never ran
# ============================================================
def test_a_rating_carried_from_a_listing_does_not_block_the_real_summary(
        tmp_path, monkeypatch):
    """
    enrich_from_listing stores the bare AmbitionBox number a Naukri
    listing carries, and that write set fetched_at. company_is_fresh then
    answered True for thirty days, so enrich_top_companies skipped the
    company — and the bias-corrected summary, which is the entire reason
    core/reviews.py exists, was never fetched for any company the primary
    source found. Zero API calls, no error, an empty section on the page.
    """
    from core import reviews
    conn = db.connect(str(tmp_path / "rev.db"))
    try:
        reviews.enrich_from_listing(conn, {"company": "Acme",
                                           "company_rating": 3.9,
                                           "company_review_count": 812})
        assert db.company_is_fresh(conn, "Acme", 30), "row is fresh…"
        assert not reviews.has_fresh_summary(conn, "Acme"), "…but not summarised"

        calls = []
        monkeypatch.setattr(reviews, "available", lambda: True)
        monkeypatch.setattr(reviews, "fetch_ratings", lambda c: calls.append(c) or
                            {"rating": 3.9, "breakdown": {"5": 400}})
        monkeypatch.setattr(reviews, "fetch_reviews",
                            lambda c, limit=60: [{"pros": "p"}])
        monkeypatch.setattr(reviews, "summarize", lambda *a, **k: {
            "rating": 3.9, "sector_context": "above the sector median",
            "confidence": "high"})

        assert reviews.enrich_top_companies(conn, [{"company": "Acme"}]) == 1
        assert calls == ["Acme"]
        row = db.get_company(conn, "Acme")
        assert row["review_summary"]["sector_context"]

        # And the cache still has to work, or every scan pays again.
        calls.clear()
        assert reviews.enrich_top_companies(conn, [{"company": "Acme"}]) == 0
        assert calls == []
    finally:
        conn.close()


def test_a_later_listing_does_not_erase_what_was_fetched(tmp_path):
    """save_company writes every column, so carrying a rating across used
    to blank the star breakdown and the sector median with it."""
    from core import reviews
    conn = db.connect(str(tmp_path / "rev2.db"))
    try:
        db.save_company(conn, "Acme", rating=3.8, review_count=100,
                        rating_breakdown={"5": 60, "1": 10},
                        sector_median_rating=3.6)
        reviews.enrich_from_listing(conn, {"company": "Acme",
                                           "company_rating": 4.1})
        row = db.get_company(conn, "Acme")
        assert row["rating"] == 4.1, "the newer rating should win"
        assert row["rating_breakdown"] == {"5": 60, "1": 10}
        assert row["sector_median_rating"] == 3.6
    finally:
        conn.close()


def test_a_scan_with_no_api_key_is_an_error_not_a_quiet_market(tmp_path,
                                                               monkeypatch):
    """
    score_jobs catches per batch, so one failure cannot lose the rest.
    With no API key every batch fails, and the run was still booked
    "ok, 0 found" — a green header, a started cooldown, and no hint that
    the single most important setting was missing.
    """
    path = str(tmp_path / "nokey.db")
    conn = db.connect(path)
    try:
        uid, _ = session.create_account(conn, "alice", "pw")
        profile = {"target": {"role_family": "Manager"}, "hard_filters": {},
                   "weights": {"trajectory_fit": 100},
                   "dimension_definitions": {}, "search_terms": ["Manager"]}
        db.save_profile(conn, uid, profile)
        fresh = [{"url": "https://x/1", "title": "Manager", "company": "Acme",
                  "description": "d", "found_at": db.now()}]
        job_ids = {"https://x/1": db.upsert_job(conn, fresh[0])}

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(scheduler.firecrawl, "enrich_descriptions",
                            lambda jobs, cfg, dry=False: jobs)

        scanner = scheduler.Scanner(db_path=path)
        scanner._score_for_user(conn, db.all_users(conn)[0], profile,
                                fresh, job_ids, "manual")

        run = db.recent_runs(conn, uid)[0]
        assert run["status"] == "error"
        assert "ANTHROPIC_API_KEY" in run["error_text"]
        assert db.last_successful_run(conn) is None
        assert scanner.cooldown_remaining(conn) == 0
    finally:
        conn.close()


# ============================================================
# A dry run is a rehearsal, not a scan
# ============================================================
def test_a_dry_run_does_not_start_the_three_hour_cooldown(tmp_path):
    """
    `--dry` is the command you run because you do not want to spend
    anything. Booking it as a successful run meant it satisfied
    last_successful_run, so the next real scan was suppressed for three
    hours and the header reported the rehearsal as the last scan.
    """
    path = str(tmp_path / "dry.db")
    conn = db.connect(path)
    try:
        uid, _ = session.create_account(conn, "alice", "pw")
        run_id = db.start_run(conn, uid, "catchup")
        db.finish_run(conn, run_id, "dry", 0)

        assert db.last_successful_run(conn) is None
        assert scheduler.Scanner(db_path=path).cooldown_remaining(conn) == 0
        assert db.recent_runs(conn, uid)[0]["status"] == "dry"
    finally:
        conn.close()


# ============================================================
# The list page renders every row it is handed
# ============================================================
def test_the_job_list_is_capped_but_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERLOOP_SECRET", "test-secret")
    from web import jobs as jobs_routes
    monkeypatch.setattr(jobs_routes, "LIST_LIMIT", 5)

    path = str(tmp_path / "cap.db")
    conn = db.connect(path)
    uid, _ = session.create_account(conn, "alice", "pw")
    db.save_profile(conn, uid, {"target": {}, "hard_filters": {},
                                "weights": {"trajectory_fit": 100},
                                "dimension_definitions": {}})
    for i in range(12):
        jid = db.upsert_job(conn, {"url": f"https://x/{i}", "title": f"T{i}",
                                   "company": "C", "found_at": db.now()})
        db.upsert_user_job(conn, uid, jid, {"score": 5.0 + i / 10})
    conn.close()

    with TestClient(create_app(boot_scan=False, db_path=path)) as c:
        c.post("/login", data={"username": "alice", "password": "pw"})
        html = c.get("/jobs").text

    assert html.count('class="entry__title"') == 5
    assert "narrow the filters" in html


# ============================================================
# Telegram — the column existed with no way to fill it
# ============================================================
def test_a_chat_id_can_actually_be_set(tmp_path, monkeypatch, capsys):
    """
    Seeding leaves telegram_chat_id NULL and the scanner refuses to fall
    back to the environment's chat id — correctly, since that is how one
    person's roles reach another person's phone. So every digest was
    skipped, for everyone, and nothing in the tool could change it.
    """
    import run as cli
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "tg.db"))

    conn = db.connect(str(tmp_path / "tg.db"))
    session.create_account(conn, "alice", "pw")
    conn.close()

    cli.cmd_set_telegram(["--user", "alice", "--chat", "12345"])

    conn = db.connect(str(tmp_path / "tg.db"))
    try:
        assert db.get_user_by_name(conn, "alice")["telegram_chat_id"] == "12345"
        cli.cmd_set_telegram(["--user", "alice", "--clear"])
        assert db.get_user_by_name(conn, "alice")["telegram_chat_id"] is None
    finally:
        conn.close()


# ============================================================
# Encoding — this runs on Windows, where the default is cp1252
# ============================================================
def test_a_profile_with_non_ascii_survives_a_round_trip(tmp_path,
                                                        monkeypatch):
    """A CV with a rupee sign or an accented name raised
    UnicodeDecodeError on every open() that had no encoding."""
    from core import intake
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(intake, "PROFILE_PATH", str(tmp_path / "p.json"))

    profile = {"target": {"role_family": "Brand Manager — ₹25L+",
                          "locations": ["Bengaluru"]},
               "angles": ["Café rollout across 12 cities"],
               "weights": {"trajectory_fit": 100},
               "dimension_definitions": {}}
    with open(intake.PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False)

    loaded = intake.load_profile()
    assert loaded["target"]["role_family"].endswith("₹25L+")
    assert "Café" in loaded["angles"][0]
