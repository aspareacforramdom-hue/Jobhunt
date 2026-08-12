"""
PHASE 6 — boot-triggered scanning.

The portal runs on a laptop and will not be running at 07:00. Cron would
fire into a closed lid. So the trigger is startup, not the clock:

  scheduled slots are TARGETS, not triggers.
  on boot, ask "has a slot passed since the last successful scan?"
  if yes, run ONE catch-up scan. if no, do nothing.

Boot at 23:00 having last scanned yesterday morning means both slots
were missed — that is still one scan, not two. Missed slots never queue.

Debounce, all three enforced:
  - at least 3 hours between automatic scans, regardless of missed slots,
    so six debug restarts produce at most one scan
  - a running scan blocks a second from starting
  - --no-scan skips the boot scan entirely

Isolation: the scrape is shared and runs once; scoring is per user and
each user is wrapped separately. If user B's scoring throws, A and C
still finish and B gets an error row in `runs`.
"""

import os
import threading
import traceback
from datetime import datetime, timedelta

import yaml

from core import (db, discover, filter as flt, firecrawl, notify, prefilter,
                  reviews, score)

SLOTS = [(7, 0), (18, 0)]          # Asia/Kolkata
MIN_GAP_HOURS = 3
LOOKBACK_DAYS = 14                 # how far back slot-hunting bothers to look

# The address the OTHER two users type into their browser, which is not
# the one this process binds to. Hardcoding localhost sends everyone a
# link to their own machine. Set this to the LAN or tunnel address
# whenever the portal is reachable from anywhere but this laptop.
PORTAL_URL = os.environ.get("CAREERLOOP_PORTAL_URL", "http://127.0.0.1:8000")


def slots_between(start, end, lookback_days=LOOKBACK_DAYS):
    """
    Every scheduled slot that fell in (start, end], in IST.

    Bounded on purpose. A laptop left closed for a year would otherwise
    build 730 datetimes and — worse — `decide()` renders every one of
    them into the reason string it shows the user. Since missed slots
    never queue and the answer is always "run exactly one scan", walking
    further back than a couple of weeks buys nothing.
    """
    if not start:
        return [end]
    if end <= start:
        return []

    start = start.astimezone(db.IST)
    end = end.astimezone(db.IST)
    floor = max(start, end - timedelta(days=lookback_days))

    out, day = [], floor.date()
    last_day = end.date()
    while day <= last_day:
        for hh, mm in SLOTS:
            moment = datetime(day.year, day.month, day.day, hh, mm,
                              tzinfo=db.IST)
            if floor < moment <= end:
                out.append(moment)
        day += timedelta(days=1)
    return out


class Scanner:
    """Owns the decision, the lock, and the live status the header polls."""

    def __init__(self, db_path=None, dry=False, sources_path="config/sources.yml"):
        self.db_path = db_path
        self.dry = dry
        self.sources_path = sources_path
        # Populated by _scan; kept on the instance so the per-user stage
        # can reach source config without threading it through.
        self.sources = {}
        self._lock = threading.Lock()
        self._thread = None
        self._state = {"running": False, "phase": "idle", "found": 0,
                       "kind": None, "notes": [], "error": None}

    # ------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------
    def decide(self, conn):
        """Is a catch-up scan due? Returns {'due': bool, 'reason': str}."""
        last = db.last_successful_run(conn)
        now = datetime.now(db.IST)

        if not last:
            return {"due": True, "reason": "no successful scan on record"}

        finished = db.parse_ts(last["finished_at"])
        if not finished:
            return {"due": True, "reason": "last scan has no finish time"}

        gap = now - finished.astimezone(db.IST)
        if gap < timedelta(hours=MIN_GAP_HOURS):
            mins = int(gap.total_seconds() // 60)
            return {"due": False,
                    "reason": f"last scan {mins}m ago, minimum gap is "
                              f"{MIN_GAP_HOURS}h"}

        missed = slots_between(finished.astimezone(db.IST), now)
        if not missed:
            return {"due": False,
                    "reason": f"no slot has passed since the last scan "
                              f"({int(gap.total_seconds() // 3600)}h ago)"}

        # Only the most recent few, and never the whole list — this string
        # is printed at boot and rendered in the UI.
        shown = ", ".join(m.strftime("%d %b %H:%M") for m in missed[-3:])
        more = f" and {len(missed) - 3} earlier" if len(missed) > 3 else ""
        return {"due": True,
                "reason": f"{len(missed)} slot(s) missed ({shown}{more}) — "
                          f"running one catch-up scan"}

    def cooldown_remaining(self, conn):
        """Seconds until an automatic scan would be allowed again."""
        last = db.last_successful_run(conn)
        finished = db.parse_ts(last["finished_at"]) if last else None
        if not finished:
            return 0
        left = timedelta(hours=MIN_GAP_HOURS) - (
            datetime.now(db.timezone.utc) - finished)
        return max(0, int(left.total_seconds()))

    # ------------------------------------------------------------
    # Status  (polled by the header)
    # ------------------------------------------------------------
    def status(self):
        with self._lock:
            state = dict(self._state)

        conn = db.connect(self.db_path)
        try:
            last = db.last_successful_run(conn)
            state["last_finished"] = last["finished_at"] if last else None
            state["last_found"] = last["jobs_found"] if last else 0
            state["cooldown"] = self.cooldown_remaining(conn)
        finally:
            conn.close()
        return state

    def _set(self, **kw):
        with self._lock:
            self._state.update(kw)

    # ------------------------------------------------------------
    # Running
    # ------------------------------------------------------------
    def start(self, kind="manual", override=False):
        """Returns (started, message). Never raises into a request."""
        with self._lock:
            if self._state["running"]:
                return False, "A scan is already running"

            if not override:
                conn = db.connect(self.db_path)
                try:
                    left = self.cooldown_remaining(conn)
                finally:
                    conn.close()
                if left:
                    return False, f"Cooling down, {left // 60}m left"

            self._state.update({"running": True, "phase": "starting",
                                "found": 0, "kind": kind, "error": None,
                                "notes": []})

        self._thread = threading.Thread(target=self._run, args=(kind,),
                                        daemon=True)
        self._thread.start()
        return True, "Scan started"

    def _run(self, kind):
        conn = db.connect(self.db_path)
        try:
            self._scan(conn, kind)
        except Exception as e:
            traceback.print_exc()
            self._set(error=str(e), phase="failed")
        finally:
            conn.close()
            self._set(running=False)

    # ------------------------------------------------------------
    def _scan(self, conn, kind):
        users = [u for u in db.all_users(conn)
                 if db.load_profile(conn, u["id"])]
        if not users:
            self._set(phase="no profiles yet")
            return

        profiles = {u["id"]: db.load_profile(conn, u["id"]) for u in users}

        # The scrape is shared: the union of everyone's search terms, run
        # once. Two users chasing overlapping roles cost one scrape, not
        # two, and the listing is stored once in `jobs`.
        terms, locations = set(), set()
        for p in profiles.values():
            terms.update(p.get("search_terms") or [])
            locations.update(p.get("hard_filters", {}).get("locations") or [])

        with open(self.sources_path) as f:
            sources = yaml.safe_load(f) or {}
        self.sources = sources
        sources.setdefault("jobspy", {})["search_terms"] = sorted(terms)
        sources["jobspy"]["locations"] = sorted(locations) or \
            sources["jobspy"].get("default_locations", [])

        self._set(phase="searching")
        raw, notes = discover.discover_all(sources, dry=self.dry)
        self._set(found=len(raw), notes=notes, phase="filtering")

        fresh = self._dedup(conn, raw)
        job_ids = {}
        for j in fresh:
            jid = db.upsert_job(conn, j)
            if jid:
                job_ids[j["url"]] = jid
        conn.commit()
        self._set(found=len(fresh), phase="scoring")

        for user in users:
            self._score_for_user(conn, user, profiles[user["id"]],
                                 fresh, job_ids, kind)

        self._set(phase="idle")

    def _dedup(self, conn, raw):
        """
        Dedup against the shared pool, not a per-user seen list.

        Compares canonical URLs, since that is what the pool stores — a
        raw URL carrying an email campaign tag would otherwise look new
        on every single scan.
        """
        known = db.known_urls(conn)
        unseen = [j for j in raw if j.get("url")
                  and db.normalize_url(j["url"]) not in known]
        fresh, _ = flt.filter_jobs(unseen, {}, seen=set())
        return fresh

    def _score_for_user(self, conn, user, profile, fresh, job_ids, kind):
        """
        One user's scoring, wrapped.

        A failure here must not touch the other two — they get their
        boards, this user gets an error row they can see in the UI.
        """
        user_id = user["id"]
        run_id = db.start_run(conn, user_id, kind)
        try:
            candidates = prefilter.prefilter(list(fresh), profile)

            # Never pay to score something this user already has an
            # opinion on.
            already = {r["id"] for r in conn.execute(
                "SELECT job_id id FROM user_jobs WHERE user_id=?", (user_id,))}
            candidates = [c for c in candidates
                          if job_ids.get(c["url"]) not in already]

            # Firecrawl search returns a snippet, not a description, and
            # the scorer cannot judge a role from a snippet. Buy the full
            # text HERE — after the prefilter and the already-seen cut —
            # so credits are only spent on listings that will actually be
            # scored. Doing it during discovery would cost several times
            # more for pages thrown away seconds later.
            firecrawl.enrich_descriptions(
                candidates, self.sources.get("firecrawl", {}), dry=self.dry)
            for c in candidates:
                if c.get("description"):
                    db.upsert_job(conn, c)
            conn.commit()

            if self.dry:
                print(f"  [dry] {user['username']}: {len(candidates)} "
                      f"would be scored, 0 LLM calls made")
                db.finish_run(conn, run_id, "ok", len(candidates))
                return

            scored = score.score_jobs(candidates, profile)
            for j in scored:
                jid = job_ids.get(j["url"])
                if jid:
                    db.upsert_user_job(conn, user_id, jid, j)
                reviews.enrich_from_listing(conn, j)

            db.finish_run(conn, run_id, "ok", len(scored))

            # Best-effort, bounded, and shared across users via the
            # companies cache. Never allowed to fail the run.
            try:
                self._set(phase="fetching company reviews")
                reviews.enrich_top_companies(conn, scored, dry=self.dry)
            except Exception as e:
                print(f"  [reviews] enrichment skipped: {e}")

            notify.send_scan_summary(user, scored, PORTAL_URL, dry=self.dry)

        except Exception as e:
            traceback.print_exc()
            db.finish_run(conn, run_id, "error", 0, str(e))
            print(f"  [scan] {user['username']} failed, other users "
                  f"unaffected: {e}")
