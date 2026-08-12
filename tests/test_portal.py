"""
PHASE 2 CHECKPOINT — the board renders real jobs, ignore and restore
work, and the breakdown shows this user's own dimension names.

Also proves the routes enforce the isolation the db layer provides:
a signed-in user hitting another user's job id by hand gets nothing.
"""

import pytest
from fastapi.testclient import TestClient

from core import db, session
from web.app import create_app


@pytest.fixture
def app_db(tmp_path, monkeypatch, brand_profile, ai_pm_profile):
    path = str(tmp_path / "portal.db")
    monkeypatch.setenv("CAREERLOOP_SECRET", "test-secret-not-a-password")

    conn = db.connect(path)
    a, _ = session.create_account(conn, "alice", "pw-alice")
    b, _ = session.create_account(conn, "bob", "pw-bob")
    db.save_profile(conn, a, brand_profile)
    db.save_profile(conn, b, ai_pm_profile)

    shared = db.upsert_job(conn, {
        "url": "https://x.test/shared", "title": "Brand Manager",
        "company": "Acme", "location": "Mumbai", "source": "greenhouse",
        "description": "Own the brand.", "found_at": db.now()})
    b_only = db.upsert_job(conn, {
        "url": "https://x.test/bonly", "title": "AI PM", "company": "Zeta",
        "source": "lever", "found_at": db.now()})

    db.upsert_user_job(conn, a, shared, {
        "score": 8.2, "verdict": "Moves you into brand ownership.",
        "angle": "Six years selling it face to face.", "stretch": True,
        "risk": "The team is two people.",
        "score_breakdown": {"trajectory_fit": 9, "creative_ownership": 8,
                            "brand_scale": 4, "company_quality": 7,
                            "experience_fit": 6}})
    db.upsert_user_job(conn, b, b_only, {"score": 7.1, "verdict": "B's job"})
    conn.close()

    return path, a, b, shared, b_only


@pytest.fixture
def client(app_db):
    path = app_db[0]
    app = create_app(boot_scan=False, db_path=path)
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    r = client.post("/login", data={"username": username,
                                    "password": password},
                    follow_redirects=False)
    assert r.status_code == 303
    return r


# ------------------------------------------------------------
# Auth gate
# ------------------------------------------------------------
def test_every_route_requires_a_session(client):
    for path in ["/", "/jobs", "/ignored", "/applications", "/intake",
                 "/jobs/1", "/export.xlsx"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", \
            f"{path} was reachable without signing in"


def test_the_stylesheet_loads_without_a_session(client):
    """/static is the only prefix exemption — the sign-in page needs it."""
    r = client.get("/static/careerloop.css")

    assert r.status_code == 200
    assert "--paper" in r.text


def test_the_static_exemption_does_not_leak_anything_else(client):
    """A path merely starting with the word static must still be gated."""
    for path in ["/staticky", "/static", "/jobs/1/../../static"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (303, 404), f"{path} was unexpectedly served"


def test_a_wrong_password_does_not_sign_you_in(client):
    r = client.post("/login", data={"username": "alice", "password": "nope"},
                    follow_redirects=False)
    assert r.headers["location"] == "/login?error=1"
    assert session.COOKIE_NAME not in r.cookies


def test_logout_clears_the_session(client):
    login(client, "alice", "pw-alice")
    client.get("/logout", follow_redirects=False)
    assert client.get("/", follow_redirects=False).status_code == 303


# ------------------------------------------------------------
# Board
# ------------------------------------------------------------
def test_the_board_renders_real_jobs(client):
    login(client, "alice", "pw-alice")
    html = client.get("/").text

    assert "Brand Manager" in html
    assert "Acme" in html
    assert "8.2" in html
    assert "stretch" in html


def test_the_board_shows_the_score_distribution(client):
    login(client, "alice", "pw-alice")
    html = client.get("/").text

    assert "Score distribution" in html
    assert "no fixed pass mark" in html


def test_the_board_is_not_empty_just_because_scores_are_low(app_db):
    """A flat first run must still produce a board."""
    path, a, _, _, _ = app_db
    conn = db.connect(path)
    job = db.upsert_job(conn, {"url": "https://x.test/low", "title": "Low",
                               "company": "Meh", "found_at": db.now()})
    db.upsert_user_job(conn, a, job, {"score": 5.8})
    conn.close()

    app = create_app(boot_scan=False, db_path=path)
    with TestClient(app) as c:
        login(c, "alice", "pw-alice")
        assert "Low" in c.get("/").text


def test_jobs_below_the_absolute_floor_are_kept_off_the_board(app_db):
    path, a, _, _, _ = app_db
    conn = db.connect(path)
    job = db.upsert_job(conn, {"url": "https://x.test/junk",
                               "title": "Obvious Junk", "company": "Spam",
                               "found_at": db.now()})
    db.upsert_user_job(conn, a, job, {"score": 1.2})
    conn.close()

    app = create_app(boot_scan=False, db_path=path)
    with TestClient(app) as c:
        login(c, "alice", "pw-alice")
        assert "Obvious Junk" not in c.get("/").text


# ------------------------------------------------------------
# Detail page
# ------------------------------------------------------------
def test_the_breakdown_uses_this_users_own_dimension_names(client, app_db):
    _, _, _, shared, _ = app_db
    login(client, "alice", "pw-alice")
    html = client.get(f"/jobs/{shared}").text

    assert "Creative ownership" in html
    assert "Brand scale" in html
    assert "Ai product depth" not in html, \
        "another user's dimension leaked onto this page"


def test_the_detail_page_always_argues_both_ways(client, app_db):
    _, _, _, shared, _ = app_db
    login(client, "alice", "pw-alice")
    html = client.get(f"/jobs/{shared}").text

    assert "Why it fits" in html
    assert "Why it might not" in html
    assert "The team is two people." in html
    # Weakest dimension is named explicitly, not just drawn shorter.
    assert "Weakest on brand scale" in html


def test_a_job_belonging_to_another_user_is_not_reachable_by_id(
        client, app_db):
    _, _, _, _, b_only = app_db
    login(client, "alice", "pw-alice")

    r = client.get(f"/jobs/{b_only}", follow_redirects=False)
    assert r.status_code == 303
    assert "Not+found" in r.headers["location"]


def test_posting_an_action_against_another_users_job_does_nothing(
        client, app_db):
    path, _, b, _, b_only = app_db
    login(client, "alice", "pw-alice")

    client.post(f"/jobs/{b_only}/ignore", data={"reason": "x", "back": "/"},
                follow_redirects=False)
    client.post(f"/jobs/{b_only}/status", data={"status": "applied",
                                                "back": "/"},
                follow_redirects=False)

    conn = db.connect(path)
    try:
        assert db.get_user_job(conn, b, b_only)["status"] == "new"
    finally:
        conn.close()


# ------------------------------------------------------------
# Ignore / restore
# ------------------------------------------------------------
def test_ignore_removes_from_the_board_and_restore_brings_it_back(
        client, app_db):
    _, _, _, shared, _ = app_db
    login(client, "alice", "pw-alice")

    client.post(f"/jobs/{shared}/ignore",
                data={"reason": "too small a brand", "back": "/"})
    assert "Brand Manager" not in client.get("/").text

    ignored = client.get("/ignored").text
    assert "Brand Manager" in ignored
    assert "too small a brand" in ignored

    client.post(f"/jobs/{shared}/restore", data={"back": "/ignored"})
    assert "Brand Manager" in client.get("/").text


def test_moving_a_job_between_columns(client, app_db):
    _, _, _, shared, _ = app_db
    login(client, "alice", "pw-alice")

    client.post(f"/jobs/{shared}/status",
                data={"status": "interested", "back": "/"})
    html = client.get("/").text
    assert "Interested" in html and "Brand Manager" in html


# ------------------------------------------------------------
# List, filters, export
# ------------------------------------------------------------
def test_filters_narrow_the_list_without_crossing_users(client):
    login(client, "alice", "pw-alice")

    assert "Brand Manager" in client.get("/jobs?min_score=8").text
    assert "Brand Manager" not in client.get("/jobs?min_score=9.5").text
    assert "Brand Manager" in client.get("/jobs?company=acme").text
    assert "AI PM" not in client.get("/jobs?q=AI").text


def test_export_respects_the_current_filters(client):
    login(client, "alice", "pw-alice")

    r = client.get("/export.xlsx?min_score=8")
    assert r.status_code == 200
    assert len(r.content) > 0

    empty = client.get("/export.xlsx?min_score=9.9")
    assert empty.status_code == 200


def test_intake_page_lists_this_users_dimensions_and_the_form_spec(client):
    login(client, "alice", "pw-alice")
    html = client.get("/intake").text

    assert "Creative ownership" in html
    assert "Google Form spec" in html
    assert client.get("/intake/form-spec.txt").status_code == 200


# ------------------------------------------------------------
# Dry run
# ------------------------------------------------------------
def test_dry_mode_makes_no_llm_calls_from_the_prepare_button(app_db):
    path, _, _, shared, _ = app_db
    app = create_app(boot_scan=False, dry=True, db_path=path)

    with TestClient(app) as c:
        login(c, "alice", "pw-alice")
        r = c.post(f"/jobs/{shared}/prepare", follow_redirects=False)
        assert "Dry+run" in r.headers["location"]

    conn = db.connect(path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) n FROM applications").fetchone()["n"] == 0
    finally:
        conn.close()
