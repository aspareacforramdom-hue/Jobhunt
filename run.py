#!/usr/bin/env python3
"""
CAREER LOOP  --  9-stage job intelligence machine (India-first)

    python run.py serve                the portal (three users, local)
    python run.py seed-users           create the three accounts
    python run.py warm-up              download the shortlisting model
    python run.py discover-companies   find which employers to watch (free)
    python run.py set-telegram         --user NAME --chat ID
    python run.py publish              standalone HTML, one page per user

    python run.py intake --cv cv.txt   Stage 0-1  profile + compile
    python run.py calibrate            Stage 2    market reality check
    python run.py scan                 Stage 3-8  the daily loop
    python run.py log <url> <status>   Stage 9    record an outcome
    python run.py review               Stage 9    diagnose the search
    python run.py migrate --user NAME  import the old data/*.json

    python run.py scan  --dry          no LLM, no send, no cost
    python run.py serve --no-scan      portal without the boot scan
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Every module prints arrows and box-drawing characters. On Windows the
# console defaults to cp1252, which raises UnicodeEncodeError the moment
# stdout is piped to a file or another process — so `run.py serve > log`
# would die on its first print. Force UTF-8 before anything runs.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import yaml

from core import (calibrate as calib, db, discover, feedback,
                  filter as flt, intake, legacy, notify, prefilter,
                  publish, score, session, tailor)


def cmd_intake(args):
    cv = args[args.index("--cv") + 1] if "--cv" in args else "cv.txt"
    if not os.path.exists(cv):
        print(f"CV not found: {cv}")
        return
    intake.run_intake(cv)


def cmd_calibrate(args):
    profile = intake.load_profile()
    if not profile:
        print("Run `python run.py intake --cv yourcv.txt` first.")
        return
    c = calib.calibrate(profile, force="--force" in args)
    calib.print_calibration(c)

    # Search terms live on the profile, not in global config — see
    # config/sources.yml for why.
    calib.attach_search_terms(profile, c)
    with open(intake.PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)
    print(f"→ {len(profile['search_terms'])} search terms saved to your profile")


def cmd_scan(args):
    dry = "--dry" in args

    profile = intake.load_profile()
    if not profile:
        print("Run `python run.py intake --cv yourcv.txt` first.")
        return

    with open("config/sources.yml", encoding="utf-8") as f:
        sources = yaml.safe_load(f)

    terms = profile.get("search_terms") or []
    if not terms:
        print("No search terms on your profile. Run `python run.py calibrate`.")
        return
    sources.setdefault("jobspy", {})["search_terms"] = terms
    sources["jobspy"].setdefault(
        "locations",
        profile.get("hard_filters", {}).get("locations")
        or sources["jobspy"].get("default_locations", []))
    print(f"-> using {len(terms)} market-calibrated search terms")

    raw, notes = discover.discover_all(sources, dry=dry)
    for n in notes:
        print(f"   {n}")

    fresh, seen = flt.filter_jobs(raw, {
        "exclude_keywords": profile.get("hard_filters", {}).get("exclude_keywords", [])
    })

    candidates = prefilter.prefilter(fresh, profile)

    if dry:
        for j in candidates[:10]:
            print(f"  [{j['_prefilter_signal']:.1f}] {j['title']} @ {j['company']}")
        print(f"\n[dry] {len(candidates)} would be scored. Nothing saved.")
        return

    if not candidates:
        notify.send_digest([], profile)
        return

    scored = score.score_jobs(candidates, profile)

    floor = profile.get("min_score", 6.5)
    ranked = [j for j in scored if j["score"] >= floor]
    print(f"→ {len(ranked)} cleared the {floor} bar (of {len(scored)} scored)")

    if ranked:
        print("-> preparing applications for top 3...")
        tailor.prepare_top(ranked, profile, n=3)

    os.makedirs("data", exist_ok=True)
    with open("data/latest.json", "w", encoding="utf-8") as f:
        json.dump(ranked, f, indent=2, default=str)
    flt.save_seen(seen)

    notify.send_digest(ranked, profile)
    print("-> done")


def cmd_seed_users(args):
    """Create the portal accounts. Passwords are shown once and never
    written anywhere — not to a file, not to a log."""
    names = [a for a in args if not a.startswith("--")] or \
        ["user1", "user2", "user3"]

    conn = db.connect()
    created, skipped = session.seed_users(conn, names)

    if skipped:
        print(f"→ already exist, left untouched: {', '.join(skipped)}")

    if created:
        print("\n" + "=" * 58)
        print("  ACCOUNTS CREATED — copy these now, they are not stored")
        print("=" * 58)
        for u in created:
            admin = "  (admin)" if u["is_admin"] else ""
            print(f"\n  username: {u['username']}{admin}")
            print(f"  password: {u['password']}")
        print("\n" + "=" * 58)
        print("  Nothing above is recoverable. Re-running this command")
        print("  will NOT reset an existing account's password.")
        print("=" * 58 + "\n")

    # Carry the single-user JSON era over to whoever came first.
    if created and legacy.pending():
        target = created[0]
        print(f"→ found data/*.json — importing to '{target['username']}'")
        legacy.migrate_json(conn, target["id"])

    conn.close()


def cmd_serve(args):
    """
    Serve the portal. The boot catch-up scan runs on a background
    thread — the UI must never wait on a scrape.
    """
    import uvicorn

    from web.app import create_app

    host = args[args.index("--host") + 1] if "--host" in args else "127.0.0.1"
    port = int(args[args.index("--port") + 1] if "--port" in args else 8000)

    app = create_app(boot_scan="--no-scan" not in args, dry="--dry" in args)
    print(f"→ Career Loop portal on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


def cmd_publish(args):
    """
    Write each user's board to a standalone HTML file.

    The files are self-contained, so they can be hosted anywhere — or
    read straight off disk. Nothing is uploaded from here; publishing
    where other people can see it stays a deliberate, separate act.
    """
    out_dir = args[args.index("--out") + 1] if "--out" in args else "published"
    os.makedirs(out_dir, exist_ok=True)

    conn = db.connect()
    try:
        written = []
        for user in db.all_users(conn):
            name = user["username"]
            profile = db.load_profile(conn, user["id"])
            if not profile:
                print(f"  skipped {name}: no profile yet")
                continue

            page = publish.build_for_user(
                conn, user["id"], name.title(),
                note=("Scores on this page are placeholder — the pipeline "
                      "has not been run against a live model yet.")
                if "--placeholder" in args else None)

            path = os.path.join(out_dir, f"{name}.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(page)
            written.append((name, path, len(page)))
    finally:
        conn.close()

    print()
    for name, path, size in written:
        print(f"  {name:<10} {path}  ({size / 1024:.0f} KB)")
    if written:
        print("\nSelf-contained — no logins, no server, no external requests.")
    return written


def cmd_migrate(args):
    """Import data/*.json into the database for one named user."""
    if "--user" not in args:
        print("usage: run.py migrate --user <username>")
        return
    username = args[args.index("--user") + 1]

    conn = db.connect()
    try:
        user = db.get_user_by_name(conn, username)
        if not user:
            print(f"No such user: {username}. Run `run.py seed-users` first.")
            return
        legacy.migrate_json(conn, user["id"])
    finally:
        conn.close()


def cmd_discover_companies(args):
    """
    Work out which employers to watch, instead of typing them into YAML.

        run.py discover-companies                    every user
        run.py discover-companies --user alice
        run.py discover-companies --names "Swiggy,Zomato,Cred"
        run.py discover-companies --list

    One LLM call per user turns their profile into employer names; those
    names are then probed against five free, no-auth ATS APIs. The
    probing is the useful half and it costs nothing — a company either
    answers with live jobs or it does not.

    Not part of a scan. Run it monthly, or after someone's intake
    changes.
    """
    from core import companies

    conn = db.connect()
    try:
        if "--list" in args:
            boards = db.list_job_boards(conn)
            if not boards:
                print("No boards discovered yet.")
                return
            print(f"{len(boards)} board(s) being watched:\n")
            for b in boards:
                mark = " ~loose" if b["confidence"] == "loose" else ""
                print(f"  {b['platform']:16} {b['slug']:22} "
                      f"{b['jobs_seen']:4} jobs  {b['company_name'] or ''}{mark}")
            return

        names = []
        if "--names" in args:
            names = [n.strip() for n in
                     args[args.index("--names") + 1].split(",") if n.strip()]

        users = db.all_users(conn)
        if "--user" in args:
            wanted = args[args.index("--user") + 1]
            users = [u for u in users if u["username"] == wanted]
            if not users:
                print(f"No such user: {wanted}")
                return

        total = 0
        for user in users:
            profile = db.load_profile(conn, user["id"])
            if not profile and not names:
                print(f"→ {user['username']}: no profile yet, skipped")
                continue
            print(f"→ {user['username']}")
            found = companies.discover_for_user(conn, user["id"], profile,
                                                extra_names=names)
            total += len(found)
            for b in found[:10]:
                print(f"     {b['platform']:16} {b['slug']:22} "
                      f"{b['jobs']:4} jobs")

        print(f"\n→ {total} board(s) recorded. They join every scan from "
              f"now on — see `run.py discover-companies --list`.")
    finally:
        conn.close()


def cmd_warm_up(args):
    """
    Download the shortlisting model, once, on purpose.

    A scan never does this itself. Fetching 30MB is a fine thing to do
    when someone typed a command and is watching; it is not a fine thing
    to do inside a background scan that is supposed to be quick, on a
    connection that might be a phone hotspot.
    """
    from core import semantic
    if semantic.available():
        print(f"→ shortlisting model already present ({semantic.MODEL_NAME})")
        return
    print(f"→ downloading {semantic.MODEL_NAME} (~30MB, one time)...")
    if semantic.warm_up():
        print("→ ready. Shortlisting now ranks by meaning rather than by "
              "counting shared words.")
    else:
        print("→ could not load it. Scans still work — they fall back to "
              "word overlap, which is worse but free and offline.")


def cmd_set_telegram(args):
    """
    Attach a Telegram chat id to one account.

    Without this there was no way to set the column: seeding leaves it
    NULL, and the scanner refuses to fall back to the environment's chat
    id because that is how one person's roles end up in another person's
    Telegram. So every digest was skipped with a warning, for everyone,
    and nothing in the tool could fix it.

    Get the id by messaging the bot and opening
    api.telegram.org/bot<TOKEN>/getUpdates.
    """
    if "--user" not in args:
        print("usage: run.py set-telegram --user <username> --chat <id>\n"
              "       run.py set-telegram --user <username> --clear")
        return
    username = args[args.index("--user") + 1]
    chat_id = args[args.index("--chat") + 1] if "--chat" in args else None
    if not chat_id and "--clear" not in args:
        print("Pass --chat <id>, or --clear to remove it.")
        return

    conn = db.connect()
    try:
        user = db.get_user_by_name(conn, username)
        if not user:
            print(f"No such user: {username}")
            return
        db.set_telegram_chat_id(conn, user["id"], chat_id)
        print(f"→ {username}: telegram chat id "
              f"{'cleared' if not chat_id else 'set'}")
    finally:
        conn.close()


def cmd_log(args):
    if len(args) < 2:
        print(f"usage: run.py log <url> <status>  {feedback.VALID}")
        return
    note = " ".join(args[2:]) if len(args) > 2 else ""
    feedback.log(args[0], args[1], note)


def cmd_review(args):
    """
    Diagnose a search from what actually happened, and optionally
    re-weight the scorer.

        run.py review --user alice
        run.py review --user alice --apply

    Reads outcomes from the DATABASE — every "I've applied", rejection
    and interview the portal has been recording. That signal existed all
    along and nothing consumed it: this module only ever read the
    single-user JSON file, so the scorer's behaviour was identical in
    week ten and week one.

    `--apply` is deliberately opt-in. Re-weighting silently changes what
    every future board shows, and that should follow reading the
    diagnosis, not precede it.
    """
    if "--user" not in args:
        # The JSON-era path, kept for the single-user CLI.
        profile = intake.load_profile()
        if not profile:
            print("No profile yet. Use --user NAME for a portal account.")
            return
        r = feedback.review(profile)
        if r and "--apply" in args:
            feedback.apply_adjustments(profile, r)
        return

    username = args[args.index("--user") + 1]
    conn = db.connect()
    try:
        user = db.get_user_by_name(conn, username)
        if not user:
            print(f"No such user: {username}")
            return
        profile = db.load_profile(conn, user["id"])
        if not profile:
            print(f"{username} has no profile yet.")
            return

        outcomes = feedback.outcomes_for_user(conn, user["id"])
        result = feedback.review(profile, data=outcomes)
        if not result:
            return

        if "--apply" not in args:
            print("  Re-run with --apply to update the scoring weights.\n")
            return

        # save=False: the JSON path writes data/profile.json, which is
        # not where a portal user's profile lives.
        feedback.apply_adjustments(profile, result, save=False)
        db.save_profile(conn, user["id"], profile)
        print(f"→ weights saved for {username}. The next scan scores "
              f"against them.\n")
    finally:
        conn.close()


def cmd_doctor(args):
    """
    Probe every configured source and say which ones are actually alive.

    This exists because a dead board and a quiet one look identical.
    `greenhouse: phonepe` sat in sources.yml returning HTTP 404 on every
    scan for weeks; the fetcher swallows that so one bad slug cannot take
    the whole tier down, and the scan reported "739 listings" either way.
    The distinction the config needs is:

        404 or error   the slug is wrong, nothing will ever come back
        200 with zero  the board is real, they just are not hiring today

    Only the first is a bug, and only the first is worth shouting about.
    """
    import yaml
    from concurrent.futures import ThreadPoolExecutor

    from core import discover, semantic, sources_free

    # Ask the fetchers to raise instead of swallowing, so a wrong slug
    # can be told apart from a board with nothing open. Only here.
    sources_free.STRICT = True

    src = yaml.safe_load(open("config/sources.yml", encoding="utf-8"))
    ats = src.get("ats_targets") or {}

    print("Career Loop source check")
    print("=" * 62)

    orphans = [k for k in ats if not hasattr(discover, f"from_{k}")]
    if orphans:
        print(f"\n  BROKEN  sources.yml offers {orphans} but no fetcher "
              f"exists for them.\n          Every slug listed there is "
              f"silently doing nothing.")

    pairs = [(k, s) for k, v in ats.items() if k not in orphans
             for s in (v or [])]

    def probe(pair):
        kind, slug = pair
        try:
            return (kind, slug,
                    len(getattr(discover, f"from_{kind}")(slug)), None)
        except Exception as e:                          # noqa: BLE001
            return kind, slug, -1, str(e)[:48]

    print(f"\nATS boards ({len(pairs)} slugs configured)")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = sorted(pool.map(probe, pairs))

    broken = [r for r in results if r[2] < 0]
    quiet = [r for r in results if r[2] == 0]
    for kind, slug, n, err in results:
        mark = "BROKEN " if n < 0 else ("quiet  " if n == 0 else "ok     ")
        detail = err if n < 0 else (f"{n} listings" if n else "no openings")
        print(f"  {mark} {kind:16} {slug:22} {detail}")
    print(f"\n  {len(results) - len(broken) - len(quiet)} live, "
          f"{len(quiet)} quiet, {len(broken)} broken")
    if broken:
        print("  Fix or delete the broken slugs. They cost a request on "
              "every scan\n  and can never return a listing.")

    print("\nKeys and models")
    for name, why in (("ANTHROPIC_API_KEY", "scoring, tailoring, intake"),
                      ("APIFY_TOKEN", "Naukri and LinkedIn"),
                      ("FIRECRAWL_API_KEY", "open-web career pages")):
        print(f"  {'set    ' if os.environ.get(name) else 'MISSING'} "
              f"{name:20} {why}")

    ready = semantic.available()
    print(f"  {'ready  ' if ready else 'MISSING'} {'semantic model':20} "
          f"{'free shortlisting' if ready else 'run: py run.py warm-up'}")
    try:
        import jobspy                                   # noqa: F401
        print(f"  ready   {'jobspy':20} free Indeed and LinkedIn")
    except ImportError:
        print(f"  MISSING {'jobspy':20} pip install python-jobspy")

    if broken or orphans:
        sys.exit(1)


def cmd_check_links(args):
    """
    Verify the links on everyone's board and close the ones proven gone.

    Runs automatically at the end of every scan, 60 at a time. This is
    the manual version for when you want the whole backlog cleared now.
    """
    from core import linkcheck

    conn = db.connect()
    limit = int(args[0]) if args else 500
    before = db.closed_job_count(conn)
    checked, closed = linkcheck.sweep(conn, limit=limit)
    print(f"\n{checked} links checked, {closed} closed "
          f"({before} were already closed, {db.closed_job_count(conn)} now)")
    conn.close()


COMMANDS = {
    "intake": cmd_intake,
    "calibrate": cmd_calibrate,
    "scan": cmd_scan,
    "log": cmd_log,
    "review": cmd_review,
    "seed-users": cmd_seed_users,
    "warm-up": cmd_warm_up,
    "discover-companies": cmd_discover_companies,
    "set-telegram": cmd_set_telegram,
    "publish": cmd_publish,
    "migrate": cmd_migrate,
    "doctor": cmd_doctor,
    "check-links": cmd_check_links,
    "serve": cmd_serve,
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[cmd](sys.argv[2:])
