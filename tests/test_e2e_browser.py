"""
END-TO-END, in a real browser.

tests/test_portal.py drives the routes through TestClient, which never
executes CSS or JavaScript. This one boots a real uvicorn server and
walks a real Chromium through the daily rhythm: sign in, read the board,
open a listing, ignore it, restore it, move it between columns, filter,
export.

It also checks the things only a browser can tell you — that the fonts
resolved, that no page overflows horizontally at phone width, and that
the console stayed clean.

Skipped unless playwright is installed, so the normal suite stays light:
    pip install playwright && playwright install chromium
"""

import socket
import threading
import time

import pytest

from core import db, session

playwright_api = pytest.importorskip("playwright.sync_api",
                                     reason="playwright not installed")
sync_playwright = playwright_api.sync_playwright


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """A real uvicorn on a real port, with two users and three listings."""
    import uvicorn

    from web.app import create_app

    path = str(tmp_path_factory.mktemp("e2e") / "e2e.db")
    conn = db.connect(path)
    alice, _ = session.create_account(conn, "alice", "pw-alice")
    bob, _ = session.create_account(conn, "bob", "pw-bob")

    db.save_profile(conn, alice, {
        "target": {"role_family": "Brand Marketing Manager"},
        "hard_filters": {"locations": ["Mumbai"]},
        "weights": {"trajectory_fit": 30, "creative_ownership": 30,
                    "brand_scale": 20, "company_quality": 12,
                    "experience_fit": 8},
        "dimension_definitions": {
            "creative_ownership": "Owns creative direction, or executes?",
            "brand_scale": "How large and consumer-visible is the brand?"},
        "angles": ["Sold it face to face for six years"],
        "search_terms": ["Brand Manager"], "dropped": []})

    for url, title, company, score, breakdown, risk in [
        ("https://e/1", "Brand Manager", "Tata Consumer", 8.4,
         {"trajectory_fit": 9, "creative_ownership": 8, "brand_scale": 9,
          "company_quality": 8, "experience_fit": 5},
         "You would be the most junior person in the room."),
        ("https://e/2", "Head of Brand", "Zepto", 7.9,
         {"trajectory_fit": 9, "creative_ownership": 9, "brand_scale": 4,
          "company_quality": 7, "experience_fit": 3}, None),
        ("https://e/3", "Marketing Executive", "Local Co", 4.6,
         {"trajectory_fit": 4, "creative_ownership": 3, "brand_scale": 4,
          "company_quality": 5, "experience_fit": 9}, None),
    ]:
        job_id = db.upsert_job(conn, {
            "url": url, "title": title, "company": company,
            "location": "Mumbai", "source": "greenhouse",
            "description": "Listing body.", "found_at": db.now()})
        db.upsert_user_job(conn, alice, job_id, {
            "score": score, "verdict": f"Verdict for {title}.",
            "angle": "Six years of it, face to face.", "stretch": score > 7,
            "risk": risk, "score_breakdown": breakdown})

    # Bob's own listing — must never appear on Alice's screen.
    bob_job = db.upsert_job(conn, {"url": "https://e/bob",
                                   "title": "Bobs Secret Role",
                                   "company": "BobCorp", "found_at": db.now()})
    db.upsert_user_job(conn, bob, bob_job, {"score": 9.9})
    conn.close()

    port = _free_port()
    app = create_app(boot_scan=False, db_path=path)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()

    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        pytest.fail("server did not start")

    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


@pytest.fixture(scope="module")
def page(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        pg = ctx.new_page()

        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.on("console", lambda m: pg.errors.append(m.text)
              if m.type == "error" else None)

        pg.goto(f"{live_server}/login")
        pg.fill("input[name=username]", "alice")
        pg.fill("input[name=password]", "pw-alice")
        pg.click("button")
        pg.wait_for_load_state("networkidle")

        yield pg
        browser.close()


# ------------------------------------------------------------
# The design system actually resolved
# ------------------------------------------------------------
def test_the_three_fonts_load(page):
    missing = page.evaluate("""() => ['Fraunces','Newsreader','IBM Plex Mono']
        .filter(f => !document.fonts.check('16px "' + f + '"'))""")
    assert missing == [], f"fonts fell back to a system stack: {missing}"


def test_the_stylesheet_is_applied_not_just_linked(page):
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg == "rgb(250, 248, 243)", "the paper background did not apply"


def test_scores_render_in_mono_so_a_4_cannot_read_as_a_1(page):
    """Fraunces' display `4` loses its apex at small sizes. Scores are
    compared down a column, so they must not be ambiguous."""
    family = page.evaluate(
        "getComputedStyle(document.querySelector('.entry__score')).fontFamily")
    assert "Plex Mono" in family


# ------------------------------------------------------------
# The daily rhythm
# ------------------------------------------------------------
def test_the_board_shows_this_users_jobs_and_not_the_other_users(page):
    body = page.inner_text("body")
    assert "Brand Manager" in body
    assert "Head of Brand" in body
    assert "Bobs Secret Role" not in body


def test_the_absolute_floor_keeps_junk_off_the_board(page, live_server):
    """4.6 is above the 4.0 floor, so it stays — the board is never
    empty just because a run scored flat."""
    page.goto(f"{live_server}/")
    assert "Marketing Executive" in page.inner_text("body")


def test_opening_a_listing_shows_the_users_own_dimensions(page, live_server):
    page.goto(f"{live_server}/")
    page.click("text=Brand Manager")
    page.wait_for_load_state("networkidle")

    body = page.inner_text("body")
    assert "Creative ownership" in body
    assert "Brand scale" in body
    assert "Ai product depth" not in body


def test_the_detail_page_argues_both_ways(page, live_server):
    page.goto(f"{live_server}/jobs/1")
    body = page.inner_text("body")

    assert "Why it fits" in body
    assert "Why it might not" in body
    assert "most junior person in the room" in body


def test_a_low_experience_fit_is_not_listed_as_a_reason_against(
        page, live_server):
    """Job 2 scores 3/10 on experience_fit — that is the stretch this
    tool exists to find, not an argument against applying."""
    page.goto(f"{live_server}/jobs/2")
    reasons = page.inner_text(".reasons") if page.query_selector(".reasons") \
        else ""

    assert "experience fit" not in reasons.lower()
    assert "brand scale" in reasons.lower(), \
        "the genuinely weak dimension should be named instead"


def test_ignore_then_restore_round_trips_with_the_reason(page, live_server):
    page.goto(f"{live_server}/jobs/3")
    page.fill("input[name=reason]", "sideways move")
    page.click("button:has-text('Ignore')")
    page.wait_for_load_state("networkidle")

    assert "Marketing Executive" not in page.inner_text("body")

    page.goto(f"{live_server}/ignored")
    assert "sideways move" in page.inner_text("body")

    page.click("button:has-text('Restore')")
    page.wait_for_load_state("networkidle")
    page.goto(f"{live_server}/")
    assert "Marketing Executive" in page.inner_text("body")


def test_moving_a_job_to_interested_moves_it_between_columns(page,
                                                             live_server):
    page.goto(f"{live_server}/jobs/2")
    page.click("button:has-text('Interested')")
    page.wait_for_load_state("networkidle")

    page.goto(f"{live_server}/")
    column = page.inner_text(".column:nth-child(2)")
    assert "Head of Brand" in column


def test_filtering_the_list_narrows_it(page, live_server):
    page.goto(f"{live_server}/jobs?min_score=8")
    body = page.inner_text("body")

    assert "Brand Manager" in body
    assert "Marketing Executive" not in body


def test_the_export_downloads_a_spreadsheet(page, live_server):
    page.goto(f"{live_server}/jobs?min_score=8")
    with page.expect_download() as dl:
        page.click("a:has-text('Export')")
    assert dl.value.suggested_filename.endswith((".xlsx", ".csv"))


# ------------------------------------------------------------
# Layout integrity
# ------------------------------------------------------------
@pytest.mark.parametrize("width", [390, 820, 1400])
def test_no_page_scrolls_sideways_at_any_width(page, live_server, width):
    page.set_viewport_size({"width": width, "height": 900})
    for path in ["/", "/jobs", "/ignored", "/applications", "/intake",
                 "/jobs/1"]:
        page.goto(f"{live_server}{path}")
        over = page.evaluate("document.documentElement.scrollWidth > "
                             "document.documentElement.clientWidth")
        assert not over, f"{path} overflows horizontally at {width}px"
    page.set_viewport_size({"width": 1400, "height": 900})


def test_row_actions_are_reachable_by_keyboard(page, live_server):
    """Board actions fade in on hover, so they must also appear on focus
    or they are unreachable without a mouse."""
    page.goto(f"{live_server}/")
    page.focus(".entry button")
    page.wait_for_timeout(400)      # the reveal is a transition, not a jump
    opacity = page.evaluate(
        "getComputedStyle(document.querySelector('.entry__actions')).opacity")
    assert float(opacity) == 1.0


def test_the_console_stayed_clean_throughout(page):
    assert page.errors == []
