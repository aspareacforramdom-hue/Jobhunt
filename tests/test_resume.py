"""
THE RÉSUMÉ — the thing the tool was judging you without ever reading.

Before this, a CV was parsed once at intake, compiled into a few
sentences, and thrown away. Two consequences, one of them capable of
damaging a real application:

  1. The scorer judged every job against roughly three sentences about a
     person, while the document that actually evidences their career sat
     unused.

  2. `tailor` was asked to return `bullet_swaps.replace` — "quote the
     opening words of an existing bullet" — from a CV it had never been
     given. It invented one, and prepare.html captioned the invention
     "these reword what is already on your CV". A user could paste a
     fabricated line into their own résumé on that promise.

The CV is now the most sensitive text in the database, so the privacy
tests here matter as much as the capability ones.
"""

import pytest

from core import db, publish, score, session, tailor

RESUME = """SREEHARI VILAS — Product Manager
sreehari@example.com · +91 98765 43210 · Mumbai

EXPERIENCE
Paytm — Senior Product Manager, Payments (2022-2026)
- Owned the reconciliation service processing 2M transactions per day
- Led the migration off a monolith to 14 services, cutting p99 by 40%
- Grew UPI merchant activation 3x in eight months

Kotak Mahindra — Product Manager (2019-2022)
- Shipped the first in-app lending flow, 400k applications in year one
"""

PROFILE = {
    "target": {"role_family": "Product Management", "seniority": "Senior"},
    "hard_filters": {"min_ctc_lpa": 45},
    "weights": {"trajectory_fit": 60, "company_quality": 32,
                "experience_fit": 8},
    "dimension_definitions": {},
    "angles": ["Owns payments infrastructure at scale"],
    "search_terms": ["Product Manager"],
}

JOB = {"title": "Product Manager - Payments", "company": "Razorpay",
       "location": "Bengaluru", "url": "https://x/1",
       "description": "Own reconciliation and settlement for UPI.",
       "verdict": "Direct match.", "angle": "Payments depth.",
       "score": 8.4, "score_breakdown": {"trajectory_fit": 9}}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "cv.db"))
    yield c
    c.close()


# ============================================================
# It is kept at all
# ============================================================
def test_a_resume_survives_a_save_and_load(conn):
    uid, _ = session.create_account(conn, "alice", "pw")
    db.save_profile(conn, uid, dict(PROFILE, resume_text=RESUME))

    assert db.load_profile(conn, uid)["resume_text"] == RESUME


def test_saving_a_profile_without_a_resume_does_not_erase_one(conn):
    """
    The intake CSV upload saves a whole profile and knows nothing about
    the CV. Without COALESCE it would blank it every time someone
    recompiled their answers.
    """
    uid, _ = session.create_account(conn, "alice", "pw")
    db.save_profile(conn, uid, dict(PROFILE, resume_text=RESUME))

    db.save_profile(conn, uid, PROFILE)          # no resume_text key

    assert db.load_profile(conn, uid)["resume_text"] == RESUME


# ============================================================
# It reaches the two places that were guessing
# ============================================================
def test_the_scorer_is_given_the_resume():
    system = score._system(dict(PROFILE, resume_text=RESUME))

    assert "reconciliation service processing 2M" in system
    assert "RÉSUMÉ" in system


def test_the_resume_sits_in_the_cached_half_of_the_prompt():
    """
    A scan scores ~25 batches per user against one identical system
    block. In the cached prefix the CV is paid for once and read back at
    a tenth of the price; in the per-batch message it would be re-billed
    at full price on every batch.
    """
    system = score._system(dict(PROFILE, resume_text=RESUME))
    listings = score._prompt([{"url": "u", "title": "t", "company": "c"}])

    assert "2M transactions" in system
    assert "2M transactions" not in listings


def test_the_cached_prefix_stays_byte_stable_with_a_resume():
    """Caching is an exact prefix match; an unstable block silently
    misses and every batch pays full price."""
    p = dict(PROFILE, resume_text=RESUME)
    assert score._system(p) == score._system(dict(p))


def test_the_tailor_is_given_the_resume():
    block = tailor._resume_block(dict(PROFILE, resume_text=RESUME))

    assert "monolith to 14 services" in block
    assert "verbatim" in block


def test_without_a_resume_the_tailor_is_told_to_invent_nothing():
    """
    The specific harm: asked to quote an existing CV bullet with no CV,
    a model produces a plausible one and the interface labels it as the
    user's own writing.
    """
    block = tailor._resume_block(PROFILE)

    assert "NOT PROVIDED" in block
    assert "empty bullet_swaps" in block


def test_without_a_resume_the_scorer_is_told_not_to_guess():
    system = score._system(PROFILE)

    assert "not provided" in system
    assert "do not guess" in system.lower()


# ============================================================
# It never leaves the machine
# ============================================================
def test_a_published_page_carries_nothing_from_the_resume(conn):
    """
    Published pages have no login. An unlisted URL is not a secret — it
    gets forwarded and pasted into group chats. A CV reaching one would
    expose a phone number, an employer and a salary history at once.
    """
    uid, _ = session.create_account(conn, "alice", "pw")
    db.save_profile(conn, uid, dict(PROFILE, resume_text=RESUME))
    job = db.upsert_job(conn, {"url": "https://x/1", "title": "PM",
                               "company": "Acme"})
    db.upsert_user_job(conn, uid, job, {"score": 8.0, "evidence":
                                        "Owned the reconciliation service"})

    out = publish.build_for_user(conn, uid, "Alice")

    for leak in ("sreehari@example.com", "98765", "Kotak", "Paytm",
                 "2M transactions", "SREEHARI"):
        assert leak not in out, f"the published page leaked {leak!r}"

    # `evidence` is itself a quotation FROM the CV, so it is CV content
    # wearing a different field name. It belongs on the private detail
    # page and nowhere else — this is the assertion that stops someone
    # later adding it to the published template as "just another field".
    assert "Owned the reconciliation service" not in out


def test_the_evidence_line_is_per_user_like_every_other_verdict(conn):
    a, _ = session.create_account(conn, "alice", "pw")
    b, _ = session.create_account(conn, "bob", "pw")
    job = db.upsert_job(conn, {"url": "https://x/1", "title": "PM",
                               "company": "Acme"})

    db.upsert_user_job(conn, a, job, {"score": 8.0,
                                      "evidence": "alice's proof"})
    db.upsert_user_job(conn, b, job, {"score": 5.0, "evidence": None})

    assert db.get_user_job(conn, a, job)["evidence"] == "alice's proof"
    assert db.get_user_job(conn, b, job)["evidence"] is None


def test_the_prepare_page_only_promises_what_it_can_deliver(tmp_path,
                                                            monkeypatch):
    """
    The caption "these reword what is already on your CV" was shown
    unconditionally. With no CV on file it was simply untrue.
    """
    from fastapi.testclient import TestClient
    from web.app import create_app

    monkeypatch.setenv("CAREERLOOP_SECRET", "test-secret")
    path = str(tmp_path / "prep.db")
    c = db.connect(path)
    uid, _ = session.create_account(c, "alice", "pw")
    db.save_profile(c, uid, PROFILE)                  # deliberately no CV
    jid = db.upsert_job(c, {"url": "https://x/1", "title": "PM",
                            "company": "Acme", "found_at": db.now()})
    db.upsert_user_job(c, uid, jid, {"score": 8.0})
    db.save_application(c, uid, jid, {"cover_letter": "hello",
                                      "bullet_swaps": []}, None)
    c.close()

    with TestClient(create_app(boot_scan=False, db_path=path)) as client:
        client.post("/login", data={"username": "alice", "password": "pw"})
        html = client.get(f"/applications/{jid}").text

    assert "already on your CV" not in html
    assert "No CV on file" in html
