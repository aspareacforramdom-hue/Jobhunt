"""
PHASE 2 — the portal.

FastAPI + Jinja2 + Tailwind over a CDN. Server-rendered rather than a
SPA: for a three-person tool it ships in a day and looks identical.

Auth is default-deny. The gate is middleware, not a per-route
decorator, so a new route is protected by existing and has to be added
to PUBLIC_PATHS deliberately to be exposed.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from core import board, constraints, db, legacy, personal, scheduler, session
from web import jobs as jobs_routes
from web.render import page, templates

PUBLIC_PATHS = {"/login"}

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def create_app(boot_scan=False, dry=False, db_path=None, single_user=None):
    if not isinstance(boot_scan, bool):
        raise TypeError(
            "create_app(boot_scan, dry, db_path): boot_scan must be a bool. "
            "To point the app at a database, pass db_path= by keyword.")
    single_user = db_path is None if single_user is None else single_user
    if single_user:
        conn = db.connect(db_path)
        try:
            personal.bootstrap(conn)
        finally:
            conn.close()
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Boot work must not be able to stop the portal from starting.
        # A half-readable data/*.json or an unexpected profile shape
        # raised out of here, and the failure mode was the whole app
        # refusing to come up with a traceback — for an optional import
        # and a scheduling hint. Both are reported and skipped instead,
        # which leaves a portal that still shows every job already
        # scored and a "Scan now" button that still works.
        decision = {"due": False, "reason": "could not be determined"}
        try:
            conn = db.connect(db_path)
            try:
                if not single_user:
                    _first_run_import(conn)
                decision = app.state.scanner.decide(conn)
            finally:
                conn.close()
        except Exception as e:                       # noqa: BLE001
            print(f"→ boot checks failed, serving anyway: {e}")

        print(f"→ boot scan: {decision['reason']}")
        if boot_scan and decision["due"]:
            # Serve immediately. The scrape runs on a background thread —
            # blocking the UI on a scrape is how a portal feels broken.
            app.state.scanner.start("catchup")
        yield

    app = FastAPI(title="Career Loop", docs_url=None, redoc_url=None,
                  lifespan=lifespan)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    app.state.db_path = db_path
    app.state.dry = dry
    app.state.single_user = single_user
    app.state.scanner = scheduler.Scanner(db_path=db_path, dry=dry,
        sources_path="config/personal_sources.yml" if single_user else "config/sources.yml")

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        request.state.user = None
        request.state.db_path = db_path
        if single_user:
            from urllib.parse import urlsplit
            host = request.url.hostname
            client = request.client.host if request.client else ""
            if host not in {"localhost", "127.0.0.1", "::1", "testserver"} or client not in {"127.0.0.1", "::1", "testclient"}:
                return PlainTextResponse("Local access only", status_code=403)
            origin = request.headers.get("origin")
            if request.method not in {"GET", "HEAD", "OPTIONS"} and (request.headers.get("sec-fetch-site") == "cross-site" or (origin and urlsplit(origin).netloc != request.url.netloc)):
                return PlainTextResponse("Cross-origin write refused", status_code=403)
            if request.url.path in {"/login", "/logout", "/intake"} or request.url.path.startswith("/intake/"):
                return RedirectResponse("/", status_code=303)
            conn = db.connect(db_path)
            try:
                request.state.user = personal.ensure_owner(conn)
            finally:
                conn.close()
            return await call_next(request)

        # The stylesheet has to load on the sign-in page, so /static is
        # exempt. It is the only prefix exemption — everything else is
        # matched exactly, so a new route is gated by default.
        is_static = request.url.path.startswith("/static/")

        # Static files belong to nobody, so resolving a user for them is
        # a database connection opened and closed per stylesheet, font
        # and icon on every page load. Skip it — and note the skip is
        # only reached for paths that are already exempt from the gate
        # below, so it cannot widen what is public.
        if not is_static:
            conn = db.connect(db_path)
            try:
                request.state.user = session.user_from_cookie(
                    conn, request.cookies.get(session.COOKIE_NAME))
            finally:
                conn.close()

            if request.state.user is None \
                    and request.url.path not in PUBLIC_PATHS:
                return RedirectResponse("/login", status_code=303)

        return await call_next(request)

    # ------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------
    @app.get("/login")
    def login_form(request: Request):
        if request.state.user:
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request, "login.html",
            {"error": request.query_params.get("error")})

    @app.post("/login")
    def login(request: Request, username: str = Form(...),
              password: str = Form(...)):
        conn = db.connect(db_path)
        try:
            user = session.authenticate(conn, username, password)
        finally:
            conn.close()

        if not user:
            # Deliberately does not say which half was wrong, and the
            # password never reaches a log line.
            return RedirectResponse("/login?error=1", status_code=303)

        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(session.COOKIE_NAME, session.issue_session(user["id"]),
                        httponly=True, samesite="lax",
                        max_age=session.SESSION_MAX_AGE)
        return resp

    @app.get("/logout")
    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(session.COOKIE_NAME)
        return resp

    # ------------------------------------------------------------
    # Board
    # ------------------------------------------------------------
    @app.get("/")
    def index(request: Request):
        user_id = request.state.user["id"]
        conn = db.connect(db_path)
        try:
            data = None if single_user else board.build_board(conn, user_id)
            profile = db.load_profile(conn, user_id)
            counts = db.status_counts(conn, user_id)
            if single_user:
                requested_status = request.query_params.get("status", "")
                excluded_view = requested_status == "excluded"
                matched_total = conn.execute(
                    "SELECT COUNT(*) FROM user_jobs WHERE user_id=? AND score IS NOT NULL",
                    (user_id,)).fetchone()[0]
                excluded_total = conn.execute(
                    "SELECT COUNT(*) FROM user_jobs WHERE user_id=? "
                    "AND json_extract(evaluation_json, '$.eligible') = 0",
                    (user_id,)).fetchone()[0]
                jobs = db.list_user_jobs(conn, user_id, exclude_ignored=False,
                                         include_description=True,
                                         eligible_only=not excluded_view)
                current_revision = personal.revision(profile or {})
                jobs = [job for job in jobs
                        if job["status"] != "new"
                        or job.get("score") is None
                        or job.get("profile_revision") == current_revision]
                for job in jobs:
                    stored = job.get("evaluation") or {}
                    current = constraints.assess(job, profile or {})
                    job["evaluation"] = {**stored, **current,
                                         "eligible": stored.get("eligible", True) and current["eligible"]}
                    job["score_stale"] = job.get("score") is not None and job.get("profile_revision") != current_revision
                jobs = [j for j in jobs if (not j["evaluation"]["eligible"]
                        if excluded_view else j["evaluation"]["eligible"] or j["status"] != "new")]
                counts = {}
                for job in jobs:
                    counts[job["status"]] = counts.get(job["status"], 0) + 1
                query = request.query_params.get("q", "").strip().lower()
                status = requested_status
                jobs = [j for j in jobs if (not status or j["status"] == status
                        or status == "excluded"
                        or (status == "unrated" and j.get("score") is None))
                        and (status or j["status"] != "ignored")
                        and (not query or query in ((j.get("title") or "") + " " + (j.get("company") or "")).lower())]
                total_matches = len(jobs)
                try:
                    result_page = int(request.query_params.get("p", "1"))
                except ValueError:
                    result_page = 1
                result_pages = max(1, (total_matches + 199) // 200)
                result_page = max(1, min(result_page, result_pages))
                jobs = jobs[(result_page - 1) * 200:result_page * 200]
        finally:
            conn.close()

        if single_user:
            from core.apify_budget import snapshot
            return page(request, "personal.html", jobs=jobs, profile=profile,
                        counts=counts, query=query, selected_status=status,
                        total_matches=total_matches,
                        matched_total=matched_total, excluded_total=excluded_total,
                        result_page=result_page, result_pages=result_pages,
                        apify_budget=snapshot())
        return page(request, "board.html", board=data, profile=profile,
                    ignored_count=counts.get("ignored", 0))

    # ------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------
    @app.post("/scan")
    def manual_scan(request: Request, mode: str = Form("manual")):
        """
        The cooldown is advisory here. The button is disabled during it,
        but an explicit override is allowed — it is the user's money.
        """
        _started, why = request.app.state.scanner.start(
            "discover" if mode == "discover" else "manual", override=True)
        from urllib.parse import quote
        return RedirectResponse("/?msg=" + quote(str(why), safe=""), status_code=303)

    @app.post("/score")
    def score_saved(request: Request):
        _started, why = request.app.state.scanner.start("score", override=True)
        from urllib.parse import quote
        return RedirectResponse("/?msg=" + quote(str(why), safe=""), status_code=303)

    @app.post("/preferences")
    def preferences(request: Request, location: str = Form(...),
                    salary: float = Form(...)):
        import math
        choices = {"bengaluru": ["Bengaluru, India", "Remote India"],
                   "remote": ["Remote India"],
                   "both": ["Mumbai, India", "Bengaluru, India", "Remote India"]}
        if not single_user or location not in choices or not math.isfinite(salary) or not 0 < salary < 200:
            return PlainTextResponse("Invalid preferences", status_code=400)
        conn = db.connect(db_path)
        try:
            profile = db.load_profile(conn, request.state.user["id"])
            if not profile:
                return PlainTextResponse("Profile missing", status_code=409)
            profile.setdefault("hard_filters", {}).update(locations=choices[location], min_ctc_lpa=salary)
            profile["uncertainties"] = [s for s in profile.get("uncertainties", [])
                                        if not s.startswith(("Confirm Bengaluru", "Confirm Mumbai", "Confirm 25 LPA"))]
            if location == "both":
                profile["uncertainties"].append(
                    "Confirm Mumbai relocation and office arrangement before applying.")
            personal.save(conn, profile)
        finally:
            conn.close()
        return RedirectResponse("/?msg=Preferences+saved", status_code=303)

    @app.get("/scan/status")
    def scan_status(request: Request):
        """Polled by the header so the status updates without a reload."""
        return request.app.state.scanner.status()

    @app.post("/sources/scan")
    def source_scan(request: Request):
        _started, message = request.app.state.scanner.start("manual", override=True)
        from urllib.parse import quote
        return RedirectResponse("/sources?msg=" + quote(message), status_code=303)

    @app.get("/sources")
    def sources_panel(request: Request, q: str = "", state: str = "", p: int = 1):
        from core import source_registry, firecrawl, firecrawl_budget
        conn = db.connect(db_path)
        try:
            profile = db.load_profile(conn, request.state.user["id"]) or {}
            source_registry.seed_curated(conn)
            registry = source_registry.inventory(conn, q, state, p)
            firecrawl_status = {}
            try:
                firecrawl_status = firecrawl_budget.snapshot()
                firecrawl_status["configured"] = firecrawl.available()
                firecrawl_status["leads"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE source='firecrawl'").fetchone()[0]
                firecrawl_status["scored"] = conn.execute("SELECT COUNT(*) FROM user_jobs u JOIN jobs j ON j.id=u.job_id WHERE j.source='firecrawl'").fetchone()[0]
            except Exception:
                firecrawl_status = {"error": "Firecrawl usage ledger unavailable; check scan messages"}
        finally:
            conn.close()
        return page(request, "sources.html", registry=registry, query=q, selected_state=state,
                    profile=profile,
                    firecrawl_status=firecrawl_status)

    jobs_routes.register(app, db_path)
    return app


def _first_run_import(conn):
    """Carry the single-user JSON era over, once, if nobody has yet."""
    if not legacy.pending():
        return
    already = conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
    if already:
        return
    users = db.all_users(conn)
    if not users:
        return
    print("→ first run with data/*.json present — importing to "
          f"'{users[0]['username']}'")
    legacy.migrate_json(conn, users[0]["id"])
