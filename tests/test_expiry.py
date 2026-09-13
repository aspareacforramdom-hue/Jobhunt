"""
THE DEAD LINKS, and the design that measurement forced.

Nothing in this codebase had ever expired a job; the pool only grew.
Measured on the three live boards: 20 of 70 links were already HTTP 404
— a 29% dead rate — and the median card was 31 days old. A board where
one link in three is dead is worse than no board, because the user only
discovers it after deciding to apply.

The first fix inferred closure for free. A Greenhouse or Lever board
answers with the company's complete current list, so anything missing
had surely been taken down: no probing, no extra request. It closed 278
of 963 listings, and the pool-level number agreed beautifully with the
29% measured by probing.

Then 60 of the closed ones were probed individually. 42 still answered
HTTP 200 with a working apply button — a 70% FALSE POSITIVE rate. The
cause was not pagination; the Lever API returns the same 19 rows for
mindtickle at any limit. Employers UNLIST a posting while leaving the
direct link live and applicable, and a board read cannot tell that
apart from a deletion.

So the design inverted, and these tests hold the inversion in place:

  absence from a full board raises a SUSPICION (missing_since) and
  nothing more. Its value is ordering — it decides what gets asked
  about first, which matters when the pool is 1,600 and a sweep is 200.

  only an HTTP answer CLOSES anything. 404, 410, or a page that says so
  in words. A timeout or a 403 bot-check means "could not find out
  today"; closing on those would empty a board the first time someone's
  wifi dropped.
"""

import pytest

from core import db, linkcheck, session
from core.scheduler import Scanner


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "expiry.db"))
    yield c
    c.close()


@pytest.fixture
def user(conn):
    uid, _ = session.create_account(conn, "alice", "pw")
    db.save_profile(conn, uid, {"target": {}, "hard_filters": {},
                                "weights": {"trajectory_fit": 100},
                                "dimension_definitions": {}})
    return uid


OLD = "2026-01-01T00:00:00Z"
NOW = "2026-09-13T00:00:00Z"


def _seed(conn, *jobs):
    ids = []
    for url, company, source in jobs:
        ids.append(db.upsert_job(conn, {
            "url": url, "title": f"T-{url}", "company": company,
            "source": source, "found_at": OLD}))
    conn.execute("UPDATE jobs SET last_seen_at = ?", (OLD,))
    return ids


# ============================================================
# Absence is a suspicion, never a verdict
# ============================================================
def test_falling_off_a_board_flags_but_does_not_close(conn):
    """
    THE lesson. Closing on this directly was wrong 70% of the time:
    employers unlist a posting while the link stays live and applicable,
    so the job is still a real opportunity and deleting it costs the
    user a job they could have applied to.
    """
    _seed(conn, ("https://x/1", "paytm", "lever"),
                ("https://x/2", "paytm", "lever"))

    db.mark_seen(conn, ["https://x/1"], when=NOW)      # 2 did not come back
    flagged = db.mark_missing(conn, "lever", "paytm", when=NOW)

    assert flagged == 1
    assert db.suspect_job_count(conn) == 1
    assert db.closed_job_count(conn) == 0, "suspicion must not close anything"
    assert db.open_job_count(conn) == 2


def test_a_suspect_still_appears_on_the_board(conn, user):
    """Until the page has actually been asked, it is a live listing."""
    ids = _seed(conn, ("https://x/1", "paytm", "lever"))
    db.upsert_user_job(conn, user, ids[0], {"score": 9.0})

    db.mark_missing(conn, "lever", "paytm", when=NOW)

    assert len(db.list_user_jobs(conn, user)) == 1


def test_an_empty_answer_never_flags_a_board(conn):
    """
    An empty fetch is what a 404, a timeout, a rename and a genuinely
    empty board all look like. Acting on one would send the checker
    after a live company's entire board.
    """
    _seed(conn, ("https://x/1", "paytm", "lever"),
                ("https://x/2", "paytm", "lever"))

    Scanner._flag_missing(conn, raw=[], stamp=NOW)

    assert db.suspect_job_count(conn) == 0
    assert db.closed_job_count(conn) == 0


def test_one_silent_board_does_not_flag_the_others(conn):
    _seed(conn, ("https://x/1", "paytm", "lever"),
                ("https://x/2", "postman", "greenhouse"))

    # Greenhouse answered; Lever returned nothing this scan.
    Scanner._flag_missing(conn, raw=[{"url": "https://x/9",
                                      "company": "postman",
                                      "source": "greenhouse"}], stamp=NOW)

    row = conn.execute(
        "SELECT missing_since FROM jobs WHERE company='paytm'").fetchone()
    assert row["missing_since"] is None, "paytm must be untouched"


def test_a_keyword_source_carries_no_information_by_absence(conn):
    """
    Indeed, LinkedIn and Naukri answer a SEARCH — a slice ranked for a
    keyword, never a company's full list. A job missing from today's
    slice may simply have ranked lower.
    """
    _seed(conn, ("https://x/1", "acme", "indeed"))

    Scanner._flag_missing(conn, raw=[{"url": "https://x/2", "company": "acme",
                                      "source": "indeed"}], stamp=NOW)

    assert db.suspect_job_count(conn) == 0
    with pytest.raises(ValueError, match="complete list"):
        db.mark_missing(conn, "indeed", "acme")


def test_company_matching_ignores_case(conn):
    _seed(conn, ("https://x/1", "Paytm", "lever"))

    assert db.mark_missing(conn, "lever", "PAYTM", when=NOW) == 1


def test_reappearing_on_a_board_clears_the_suspicion(conn):
    """Unlisted one week, listed the next. The queue should forget it."""
    _seed(conn, ("https://x/1", "paytm", "lever"))
    db.mark_missing(conn, "lever", "paytm", when=NOW)
    assert db.suspect_job_count(conn) == 1

    db.mark_seen(conn, ["https://x/1"], when="2026-09-14T00:00:00Z")

    assert db.suspect_job_count(conn) == 0


# ============================================================
# Only an HTTP answer closes anything
# ============================================================
def test_only_404_and_410_are_evidence(monkeypatch):
    import urllib.error

    def answer(code):
        def fake(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "", {}, None)
        return fake

    for code, expected in ((404, True), (410, True),
                           (403, False), (429, False), (500, False)):
        monkeypatch.setattr(linkcheck.urllib.request, "urlopen", answer(code))
        gone, why = linkcheck._verdict("https://x/1")
        assert gone is expected, f"HTTP {code} judged wrong: {why}"


def test_a_timeout_is_not_evidence(monkeypatch):
    """Closing on a timeout would empty a board the first time the
    user's connection wobbled."""
    def boom(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(linkcheck.urllib.request, "urlopen", boom)

    gone, why = linkcheck._verdict("https://x/1")
    assert gone is False
    assert "not evidence" in why


def test_a_page_that_says_it_is_closed_is_believed(monkeypatch):
    class Resp:
        status = 200

        def read(self, n=None):
            return b"<h1>This job is no longer available</h1>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(linkcheck.urllib.request, "urlopen",
                        lambda req, timeout=None: Resp())

    gone, why = linkcheck._verdict("https://x/1")
    assert gone is True
    assert "no longer available" in why


def test_a_live_lever_page_is_not_closed_just_for_being_unlisted(monkeypatch):
    """
    The exact case that broke the first design: Mindtickle's Senior
    Product Manager was absent from the Lever API at any limit, and its
    page answered 200 with a working apply button.
    """
    class Resp:
        status = 200

        def read(self, n=None):
            return (b"Mindtickle - Senior Product Manager ... "
                    b"apply for this job")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(linkcheck.urllib.request, "urlopen",
                        lambda req, timeout=None: Resp())

    gone, _ = linkcheck._verdict("https://jobs.lever.co/mindtickle/e55245fd")
    assert gone is False


# ============================================================
# What gets asked, and in what order
# ============================================================
def test_only_links_on_a_board_are_checked(conn, user):
    """The pool is 1,600 rows and growing; the boards are a few dozen
    each. A dead link costs nobody anything until it is in front of
    them."""
    on_board, off_board = _seed(conn, ("https://x/1", "acme", "indeed"),
                                      ("https://x/2", "acme", "indeed"))
    db.upsert_user_job(conn, user, on_board, {"score": 8.0})

    assert linkcheck.candidates(conn) == ["https://x/1"]
    assert off_board is not None


def test_suspects_are_asked_about_first(conn, user):
    """This is the entire value the free board read still provides:
    something that fell off its board today is far likelier to be gone
    than something that merely looks old."""
    ids = _seed(conn, ("https://old/1", "paytm", "lever"),
                      ("https://sus/2", "paytm", "lever"))
    for jid in ids:
        db.upsert_user_job(conn, user, jid, {"score": 8.0})
    conn.execute("UPDATE jobs SET missing_since = ? WHERE url = ?",
                 (NOW, "https://sus/2"))

    assert linkcheck.candidates(conn)[0] == "https://sus/2"


def test_a_sweep_closes_only_what_it_proved(conn, user, monkeypatch):
    ids = _seed(conn, ("https://gone/1", "acme", "indeed"),
                      ("https://live/2", "acme", "indeed"))
    for jid in ids:
        db.upsert_user_job(conn, user, jid, {"score": 8.0})

    monkeypatch.setattr(linkcheck, "_verdict",
                        lambda u: ("gone" in u, "HTTP 404"))

    checked, closed = linkcheck.sweep(conn, verbose=False)

    assert (checked, closed) == (2, 1)
    assert [j["url"] for j in db.list_user_jobs(conn, user)] == \
        ["https://live/2"]


def test_closing_twice_counts_once(conn, user, monkeypatch):
    """The sweep reruns over the same board; the number it reports has
    to be listings newly closed, not rows it touched."""
    ids = _seed(conn, ("https://gone/1", "acme", "indeed"))
    db.upsert_user_job(conn, user, ids[0], {"score": 8.0})
    monkeypatch.setattr(linkcheck, "_verdict", lambda u: (True, "HTTP 404"))

    first = linkcheck.sweep(conn, verbose=False)
    second = linkcheck.sweep(conn, verbose=False)

    assert first == (1, 1)
    assert second == (0, 0), "a closed listing leaves the queue"


# ============================================================
# What the board shows
# ============================================================
def test_a_proven_dead_listing_leaves_the_board(conn, user):
    jid, = _seed(conn, ("https://x/1", "paytm", "lever"))
    db.upsert_user_job(conn, user, jid, {"score": 9.0})
    assert len(db.list_user_jobs(conn, user)) == 1

    db.close_urls(conn, ["https://x/1"], when=NOW)

    assert db.list_user_jobs(conn, user) == []


def test_but_a_job_you_applied_to_stays(conn, user):
    """It is part of your pipeline. The job you applied to on Monday
    must not silently disappear on Friday because the posting came
    down — that is exactly when you need to remember you applied."""
    jid, = _seed(conn, ("https://x/1", "paytm", "lever"))
    db.upsert_user_job(conn, user, jid, {"score": 9.0})
    db.set_status(conn, user, jid, "applied")

    db.close_urls(conn, ["https://x/1"], when=NOW)

    rows = db.list_user_jobs(conn, user)
    assert len(rows) == 1
    assert rows[0]["closed"] is True, "and it is marked as gone"


def test_the_count_agrees_with_the_rows(conn, user):
    """A count that disagrees with the list is how a board ends up
    saying "15 of 200" above twelve listings."""
    ids = _seed(conn, ("https://x/1", "paytm", "lever"),
                      ("https://x/2", "paytm", "lever"),
                      ("https://x/3", "paytm", "lever"))
    for jid in ids:
        db.upsert_user_job(conn, user, jid, {"score": 8.0})

    db.close_urls(conn, ["https://x/2", "https://x/3"], when=NOW)

    assert db.count_user_jobs(conn, user) == len(db.list_user_jobs(conn, user))
    assert db.count_user_jobs(conn, user) == 1


def test_closed_listings_can_still_be_asked_for(conn, user):
    ids = _seed(conn, ("https://x/1", "paytm", "lever"),
                      ("https://x/2", "paytm", "lever"))
    for jid in ids:
        db.upsert_user_job(conn, user, jid, {"score": 8.0})
    db.close_urls(conn, ["https://x/1"], when=NOW)

    assert len(db.list_user_jobs(conn, user, include_closed=True)) == 2
