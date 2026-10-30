"""Cross-component regressions found during the September 2026 audit."""

import io
import sqlite3

import pytest
from fastapi.testclient import TestClient

from core import calibrate, db, discover, firecrawl, linkcheck, prefilter
from core import scheduler, semantic, session
from web.app import create_app
from web import intake_routes


@pytest.fixture
def audit_db(tmp_path, monkeypatch):
    path = str(tmp_path / "audit.db")
    monkeypatch.setenv("CAREERLOOP_SECRET", "audit-only-secret")
    conn = db.connect(path)
    uid, _ = session.create_account(conn, "audit", "pw")
    db.save_profile(conn, uid, {
        "target": {"role_family": "Manager"}, "search_terms": ["Manager"],
        "weights": {"trajectory_fit": 60, "company_quality": 32,
                    "experience_fit": 8}, "hard_filters": {}})
    yield conn, uid, path
    conn.close()


def listing(n=1, **fields):
    return dict({"url": f"https://jobs.example/{n}", "title": "Manager",
                 "company": "Example", "location": "Mumbai",
                 "description": "Manage a team", "source": "greenhouse"},
                **fields)


def stub_scan(monkeypatch, rows):
    monkeypatch.setattr(discover, "discover_all", lambda *a, **k: (rows, []))
    monkeypatch.setattr(prefilter, "prefilter", lambda jobs, p, **kw: jobs[:kw.get("top_n", 40)])
    monkeypatch.setattr(linkcheck, "sweep", lambda *a, **k: (0, 0))
    monkeypatch.setattr(firecrawl, "enrich_descriptions", lambda *a, **k: None)
    monkeypatch.setattr(scheduler.notify, "send_scan_summary", lambda *a, **k: True)


@pytest.mark.parametrize("first_run", ["dry", "failed", "new_user"])
def test_stored_but_unscored_jobs_are_recoverable(audit_db, monkeypatch, first_run):
    conn, uid, path = audit_db
    rows = [listing()]
    stub_scan(monkeypatch, rows)
    monkeypatch.setattr(scheduler.score, "score_jobs", lambda *a, **k: [])
    if first_run == "new_user":
        db.upsert_job(conn, rows[0])
        conn.commit()
    else:
        scheduler.Scanner(db_path=path, dry=first_run == "dry")._scan(conn, "manual")
    # The provider need not rediscover the listing: paid delta searches may not.
    rows.clear()
    monkeypatch.setattr(scheduler.score, "score_jobs",
                        lambda jobs, p: [dict(j, score=8) for j in jobs])
    scheduler.Scanner(db_path=path)._scan(conn, "manual")
    assert len(db.list_user_jobs(conn, uid)) == 1


def test_scored_rows_do_not_consume_the_shortlist(audit_db, monkeypatch):
    conn, uid, path = audit_db
    rows = [listing(n) for n in range(41)]
    ids = {}
    for j in rows:
        ids[j["url"]] = db.upsert_job(conn, j)
    for j in rows[:40]:
        db.upsert_user_job(conn, uid, ids[j["url"]], {"score": 8})
    stub_scan(monkeypatch, [])
    monkeypatch.setattr(scheduler.score, "score_jobs",
                        lambda jobs, p: [dict(j, score=8) for j in jobs])
    scheduler.Scanner(db_path=path)._score_for_user(
        conn, db.get_user(conn, uid), db.load_profile(conn, uid), rows, ids, "manual")
    assert len(db.list_user_jobs(conn, uid)) == 41


def test_portal_dedup_preserves_distinct_cities(audit_db):
    conn, _, path = audit_db
    rows = [listing(1), listing(2, location="Pune")]
    assert len(scheduler.Scanner(db_path=path)._dedup(conn, rows)) == 2


def test_career_page_roles_stay_distinct_in_storage(audit_db, monkeypatch):
    conn, _, _ = audit_db
    monkeypatch.setattr(firecrawl, "available", lambda: True)
    monkeypatch.setattr(firecrawl, "_post", lambda *a: {
        "data": {"json": {"jobs": [{"title": "Designer"}, {"title": "Manager"}]}}})
    rows = firecrawl.scrape_career_pages({"career_pages": ["https://example.com/careers"]})
    assert len({db.upsert_job(conn, j) for j in rows}) == 2


def test_exhausted_shared_budget_does_not_start_naukri(monkeypatch):
    monkeypatch.setattr(discover, "_fetch_ats", lambda *a: [])
    monkeypatch.setattr(discover, "apify_available", lambda: True)
    monkeypatch.setattr(discover, "linkedin_cost", lambda *a: 1.0)
    monkeypatch.setattr(discover, "from_linkedin", lambda *a: [listing()])
    called = []
    monkeypatch.setattr(discover, "from_apify", lambda *a: called.append(True) or [])
    discover.discover_all({
        "jobspy": {"enabled": False, "search_terms": ["Manager"]},
        "linkedin": {"enabled": True, "max_spend_per_scan_usd": 1},
        "apify": {"enabled": True, "max_spend_per_scan_usd": 1}})
    assert called == []


def test_link_checks_rotate_past_live_and_indeterminate_pages(audit_db, monkeypatch):
    conn, uid, _ = audit_db
    for n in range(3):
        jid = db.upsert_job(conn, listing(n))
        db.upsert_user_job(conn, uid, jid, {"score": 8})
    checked = []
    monkeypatch.setattr(linkcheck, "_verdict",
                        lambda url: (checked.append(url) or False, "live or unavailable"))
    for _ in range(3):
        linkcheck.sweep(conn, limit=1, verbose=False)
    assert len(set(checked)) == 3


def test_semantic_ranking_never_downloads_an_uncached_model(monkeypatch):
    monkeypatch.setattr(semantic, "available", lambda: False)
    def forbidden():
        pytest.fail("a scan attempted to load an unavailable model")
    monkeypatch.setattr(semantic, "_load", forbidden)
    assert semantic.rank([listing()], {"target": {"role_family": "Manager"}}) is None


def test_both_targets_reach_search_and_keyword_fallback(monkeypatch):
    profile = {"target": {"role_family": "Designer"},
               "leverage_target": {"role_family": "Accountant"}}
    calibrate.attach_search_terms(profile, None)
    assert "Accountant" in profile["search_terms"]
    profile["search_terms"] = []
    monkeypatch.setattr(semantic, "rank", lambda *a: None)
    assert prefilter.prefilter([listing(title="Accountant")], profile)


def test_export_obeys_the_selected_track(audit_db):
    import openpyxl
    conn, uid, path = audit_db
    for n, track in enumerate(("ideal", "leverage", "both")):
        jid = db.upsert_job(conn, listing(n, title=track))
        db.upsert_user_job(conn, uid, jid, {"score": 8, "track": track})
    client = TestClient(create_app(boot_scan=False, db_path=path))
    client.cookies.set(session.COOKIE_NAME, session.issue_session(uid))
    response = client.get("/export.xlsx?track=ideal")
    workbook = openpyxl.load_workbook(io.BytesIO(response.content))
    values = list(workbook.active.values)
    assert len(values) == 3  # header + ideal + both


def test_intake_uses_saved_cv_and_refreshes_changed_targets(audit_db, monkeypatch):
    conn, uid, path = audit_db
    profile = db.load_profile(conn, uid)
    profile["resume_text"] = "A concrete record of previous work."
    db.save_profile(conn, uid, profile)
    seen = []
    def compile_profile(parsed, answers, save):
        seen.append(parsed)
        return {"target": {"role_family": "Designer"},
                "leverage_target": {"role_family": "Accountant"}}
    monkeypatch.setattr(intake_routes.intake, "compile_profile", compile_profile)
    monkeypatch.setattr(intake_routes, "load_schema", lambda: {
        "dimensions": [{"id": "current_role", "ask": "Current role?"}]})
    client = TestClient(create_app(boot_scan=False, db_path=path))
    client.cookies.set(session.COOKIE_NAME, session.issue_session(uid))
    response = client.post("/intake", files={
        "csv_file": ("answers.csv", b"Current role?\nAccountant\n", "text/csv")},
        follow_redirects=False)
    assert "Profile+compiled" in response.headers["location"]
    assert seen[0]["resume_text"] == profile["resume_text"]
    saved = db.load_profile(conn, uid)
    assert saved["search_terms"] == ["Designer", "Accountant"]
    assert saved["resume_text"] == profile["resume_text"]


def test_failed_migration_is_atomic_and_retryable(tmp_path, monkeypatch):
    path = str(tmp_path / "migration.db")
    migrations = ["CREATE TABLE example (id INTEGER);",
                  "ALTER TABLE example ADD COLUMN value TEXT; INVALID SQL;"]
    monkeypatch.setattr(db, "MIGRATIONS", migrations)
    with pytest.raises(sqlite3.OperationalError):
        db.connect(path)
    migrations[1] = "ALTER TABLE example ADD COLUMN value TEXT;"
    conn = db.connect(path)
    try:
        assert [r["name"] for r in conn.execute("PRAGMA table_info(example)")] == ["id", "value"]
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 2
    finally:
        conn.close()


def test_failed_board_write_rolls_back_even_with_listing_ratings(audit_db, monkeypatch):
    conn, uid, path = audit_db
    rows = [listing(1, company_rating=4.2), listing(2)]
    ids = {j["url"]: db.upsert_job(conn, j) for j in rows}
    conn.commit()
    stub_scan(monkeypatch, [])
    monkeypatch.setattr(scheduler.score, "score_jobs", lambda jobs, p: [
        dict(jobs[0], score=8), dict(jobs[1], score=7, verdict={"wrong": "type"})])
    scheduler.Scanner(db_path=path)._score_for_user(
        conn, db.get_user(conn, uid), db.load_profile(conn, uid), rows, ids, "manual")
    assert db.recent_runs(conn, uid)[0]["status"] == "error"
    pending = db.list_user_jobs(conn, uid)
    assert len(pending) == 2
    assert all(j["evaluation"]["rating_provider"] == "local" for j in pending)


def test_failed_scoring_keeps_firecrawl_enrichment_for_retry(audit_db, monkeypatch):
    conn, uid, path = audit_db
    rows = [listing(source="firecrawl", needs_enrichment=True)]
    stub_scan(monkeypatch, rows)
    enriched = []
    def enrich(jobs, cfg, dry=False):
        for job in jobs:
            if job.get("needs_enrichment"):
                enriched.append(job["url"])
                job.update(description="The complete role description", needs_enrichment=False)
    monkeypatch.setattr(firecrawl, "enrich_descriptions", enrich)
    monkeypatch.setattr(scheduler.score, "score_jobs", lambda *a: [])
    scheduler.Scanner(db_path=path)._scan(conn, "manual")
    rows.clear()
    monkeypatch.setattr(scheduler.score, "score_jobs",
                        lambda jobs, p: [dict(j, score=8) for j in jobs])
    scheduler.Scanner(db_path=path)._scan(conn, "manual")
    assert enriched == [listing()["url"]]
    assert len(db.list_user_jobs(conn, uid)) == 1
