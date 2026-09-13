"""
ADVERSARIAL TESTS — round two, the web layer.

Everything here targets the boundary between a request and the app:
form fields that get echoed into a Location header, CSV that does not
match its own header row, and scraped text on its way into a
spreadsheet. These are the paths where hostile or merely malformed
input becomes someone else's problem.
"""

import io

import pytest
from fastapi.testclient import TestClient

from core import db, session
from web import export
from web.app import create_app
from web.intake_routes import load_schema, match_answers

HOSTILE = 'Dev <script>alert(1)</script> & "C++"'


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERLOOP_SECRET", "test-secret")
    path = str(tmp_path / "web.db")
    conn = db.connect(path)
    uid, _ = session.create_account(conn, "alice", "pw")
    db.save_profile(conn, uid, {
        "target": {"role_family": "Manager"}, "hard_filters": {},
        "weights": {"trajectory_fit": 60, "company_quality": 32,
                    "experience_fit": 8},
        "dimension_definitions": {}, "angles": [], "search_terms": []})
    jid = db.upsert_job(conn, {"url": "https://x/1", "title": "Manager",
                               "company": "Acme", "found_at": db.now()})
    db.upsert_user_job(conn, uid, jid, {"score": 7.0,
                                        "score_breakdown": {"trajectory_fit": 7}})
    conn.close()
    return path, uid, jid


@pytest.fixture
def client(app_db):
    with TestClient(create_app(boot_scan=False, db_path=app_db[0])) as c:
        c.post("/login", data={"username": "alice", "password": "pw"})
        yield c


# ============================================================
# Open redirect — `back` is a form field the server echoes into Location
# ============================================================
@pytest.mark.parametrize("evil", [
    "https://evil.example/phish",
    "//evil.example/phish",
    "http://evil.example",
    "\\\\evil.example",
    "https:/\\evil.example",
    "javascript:alert(1)",
])
def test_the_back_field_cannot_bounce_a_user_off_site(client, app_db, evil):
    """`back` decides where the browser lands after an action. If an
    attacker can set it, a link that looks like the real portal ends on
    a lookalike login page — with the user primed to type a password."""
    _, _, jid = app_db

    r = client.post(f"/jobs/{jid}/status",
                    data={"status": "interested", "back": evil},
                    follow_redirects=False)

    location = r.headers["location"]
    assert not location.startswith(("http://", "https://", "//", "javascript:")), \
        f"open redirect via back={evil!r} -> {location!r}"
    assert location.startswith("/")


def test_the_back_field_still_works_for_real_destinations(client, app_db):
    _, _, jid = app_db
    for good in ("/", "/ignored", "/jobs?min_score=7"):
        r = client.post(f"/jobs/{jid}/ignore",
                        data={"reason": "x", "back": good},
                        follow_redirects=False)
        assert r.headers["location"] == good
        client.post(f"/jobs/{jid}/restore", data={"back": "/"})


# ============================================================
# Header injection — exception text is interpolated into Location
# ============================================================
def test_an_exception_message_cannot_inject_a_response_header(
        client, app_db, monkeypatch):
    """`?msg=Could not prepare: {e}` puts arbitrary text in a header."""
    from core import tailor

    monkeypatch.setattr(tailor, "prepare_application", lambda job, profile:
                        (_ for _ in ()).throw(
                            RuntimeError("boom\r\nX-Injected: yes")))

    _, _, jid = app_db
    r = client.post(f"/jobs/{jid}/prepare", follow_redirects=False)

    assert "x-injected" not in {k.lower() for k in r.headers}
    assert "\n" not in r.headers["location"]
    assert "\r" not in r.headers["location"]


# ============================================================
# Status enum — the select is client-side, the check must not be
# ============================================================
@pytest.mark.parametrize("status", ["", "deleted", "DROP TABLE users",
                                    "admin", "x" * 500])
def test_an_invalid_status_is_rejected(client, app_db, status):
    path, uid, jid = app_db

    client.post(f"/jobs/{jid}/status", data={"status": status, "back": "/"},
                follow_redirects=False)

    conn = db.connect(path)
    try:
        assert db.get_user_job(conn, uid, jid)["status"] in db.STATUSES
    finally:
        conn.close()


def test_actions_on_a_nonexistent_job_do_not_500(client):
    for job_id in (999999, 0, -1):
        assert client.post(f"/jobs/{job_id}/status",
                           data={"status": "applied", "back": "/"},
                           follow_redirects=False).status_code == 303
        assert client.get(f"/jobs/{job_id}",
                          follow_redirects=False).status_code == 303


def test_a_non_integer_job_id_is_a_clean_422_not_a_crash(client):
    assert client.get("/jobs/not-a-number").status_code == 422


# ============================================================
# Filters — every value arrives as an untrusted string
# ============================================================
@pytest.mark.parametrize("qs", [
    "min_score=abc", "min_score=-999", "min_score=1e400", "min_score=NaN",
    "days=abc", "days=-5", "days=99999999999",
    "order=%27%3B+DROP+TABLE+users%3B--", "order=",
    "company=%25%25%25", "q=" + "x" * 3000,
    "source=%00", "min_score=&days=&order=&company=&q=",
])
def test_junk_filter_values_never_500(client, qs):
    assert client.get(f"/jobs?{qs}").status_code == 200
    assert client.get(f"/export.xlsx?{qs}").status_code == 200


def test_the_users_table_still_exists_after_all_that(app_db):
    path, _, _ = app_db
    conn = db.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"] == 1
    finally:
        conn.close()


# ============================================================
# Export — scraped text goes straight into a spreadsheet
# ============================================================
def test_export_survives_control_characters_from_a_scraped_listing(tmp_path):
    """openpyxl raises IllegalCharacterError on control bytes, and
    scraped descriptions contain them regularly."""
    rows = [{
        "score": 7.0, "title": "Manager\x07\x00\x1b[31m",
        "company": "Acme\x0b", "location": None, "status": "new",
        "stretch": False, "verdict": "line\x01break",
        "angle": "a", "risk": None, "found_at": db.now(),
        "source": "greenhouse", "url": "https://x/1",
        "score_breakdown": {"trajectory_fit": 7},
    }]
    profile = {"weights": {"trajectory_fit": 60, "company_quality": 32,
                           "experience_fit": 8}}

    blob, _, ext = export.build(rows, profile)
    assert len(blob) > 0


def test_export_does_not_hand_excel_a_formula(tmp_path):
    """A company field beginning with = is executed by Excel on open.
    The value came off a job board, so it is not trusted input."""
    rows = [{
        "score": 7.0, "title": "=cmd|'/c calc'!A1", "company": "+1+1",
        "location": "-2+3", "status": "new", "stretch": False,
        "verdict": "@SUM(1:1)", "angle": None, "risk": None,
        "found_at": db.now(), "source": "s", "url": "https://x/1",
        "score_breakdown": {},
    }]
    blob, _, ext = export.build(rows, {"weights": {}})
    assert len(blob) > 0

    if ext == "xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(blob))
        for row in wb.active.iter_rows(min_row=2, values_only=True):
            for cell in row:
                if isinstance(cell, str):
                    assert not cell.startswith(("=", "+", "-", "@")), \
                        f"{cell!r} will execute when Excel opens the file"


# ============================================================
# CSV intake — a hand-edited sheet does not match its header row
# ============================================================
def test_match_answers_survives_a_row_wider_than_its_header():
    """csv.DictReader puts the overflow in a LIST under a None key."""
    schema = load_schema()
    row = {"Timestamp": "x", None: ["extra", "columns"],
           schema["dimensions"][0]["ask"]: "Mumbai"}

    answers, unmatched = match_answers(schema, row)
    assert isinstance(answers, dict)


def test_match_answers_survives_missing_and_odd_values():
    schema = load_schema()
    for row in ({}, {"": ""}, {None: None}, {"Timestamp": None},
                {"unknown col": 42}, {"unknown col": ["a", "b"]}):
        answers, unmatched = match_answers(schema, row)
        assert isinstance(answers, dict)
        assert isinstance(unmatched, list)


@pytest.mark.parametrize("body", [
    "",
    "just,a,header,row",
    "﻿Timestamp,Question\n1,2",
    "a\x00b,c\n1,2",
    "col\n" + "\n".join(str(i) for i in range(500)),
])
def test_uploading_a_malformed_csv_does_not_500(client, body):
    r = client.post("/intake",
                    files={"csv_file": ("f.csv", body.encode(), "text/csv")},
                    follow_redirects=False)
    assert r.status_code == 303


def test_uploading_no_file_does_not_500(client):
    assert client.post("/intake", data={"row_index": 0},
                       follow_redirects=False).status_code == 303


# ============================================================
# URL identity — the shared pool is keyed on it
# ============================================================
def test_urls_differing_only_by_trailing_slash_are_one_job(tmp_path):
    """Two sources listing the same role must not become two rows, or
    the user reviews and pays to score the same job twice."""
    conn = db.connect(str(tmp_path / "urls.db"))
    try:
        a = db.upsert_job(conn, {"url": "https://x.test/job/1",
                                 "title": "T", "company": "C"})
        b = db.upsert_job(conn, {"url": "https://x.test/job/1/",
                                 "title": "T", "company": "C"})
        c = db.upsert_job(conn, {"url": "HTTPS://X.TEST/job/1",
                                 "title": "T", "company": "C"})
        assert a == b == c
        assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 1
    finally:
        conn.close()


def test_a_url_with_tracking_parameters_is_the_same_job(tmp_path):
    conn = db.connect(str(tmp_path / "utm.db"))
    try:
        a = db.upsert_job(conn, {"url": "https://x.test/job/1", "title": "T"})
        b = db.upsert_job(conn, {
            "url": "https://x.test/job/1?utm_source=email&utm_campaign=x",
            "title": "T"})
        assert a == b
    finally:
        conn.close()


def test_a_path_cannot_be_passed_where_a_flag_belongs():
    """
    create_app(some_path) binds the path to boot_scan, leaves db_path
    None, and serves the REAL database. It happened: a scratch copy was
    passed positionally and the portal came up on data/careerloop.db
    with boot_scan truthy. A test that writes to the production
    database is the worst outcome in this file.
    """
    from web.app import create_app

    with pytest.raises(TypeError, match="must be a bool"):
        create_app("/tmp/some.db")
