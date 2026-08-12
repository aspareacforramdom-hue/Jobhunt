"""
PHASE 5 — the bias correction is the feature, so it is what gets tested.

A raw AmbitionBox number rendered without context is worse than no
number: it talks a user out of an ordinary employer.
"""

import pytest

from core import db, reviews, session


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "rev.db"))
    yield c
    c.close()


def test_a_fresh_cache_entry_is_not_refetched(conn, monkeypatch):
    db.save_company(conn, "Acme", rating=3.4, review_count=412,
                    review_summary={"rating": 3.4})

    monkeypatch.setattr(reviews, "fetch_ratings",
                        lambda c: pytest.fail("refetched a fresh entry"))
    monkeypatch.setenv("APIFY_TOKEN", "x")

    assert reviews.enrich(conn, "Acme")["rating"] == 3.4


def test_a_stale_entry_is_refetched(conn, monkeypatch):
    db.save_company(conn, "Acme", rating=3.0)
    conn.execute("UPDATE companies SET fetched_at = '2020-01-01T00:00:00+00:00'")
    conn.commit()

    assert db.company_is_fresh(conn, "Acme") is False

    monkeypatch.setenv("APIFY_TOKEN", "x")
    monkeypatch.setattr(reviews, "fetch_ratings",
                        lambda c: {"rating": 4.1, "review_count": 500,
                                   "breakdown": {"5": 300}})
    monkeypatch.setattr(reviews, "fetch_reviews", lambda c, limit=60: [])
    monkeypatch.setattr(reviews, "summarize", lambda *a, **kw: {
        "rating": 4.1, "review_count": 500, "sector_median": 3.6,
        "sector_context": "above median for Indian FMCG (3.6)",
        "strengths": [], "concerns": [], "red_flags": [],
        "current_vs_former": "", "confidence": "high"})

    out = reviews.enrich(conn, "Acme")
    assert out["rating"] == 4.1
    assert out["sector_median_rating"] == 3.6


def test_enrichment_never_raises_when_the_fetch_fails(conn, monkeypatch):
    """A review lookup failing must not break a detail page."""
    monkeypatch.setenv("APIFY_TOKEN", "x")
    monkeypatch.setattr(reviews, "fetch_ratings",
                        lambda c: (_ for _ in ()).throw(RuntimeError("502")))

    assert reviews.enrich(conn, "Acme") is None


def test_without_a_token_it_degrades_instead_of_failing(conn, monkeypatch):
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    db.save_company(conn, "Acme", rating=3.2)
    conn.execute("UPDATE companies SET fetched_at = '2020-01-01T00:00:00+00:00'")
    conn.commit()

    assert reviews.available() is False
    assert reviews.enrich(conn, "Acme")["rating"] == 3.2


def test_dry_mode_fetches_nothing(conn, monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "x")
    monkeypatch.setattr(reviews, "fetch_ratings",
                        lambda c: pytest.fail("--dry must not call Apify"))

    assert reviews.enrich(conn, "Acme", dry=True) is None


def test_a_listing_rating_never_overwrites_a_real_summary(conn):
    db.save_company(conn, "Acme", rating=3.9, review_count=412,
                    review_summary={"sector_context": "typical for FMCG"})

    reviews.enrich_from_listing(conn, {"company": "Acme",
                                       "company_rating": 2.0})

    stored = db.get_company(conn, "Acme")
    assert stored["rating"] == 3.9
    assert stored["review_summary"]["sector_context"] == "typical for FMCG"


def test_a_listing_rating_is_kept_when_there_is_nothing_better(conn):
    reviews.enrich_from_listing(conn, {"company": "Zeta",
                                       "company_rating": 3.1,
                                       "company_review_count": 88})

    stored = db.get_company(conn, "Zeta")
    assert stored["rating"] == 3.1
    assert stored["review_count"] == 88


def test_enrichment_is_bounded_and_skips_cached_companies(conn, monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "x")
    db.save_company(conn, "Cached", rating=4.0)

    called = []
    monkeypatch.setattr(reviews, "enrich",
                        lambda c, name, **kw: called.append(name) or True)

    jobs = [{"company": "Cached"}] + \
           [{"company": f"Co{i}"} for i in range(20)] + \
           [{"company": "Co1"}]        # duplicate

    done = reviews.enrich_top_companies(conn, jobs, limit=3)

    assert done == 3
    assert "Cached" not in called
    assert len(called) == len(set(called)) == 3


def test_the_cache_is_shared_so_the_second_user_pays_nothing(conn):
    """companies holds public data, so it is deliberately not per-user."""
    a, _ = session.create_account(conn, "alice", "pw")
    b, _ = session.create_account(conn, "bob", "pw")
    db.save_company(conn, "Acme", rating=3.4, review_count=412,
                    review_summary={"confidence": "high"})

    job = db.upsert_job(conn, {"url": "https://x.test/1", "title": "T",
                               "company": "Acme"})
    for uid in (a, b):
        db.upsert_user_job(conn, uid, job, {"score": 7.0})

    for uid in (a, b):
        row = db.get_user_job(conn, uid, job)
        assert row["company_rating"] == 3.4
        assert row["review_summary"]["confidence"] == "high"
