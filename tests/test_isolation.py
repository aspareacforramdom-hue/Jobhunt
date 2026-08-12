"""
HARD CONSTRAINT — never let one user's data reach another.

Two things are proved here:
  1. user A cannot read B's user_jobs (or profile, applications, runs)
  2. A's Telegram never hits B's chat id

Everything shared is shared on purpose and contains no personal data:
the `jobs` pool (public listings, scraped once) and `companies`
(public employer reviews). Opinions about those jobs are not shared.
"""

import pytest

from core import db, notify, session


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "test.db"))
    yield c
    c.close()


@pytest.fixture
def users(conn):
    a, _ = session.create_account(conn, "alice", "pw-alice")
    b, _ = session.create_account(conn, "bob", "pw-bob")
    db.set_telegram_chat_id(conn, a, "chat-alice")
    db.set_telegram_chat_id(conn, b, "chat-bob")
    return a, b


@pytest.fixture
def shared_job(conn):
    return db.upsert_job(conn, {"url": "https://x.test/1", "title": "Manager",
                                "company": "Acme", "source": "greenhouse"})


# ------------------------------------------------------------
# The shared pool is genuinely shared
# ------------------------------------------------------------
def test_the_same_listing_is_stored_once_for_both_users(conn):
    job = {"url": "https://x.test/dup", "title": "Manager", "company": "Acme"}
    first = db.upsert_job(conn, job)
    second = db.upsert_job(conn, job)

    assert first == second
    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 1


# ------------------------------------------------------------
# ...and opinions about it are not
# ------------------------------------------------------------
def test_scores_on_the_same_job_stay_separate(conn, users, shared_job):
    a, b = users
    db.upsert_user_job(conn, a, shared_job, {"score": 9.1, "verdict": "A's",
                                             "angle": "a", "stretch": True,
                                             "score_breakdown": {"x": 9}})
    db.upsert_user_job(conn, b, shared_job, {"score": 2.4, "verdict": "B's",
                                             "angle": "b", "stretch": False,
                                             "score_breakdown": {"y": 2}})

    a_view = db.get_user_job(conn, a, shared_job)
    b_view = db.get_user_job(conn, b, shared_job)

    assert (a_view["score"], a_view["verdict"]) == (9.1, "A's")
    assert (b_view["score"], b_view["verdict"]) == (2.4, "B's")
    assert a_view["score_breakdown"] != b_view["score_breakdown"]


def test_a_cannot_read_bs_user_jobs(conn, users):
    a, b = users
    only_b = db.upsert_job(conn, {"url": "https://x.test/b-only",
                                  "title": "Secret", "company": "BCorp"})
    db.upsert_user_job(conn, b, only_b, {"score": 8.0})

    assert db.get_user_job(conn, a, only_b) is None
    assert db.list_user_jobs(conn, a) == []
    assert len(db.list_user_jobs(conn, b)) == 1


def test_every_list_filter_still_scopes_to_one_user(conn, users):
    """A filter combination must never widen the result past its user."""
    a, b = users
    job = db.upsert_job(conn, {"url": "https://x.test/f", "title": "Lead",
                               "company": "Acme", "source": "lever",
                               "found_at": db.now()})
    db.upsert_user_job(conn, b, job, {"score": 9.9})

    for kwargs in ({}, {"min_score": 0}, {"company": "acme"},
                   {"source": "lever"}, {"search": "lead"},
                   {"order": "company"}, {"order": "date"},
                   {"statuses": ["new"]}, {"exclude_ignored": False},
                   {"since": "2000-01-01"}, {"limit": 100}):
        assert db.list_user_jobs(conn, a, **kwargs) == [], \
            f"filter {kwargs} leaked another user's rows"


def test_a_status_change_by_one_user_does_not_move_the_other(
        conn, users, shared_job):
    a, b = users
    for u in (a, b):
        db.upsert_user_job(conn, u, shared_job, {"score": 7.0})

    db.set_status(conn, a, shared_job, "applied")

    assert db.get_user_job(conn, a, shared_job)["status"] == "applied"
    assert db.get_user_job(conn, b, shared_job)["status"] == "new"


def test_ignoring_a_job_only_removes_it_from_that_users_board(
        conn, users, shared_job):
    a, b = users
    for u in (a, b):
        db.upsert_user_job(conn, u, shared_job, {"score": 7.0})

    db.ignore_job(conn, a, shared_job, reason="wrong city")

    assert db.list_user_jobs(conn, a) == []
    assert len(db.list_user_jobs(conn, b)) == 1

    ignored = db.list_user_jobs(conn, a, statuses=["ignored"])
    assert ignored[0]["ignored_reason"] == "wrong city"
    assert db.list_user_jobs(conn, b, statuses=["ignored"]) == []


def test_restoring_keeps_the_reason(conn, users, shared_job):
    a, _ = users
    db.upsert_user_job(conn, a, shared_job, {"score": 7.0})
    db.ignore_job(conn, a, shared_job, reason="pay too low")
    db.restore_job(conn, a, shared_job)

    row = db.get_user_job(conn, a, shared_job)
    assert row["status"] == "new"
    assert row["ignored_reason"] == "pay too low"


def test_profiles_applications_and_runs_are_scoped(conn, users, shared_job):
    a, b = users
    db.save_profile(conn, b, {"target": {"role_family": "B's target"},
                              "weights": {"trajectory_fit": 100}})
    db.save_application(conn, b, shared_job, {"cover_letter": "B's letter"})
    db.finish_run(conn, db.start_run(conn, b, "deep"), "ok", 5)

    assert db.load_profile(conn, a) is None
    assert db.list_applications(conn, a) == []
    assert db.get_application(conn, a, shared_job) is None
    assert db.recent_runs(conn, a) == []
    assert len(db.recent_runs(conn, b)) == 1


def test_one_users_scan_failure_does_not_touch_another(conn, users):
    a, b = users
    db.finish_run(conn, db.start_run(conn, a, "catchup"), "ok", 12)
    db.finish_run(conn, db.start_run(conn, b, "catchup"), "error", 0,
                  "scoring blew up")

    assert db.last_successful_run(conn, a)["jobs_found"] == 12
    assert db.last_successful_run(conn, b) is None
    assert db.recent_runs(conn, b)[0]["error_text"] == "scoring blew up"


def test_deleting_a_user_leaves_the_shared_pool_intact(conn, users,
                                                       shared_job):
    a, b = users
    for u in (a, b):
        db.upsert_user_job(conn, u, shared_job, {"score": 7.0})

    conn.execute("DELETE FROM users WHERE id = ?", (a,))
    conn.commit()

    assert db.get_job(conn, shared_job) is not None
    assert len(db.list_user_jobs(conn, b)) == 1


# ------------------------------------------------------------
# Telegram
# ------------------------------------------------------------
def _capture(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_send",
                        lambda text, chat_id=None, dry=False:
                        sent.append((chat_id, text)) or True)
    return sent


def test_a_scan_summary_only_goes_to_its_own_users_chat(conn, users,
                                                        monkeypatch):
    a, b = users
    sent = _capture(monkeypatch)
    jobs = [{"score": 8.1, "title": "Manager", "company": "Acme"}]

    notify.send_scan_summary(db.get_user(conn, a), jobs, "http://localhost")
    notify.send_scan_summary(db.get_user(conn, b), jobs, "http://localhost")

    assert [c for c, _ in sent] == ["chat-alice", "chat-bob"]


def test_a_user_without_a_chat_id_is_skipped_not_defaulted(
        conn, users, monkeypatch):
    """The dangerous bug: falling back to TELEGRAM_CHAT_ID and posting
    one user's roles into whoever's chat is in the environment."""
    a, _ = users
    db.set_telegram_chat_id(conn, a, None)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat-someone-else")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    sent = _capture(monkeypatch)

    result = notify.send_scan_summary(
        db.get_user(conn, a), [{"score": 8.1, "title": "T", "company": "C"}],
        "http://localhost")

    assert result is False
    assert sent == []


def test_a_user_with_no_results_is_not_messaged(conn, users, monkeypatch):
    a, _ = users
    sent = _capture(monkeypatch)

    assert notify.send_scan_summary(db.get_user(conn, a), [],
                                    "http://localhost") is False
    assert sent == []


# ------------------------------------------------------------
# Sessions
# ------------------------------------------------------------
def test_a_password_is_never_stored_in_the_clear(conn):
    user_id, password = session.create_account(conn, "carol")
    row = db.get_user(conn, user_id)

    assert password not in row["password_hash"]
    assert row["password_hash"] != password
    assert len(row["salt"]) == 32
    assert session.verify_password(password, row["password_hash"], row["salt"])
    assert not session.verify_password("wrong", row["password_hash"],
                                       row["salt"])


def test_the_same_password_hashes_differently_for_two_users(conn):
    a, _ = session.create_account(conn, "d1", "identical")
    b, _ = session.create_account(conn, "d2", "identical")

    assert db.get_user(conn, a)["password_hash"] \
        != db.get_user(conn, b)["password_hash"]


def test_a_session_cookie_cannot_be_edited_into_another_user(conn, users):
    a, b = users
    token = session.issue_session(a)

    assert session.read_session(token) == a

    body, sig = token.rsplit(".", 1)
    forged = session.issue_session(b).rsplit(".", 1)[0] + "." + sig
    assert session.read_session(forged) is None
    assert session.read_session(body + ".") is None
    assert session.read_session("garbage") is None


def test_an_expired_session_is_rejected(users):
    a, _ = users
    old = session.issue_session(a, issued_at=0)

    assert session.read_session(old) is None
    assert session.read_session(old, max_age=0) == a   # max_age off


def test_authenticate_rejects_a_wrong_password(conn):
    session.create_account(conn, "erin", "correct-horse")

    assert session.authenticate(conn, "erin", "correct-horse") is not None
    assert session.authenticate(conn, "erin", "wrong") is None
    assert session.authenticate(conn, "nobody", "correct-horse") is None
