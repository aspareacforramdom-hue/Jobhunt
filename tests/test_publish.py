"""
PUBLISHED PAGES — no login, so the privacy rules are the whole test.

A published page has no auth in front of it. An unlisted URL is not a
secret: it gets forwarded, pasted into a group chat, and indexed if it
ever leaks. So the question every test here asks is "if this URL reached
a stranger, what would they learn about this person?"

The answer must be: public job listings, and this tool's reasoning about
them. Never a CV, a salary floor, a contact detail, or another user's
board.
"""

import re

import pytest

from core import db, publish, session

PROFILE = {
    "target": {"role_family": "Brand Marketing Manager",
               "seniority": "Manager",
               "locations": ["Mumbai"]},
    # Every field below is sensitive and must never reach the page.
    "hard_filters": {"min_ctc_lpa": 28, "locations": ["Mumbai"],
                     "exclude_keywords": ["telecalling"]},
    "angles": ["Left Kotak after a manager took credit for the Q3 launch",
               "sreehari@example.com", "+91 98765 43210"],
    "weights": {"trajectory_fit": 30, "creative_ownership": 30,
                "brand_scale": 20, "company_quality": 12,
                "experience_fit": 8},
    "dimension_definitions": {"creative_ownership": "Owns direction?",
                              "brand_scale": "How visible a brand?"},
}

JOB = {
    "id": 1, "title": "Brand Manager", "company": "Tata Consumer",
    "location": "Mumbai", "url": "https://x.test/1", "score": 8.4,
    "verdict": "Real ownership.", "angle": "Six years selling the category.",
    "risk": "Most junior person in the room.", "stretch": True,
    "score_breakdown": {"trajectory_fit": 9, "creative_ownership": 8,
                        "brand_scale": 4, "company_quality": 8,
                        "experience_fit": 5},
}

DIST = {"count": 3, "min": 4.6, "max": 8.4, "median": 7.9, "p90": 8.4,
        "below_floor": 0,
        "buckets": [{"score": b, "count": 1 if b in (4, 7, 8) else 0,
                     "pct": 100 if b in (4, 7, 8) else 0}
                    for b in range(1, 11)]}


@pytest.fixture
def page():
    return publish.render("Shivani", PROFILE, [JOB], DIST)


def body_of(page):
    """Everything a reader sees, minus the stylesheet and the timestamp.

    Asserting against the whole document gives false positives — a hex
    colour like #78828f contains "28" — and a false positive here trains
    people to loosen the privacy tests, which is the last thing this
    file should encourage.

    The "updated 15 Aug, 21:28 IST" stamp is cut for the same reason, and
    it is the sharper version of the problem: the salary floor below is
    28, so this file failed for eleven minutes of every hour and passed
    the rest. A privacy test that fails on the clock gets muted.
    """
    body = page.split("</style>", 1)[-1]
    return re.sub(r'<p class="stamp">.*?</p>', "", body, flags=re.S)


# ============================================================
# Privacy — the reason this file exists
# ============================================================
def test_the_salary_floor_never_reaches_the_page(page):
    """The single most sensitive number in a profile."""
    body = body_of(page)
    assert "28" not in body
    assert "min_ctc" not in page
    assert "lpa" not in body.lower()


def test_cv_derived_angles_never_reach_the_page(page):
    """profile['angles'] comes from the CV and the intake answers."""
    assert "Kotak" not in page
    assert "took credit" not in page


def test_contact_details_never_reach_the_page(page):
    assert "@example.com" not in page
    assert "98765" not in page


def test_exclusions_never_reach_the_page(page):
    """What someone refuses to do is their business."""
    assert "telecalling" not in page


def test_only_whitelisted_profile_fields_appear(page):
    """A field added to `target` later must not silently publish."""
    profile = {**PROFILE,
               "target": {**PROFILE["target"],
                          "current_employer": "Kotak Mahindra",
                          "notice_period": "3 months"}}

    out = publish.render("Shivani", profile, [JOB], DIST)
    assert "Kotak" not in out
    assert "3 months" not in out


def test_what_it_does_publish_is_the_public_part(page):
    assert "Brand Manager" in page
    assert "Tata Consumer" in page
    assert "Brand Marketing Manager" in page
    assert "Six years selling the category." in page


# ============================================================
# Hostile content — listings are scraped, not authored
# ============================================================
def test_scraped_content_is_escaped():
    hostile = 'Dev <script>alert(1)</script> & "co"'
    out = publish.render(hostile, PROFILE,
                         [{**JOB, "title": hostile, "company": hostile,
                           "angle": hostile, "risk": hostile}], DIST)

    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out


def test_a_hostile_url_cannot_break_out_of_the_href():
    out = publish.render("X", PROFILE,
                         [{**JOB, "url": 'https://x/"><script>bad()</script>'}],
                         DIST)
    assert "<script>bad()</script>" not in out


# ============================================================
# Self-contained — published pages run under a strict CSP
# ============================================================
def test_the_page_makes_no_external_requests(page):
    """A font CDN or remote image is blocked and fails silently."""
    for pattern in ("http://", "fonts.googleapis", "fonts.gstatic",
                    "cdn.", "<img", "<iframe", "@import"):
        assert pattern not in page, f"page reaches out via {pattern!r}"


def test_outbound_listing_links_are_allowed_but_safe(page):
    """Listing links are the one external thing — they must be safe."""
    assert 'rel="noopener noreferrer"' in page
    assert 'target="_blank"' in page


def test_the_page_is_a_complete_document(page):
    assert page.startswith("<!doctype html>")
    assert page.rstrip().endswith("</html>")
    assert page.count("<style>") == 1


def test_both_themes_are_defined(page):
    """The viewer's theme toggle must win in both directions."""
    assert "prefers-color-scheme:dark" in page
    assert 'data-theme="dark"' in page
    assert 'data-theme="light"' in page


# ============================================================
# Content rules the product depends on
# ============================================================
def test_every_role_argues_both_ways(page):
    assert "Why it might not" in page
    assert "Most junior person in the room." in page


def test_a_role_with_no_stated_risk_still_gets_the_section():
    out = publish.render("X", PROFILE,
                         [{**JOB, "risk": None,
                           "score_breakdown": {"trajectory_fit": 9,
                                               "creative_ownership": 9}}],
                         DIST)
    assert "Why it might not" in out
    assert "Read the listing before trusting that" in out


def test_a_low_experience_fit_is_not_published_as_a_weakness(page):
    """Same philosophy as the portal: a stretch is the goal."""
    against = page.split("Why it might not")[1].split("</ul>")[0]
    assert "xperience fit" not in against


def test_an_empty_board_says_so_rather_than_rendering_blank():
    out = publish.render("X", PROFILE, [], {"count": 0})
    assert "quiet market" in out


def test_the_page_states_it_never_applies_for_you(page):
    assert "yours to send" in page


# ============================================================
# Per-user isolation carries through to the files
# ============================================================
def test_one_users_page_contains_nothing_of_anothers(tmp_path):
    conn = db.connect(str(tmp_path / "pub.db"))
    try:
        a, _ = session.create_account(conn, "shivani", "pw")
        b, _ = session.create_account(conn, "suraj", "pw")
        db.save_profile(conn, a, PROFILE)
        db.save_profile(conn, b, {**PROFILE,
                                  "target": {"role_family": "Data Engineer"}})

        job_a = db.upsert_job(conn, {"url": "https://x/a",
                                     "title": "Brand Manager",
                                     "company": "Acme"})
        job_b = db.upsert_job(conn, {"url": "https://x/b",
                                     "title": "Pipeline Engineer",
                                     "company": "BCorp"})
        db.upsert_user_job(conn, a, job_a, {"score": 8.0})
        db.upsert_user_job(conn, b, job_b, {"score": 9.0})

        page_a = publish.build_for_user(conn, a, "Shivani")
        page_b = publish.build_for_user(conn, b, "Suraj")

        assert "Brand Manager" in page_a and "Pipeline Engineer" not in page_a
        assert "Pipeline Engineer" in page_b and "Brand Manager" not in page_b
        assert "Data Engineer" not in page_a
    finally:
        conn.close()


def test_ignored_roles_do_not_appear_on_the_published_page(tmp_path):
    conn = db.connect(str(tmp_path / "ig.db"))
    try:
        uid, _ = session.create_account(conn, "shivani", "pw")
        db.save_profile(conn, uid, PROFILE)
        job = db.upsert_job(conn, {"url": "https://x/1", "title": "Rejected",
                                   "company": "Acme"})
        db.upsert_user_job(conn, uid, job, {"score": 9.0})
        db.ignore_job(conn, uid, job, "not for me")

        page = publish.build_for_user(conn, uid, "Shivani")
        assert "Rejected" not in page
        assert "not for me" not in page
    finally:
        conn.close()
