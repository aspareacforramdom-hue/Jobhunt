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
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from core import board, db, legacy, scheduler, session
from web import jobs as jobs_routes
from web.render import page, templates

PUBLIC_PATHS = {"/login"}

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def create_app(boot_scan=True, dry=False, db_path=None):

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
    app.state.scanner = scheduler.Scanner(db_path=db_path, dry=dry)

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        request.state.user = None
        request.state.db_path = db_path

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
            data = board.build_board(conn, user_id)
            profile = db.load_profile(conn, user_id)
            counts = db.status_counts(conn, user_id)
        finally:
            conn.close()

        return page(request, "board.html", board=data, profile=profile,
                    ignored_count=counts.get("ignored", 0))

    # ------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------
    @app.post("/scan")
    def manual_scan(request: Request):
        """
        The cooldown is advisory here. The button is disabled during it,
        but an explicit override is allowed — it is the user's money.
        """
        started, why = request.app.state.scanner.start(
            "manual", override=True)
        return RedirectResponse(f"/?msg={why}", status_code=303)

    @app.get("/scan/status")
    def scan_status(request: Request):
        """Polled by the header so the status updates without a reload."""
        return request.app.state.scanner.status()

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
