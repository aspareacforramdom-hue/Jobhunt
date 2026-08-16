"""
Job routes: the list, the detail page, the board actions, the prepare
screen, and the export.

Every handler resolves its user id from request.state.user and passes it
as the first argument to the db helper. There is no path where a job id
alone is enough to read a row.
"""

import math
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import Form, Request
from fastapi.responses import RedirectResponse, Response

from core import db, reviews, tailor
from web import export as export_mod
from web import intake_routes
from web.render import breakdown_rows, page


# The list page renders every row it is given. At 800 scored listings
# that is an 800-row table nobody scrolls, built and shipped on every
# filter change. The export deliberately keeps no cap — a spreadsheet is
# where you go when you actually want all of them.
LIST_LIMIT = 250


def register(app, db_path=None):

    def _conn():
        return db.connect(db_path)

    def _uid(request):
        return request.state.user["id"]

    # ------------------------------------------------------------
    # List
    # ------------------------------------------------------------
    @app.get("/jobs")
    def job_list(request: Request):
        q = request.query_params
        conn = _conn()
        try:
            rows = db.list_user_jobs(
                conn, _uid(request),
                min_score=_float(q.get("min_score")),
                company=q.get("company") or None,
                source=q.get("source") or None,
                since=_since(q.get("days")),
                search=q.get("q") or None,
                order=q.get("order") or "score",
                limit=LIST_LIMIT + 1)
            sources = [r["source"] for r in conn.execute(
                "SELECT DISTINCT source FROM jobs WHERE source IS NOT NULL")]
        finally:
            conn.close()

        truncated = len(rows) > LIST_LIMIT
        return page(request, "jobs.html", jobs=rows[:LIST_LIMIT],
                    sources=sources, filters=dict(q),
                    truncated=truncated, limit=LIST_LIMIT)

    @app.get("/ignored")
    def ignored(request: Request):
        conn = _conn()
        try:
            rows = db.list_user_jobs(conn, _uid(request),
                                     statuses=["ignored"], order="date")
        finally:
            conn.close()
        return page(request, "ignored.html", jobs=rows)

    # ------------------------------------------------------------
    # Detail
    # ------------------------------------------------------------
    @app.get("/jobs/{job_id}")
    def job_detail(request: Request, job_id: int):
        user_id = _uid(request)
        conn = _conn()
        try:
            job = db.get_user_job(conn, user_id, job_id)
            if not job:
                return RedirectResponse("/jobs?msg=Not+found", status_code=303)
            profile = db.load_profile(conn, user_id)
            company = db.get_company(conn, job.get("company"))
            application = db.get_application(conn, user_id, job_id)
        finally:
            conn.close()

        rows = breakdown_rows(job, profile)
        return page(request, "detail.html", job=job, profile=profile,
                    breakdown=rows, company=company,
                    application=application,
                    reasons_against=_reasons_against(job, rows))

    # ------------------------------------------------------------
    # Board actions
    # ------------------------------------------------------------
    @app.post("/jobs/{job_id}/status")
    def set_status(request: Request, job_id: int, status: str = Form(...),
                   back: str = Form("/")):
        # The status select is rendered by us, but the POST is not — an
        # unknown value used to reach db.set_status and raise, turning a
        # typo into a 500.
        if status not in db.STATUSES:
            return RedirectResponse(_safe_back(back) + "?msg=Unknown+status",
                                    status_code=303)
        conn = _conn()
        try:
            if db.get_user_job(conn, _uid(request), job_id):
                db.set_status(conn, _uid(request), job_id, status)
        finally:
            conn.close()
        return RedirectResponse(_safe_back(back), status_code=303)

    @app.post("/jobs/{job_id}/ignore")
    def ignore(request: Request, job_id: int, reason: str = Form(""),
               back: str = Form("/")):
        conn = _conn()
        try:
            if db.get_user_job(conn, _uid(request), job_id):
                db.ignore_job(conn, _uid(request), job_id,
                              (reason or "").strip()[:500])
        finally:
            conn.close()
        return RedirectResponse(_safe_back(back), status_code=303)

    @app.post("/jobs/{job_id}/restore")
    def restore(request: Request, job_id: int, back: str = Form("/ignored")):
        conn = _conn()
        try:
            if db.get_user_job(conn, _uid(request), job_id):
                db.restore_job(conn, _uid(request), job_id)
        finally:
            conn.close()
        return RedirectResponse(_safe_back(back, "/ignored"), status_code=303)

    @app.post("/jobs/{job_id}/reviews")
    def fetch_reviews(request: Request, job_id: int):
        """On-demand review lookup for a company the user is looking at."""
        conn = _conn()
        try:
            job = db.get_user_job(conn, _uid(request), job_id)
            if not job:
                return RedirectResponse("/jobs?msg=Not+found", status_code=303)
            if request.app.state.dry:
                return RedirectResponse(
                    f"/jobs/{job_id}?msg=Dry+run+—+nothing+fetched",
                    status_code=303)
            if not reviews.available():
                return RedirectResponse(
                    f"/jobs/{job_id}?msg=APIFY_TOKEN+not+set+—+reviews+"
                    "unavailable", status_code=303)
            reviews.enrich(conn, job.get("company"), force=True)
        finally:
            conn.close()
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    # ------------------------------------------------------------
    # Prepare  (Phase 3)
    # ------------------------------------------------------------
    @app.post("/jobs/{job_id}/prepare")
    def prepare(request: Request, job_id: int):
        if request.app.state.dry:
            return RedirectResponse(
                f"/jobs/{job_id}?msg=Dry+run+—+no+LLM+calls+made",
                status_code=303)

        user_id = _uid(request)
        conn = _conn()
        try:
            job = db.get_user_job(conn, user_id, job_id)
            if not job:
                return RedirectResponse("/jobs?msg=Not+found", status_code=303)
            profile = db.load_profile(conn, user_id)

            try:
                tailored, outreach = tailor.prepare_application(job, profile)
            except Exception as e:
                return RedirectResponse(
                    f"/jobs/{job_id}?msg={_msg('Could not prepare: ', e)}",
                    status_code=303)

            db.save_application(conn, user_id, job_id, tailored, outreach)
            if job["status"] in ("new", "interested"):
                db.set_status(conn, user_id, job_id, "prepared")
        finally:
            conn.close()

        return RedirectResponse(f"/applications/{job_id}", status_code=303)

    @app.get("/applications/{job_id}")
    def application_detail(request: Request, job_id: int):
        user_id = _uid(request)
        conn = _conn()
        try:
            job = db.get_user_job(conn, user_id, job_id)
            application = db.get_application(conn, user_id, job_id)
            if not job or not application:
                return RedirectResponse("/applications?msg=Not+prepared+yet",
                                        status_code=303)
            # Only the fact, never the text — the page must not carry a
            # copy of someone's CV around just to decide on a sentence.
            profile = db.load_profile(conn, user_id) or {}
            has_resume = bool((profile.get("resume_text") or "").strip())
        finally:
            conn.close()
        return page(request, "prepare.html", job=job,
                    application=application, has_resume=has_resume)

    @app.get("/applications")
    def applications(request: Request):
        conn = _conn()
        try:
            rows = db.list_applications(conn, _uid(request))
        finally:
            conn.close()
        return page(request, "applications.html", applications=rows,
                    statuses=db.STATUSES)

    # ------------------------------------------------------------
    # Export
    # ------------------------------------------------------------
    @app.get("/export.xlsx")
    def export(request: Request):
        q = request.query_params
        conn = _conn()
        try:
            rows = db.list_user_jobs(
                conn, _uid(request),
                min_score=_float(q.get("min_score")),
                company=q.get("company") or None,
                source=q.get("source") or None,
                since=_since(q.get("days")),
                search=q.get("q") or None,
                order=q.get("order") or "score")
            profile = db.load_profile(conn, _uid(request))
        finally:
            conn.close()

        blob, media, ext = export_mod.build(rows, profile)
        return Response(blob, media_type=media, headers={
            "content-disposition": f'attachment; filename="careerloop.{ext}"'})

    intake_routes.register(app, db_path)


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def _safe_back(back, default="/"):
    """
    `back` decides where the browser lands after an action, and it comes
    in on the form. Left unchecked it is an open redirect: a link that
    looks like the portal can drop the user on a lookalike login page
    with a password already in mind.

    Only same-origin absolute paths are allowed. `//host` and `/\\host`
    are both protocol-relative and go off-site, so a second character of
    `/` or `\\` disqualifies it too.
    """
    back = (back or "").strip()
    if not back.startswith("/"):
        return default
    if len(back) > 1 and back[1] in "/\\":
        return default
    if any(c in back for c in "\r\n\t\x00") or "\\" in back:
        return default
    return back[:500]


def _msg(prefix, detail):
    """
    Turn an exception into something worth showing a person.

    Starlette percent-encodes the Location value and Jinja escapes the
    flash, so this is not a safety fix — the raw version was merely
    unreadable. An API error body arrives multi-line and kilobytes long,
    and it went into the URL whole, so the user got a wall of `%0A` and
    JSON where a sentence belonged. Capped, flattened, and encoded so it
    cannot be read as further query parameters.
    """
    return quote(" ".join(f"{prefix}{detail}".split())[:240], safe="")


def _float(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _since(days):
    try:
        n = int(days)
    except (TypeError, ValueError):
        return None
    # A huge `days` overflows timedelta; a negative one asks for the
    # future, which silently returns nothing.
    n = max(0, min(n, 3650))
    try:
        return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _reasons_against(job, breakdown):
    """
    Every job gets a reasons-against section.

    A UI that makes every listing feel great produces bad applications,
    so the weakest dimension is surfaced whether or not the scorer
    volunteered a risk. When there genuinely is nothing, the page says
    so rather than hiding the section.
    """
    reasons = []

    if job.get("risk"):
        reasons.append(job["risk"])

    # The weakest dimension, named out loud. experience_fit is excluded
    # upstream — a stretch role scores low there by definition, and
    # calling that a weakness contradicts the badge above it.
    weak = next((r for r in breakdown if r["is_weakest"]), None)
    if weak and weak["score"] <= 6:
        reasons.append(
            f"Weakest on {weak['label'].lower()} — {weak['score']:.0f}/10, "
            f"and you weighted that {weak['weight']}% of the score.")

    if job.get("company_review_count") and job["company_review_count"] < 20:
        reasons.append(
            f"Only {job['company_review_count']} employee reviews exist for "
            f"this company. That is an anecdote, not a signal.")

    return reasons
