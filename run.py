#!/usr/bin/env python3
"""
CAREER LOOP  --  9-stage job intelligence machine (India-first)

    python run.py serve                the portal (three users, local)
    python run.py seed-users           create the three accounts

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
    with open(intake.PROFILE_PATH, "w") as f:
        json.dump(profile, f, indent=2)
    print(f"→ {len(profile['search_terms'])} search terms saved to your profile")


def cmd_scan(args):
    dry = "--dry" in args

    profile = intake.load_profile()
    if not profile:
        print("Run `python run.py intake --cv yourcv.txt` first.")
        return

    with open("config/sources.yml") as f:
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
    with open("data/latest.json", "w") as f:
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
    user = db.get_user_by_name(conn, username)
    if not user:
        print(f"No such user: {username}. Run `run.py seed-users` first.")
        return
    legacy.migrate_json(conn, user["id"])
    conn.close()


def cmd_log(args):
    if len(args) < 2:
        print(f"usage: run.py log <url> <status>  {feedback.VALID}")
        return
    note = " ".join(args[2:]) if len(args) > 2 else ""
    feedback.log(args[0], args[1], note)


def cmd_review(args):
    profile = intake.load_profile()
    if not profile:
        print("No profile yet.")
        return
    r = feedback.review(profile)
    if r and "--apply" in args:
        feedback.apply_adjustments(profile, r)


COMMANDS = {
    "intake": cmd_intake,
    "calibrate": cmd_calibrate,
    "scan": cmd_scan,
    "log": cmd_log,
    "review": cmd_review,
    "seed-users": cmd_seed_users,
    "publish": cmd_publish,
    "migrate": cmd_migrate,
    "serve": cmd_serve,
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[cmd](sys.argv[2:])
