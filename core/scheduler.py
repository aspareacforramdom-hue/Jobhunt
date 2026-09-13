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

import json
import os
import threading
import traceback
from datetime import datetime, timedelta

import yaml

from core import (db, discover, filter as flt, firecrawl, linkcheck,
                  notify, prefilter, reviews, score)

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


ROTATION_KEY = "paid_search_term_offset"


def _rotate_terms(conn, terms, per_scan):
    """
    The next slice of search terms for the tier that charges per search.

    Three people between them have eighteen keywords and one scan can
    afford about six. Always taking the first six means the other twelve
    are never searched — one person's job hunt silently funded by
    everyone else's alphabetical luck. So the window walks forward and
    wraps, and the position survives a restart.

    Returns every term when there is no cap, which is what the free
    tiers get.
    """
    if not terms:
        return []
    try:
        per_scan = int(per_scan)
    except (TypeError, ValueError):
        per_scan = 0
    if per_scan <= 0 or per_scan >= len(terms):
        return list(terms)

    try:
        offset = int(db.get_setting(conn, ROTATION_KEY, 0) or 0) % len(terms)
    except (ValueError, TypeError):
        offset = 0

    doubled = list(terms) + list(terms)
    slice_ = doubled[offset:offset + per_scan]
    db.set_setting(conn, ROTATION_KEY, (offset + per_scan) % len(terms))
    return slice_


def _merge_discovered_boards(conn, sources):
    """
    Fold probed boards into the hand-written ones from sources.yml.

    Config wins on conflict in the sense that a slug written by hand is
    never dropped — the two lists are unioned, not replaced. Someone who
    typed a company in still gets it even if probing never surfaced it,
    and discovery can only ever add coverage.
    """
    ats = sources.setdefault("ats_targets", {})
    added = 0
    for row in db.list_job_boards(conn):
        current = ats.setdefault(row["platform"], []) or []
        if row["slug"] not in current:
            current.append(row["slug"])
            ats[row["platform"]] = current
            added += 1
    if added:
        print(f"→ {added} discovered board(s) added to this scan")
    return added


def _seeded_terms(conn, key=None):
    """Keywords that have already had their one deep first pass."""
    raw = db.get_setting(conn, key or discover.BACKFILL_KEY)
    try:
        seeded = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return seeded if isinstance(seeded, list) else []


def _remember_seeded(conn, terms, key=None):
    """
    Add newly seeded keywords to the permanent record.

    Kept in the database rather than in memory because the whole value
    of the deep pass is that it happens ONCE. A restart that forgot this
    would re-buy a month of history for every keyword, which is the
    single most expensive thing this tool can do.
    """
    if not terms:
        return
    key = key or discover.BACKFILL_KEY
    merged = sorted(set(_seeded_terms(conn, key)) | set(terms))
    db.set_setting(conn, key, json.dumps(merged))


# How many board links to verify per scan. At 8 workers a link
# costs well under a second, so 60 adds a few seconds to a scan
# that already takes minutes, and three scans clear a 180-link
# backlog.
LINKCHECK_PER_SCAN = 60


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
    def decide(self, conn, now=None):
        """
        Is a catch-up scan due? Returns {'due': bool, 'reason': str}.

        `now` is a seam for the tests and nothing else. The state this
        answers about — past the debounce, but before any slot — only
        exists at certain hours of the real day, so a test pinned to the
        wall clock had to skip itself for roughly a third of the day,
        including the hours around the slots where the logic matters
        most. Production never passes it.
        """
        last = db.last_successful_run(conn)
        now = now or datetime.now(db.IST)

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

        # `running` is already True at this point, so a thread that never
        # starts — the process is out of threads, or the interpreter is
        # shutting down — would wedge the scanner as permanently busy,
        # with the only cure being a restart. Hand the flag back.
        try:
            self._thread = threading.Thread(target=self._run, args=(kind,),
                                            daemon=True)
            self._thread.start()
        except RuntimeError as e:
            self._set(running=False, phase="failed", error=str(e))
            return False, f"Could not start a scan thread: {e}"
        return True, "Scan started"

    def _run(self, kind):
        try:
            conn = db.connect(self.db_path)
        except Exception as e:                       # noqa: BLE001
            # Opening the database is outside the try below, so a locked
            # or unreadable file used to escape into the thread and leave
            # `running` set forever — the same wedge as above.
            traceback.print_exc()
            self._set(error=str(e), phase="failed", running=False)
            return
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

        with open(self.sources_path, encoding="utf-8") as f:
            sources = yaml.safe_load(f) or {}
        self.sources = sources
        _merge_discovered_boards(conn, sources)
        all_terms = sorted(terms)
        sources.setdefault("jobspy", {})["search_terms"] = all_terms
        sources["jobspy"]["locations"] = sorted(locations) or \
            sources["jobspy"].get("default_locations", [])

        # The free tiers search on everything; the PAID tier gets a
        # rotating slice, because three people between them have far more
        # keywords than one scan can afford. Taking the first N every
        # time would mean the tail is never searched at all.
        paid = _rotate_terms(conn, all_terms,
                             sources.get("apify", {}).get("max_terms_per_scan"))
        apify_cfg = sources.setdefault("apify", {})
        apify_cfg["search_terms"] = paid
        # Which keywords have already had their one deep seeding pass.
        # Remembered in the database, not in this process, so a restart
        # does not re-buy a month of history for every keyword.
        apify_cfg["backfilled_terms"] = _seeded_terms(conn)

        # LinkedIn runs once per scan with every keyword, so its slice is
        # the whole rotation, not a share of it.
        li_cfg = sources.setdefault("linkedin", {})
        li_cfg["search_terms"] = paid
        li_cfg["backfilled_terms"] = _seeded_terms(
            conn, discover.LINKEDIN_BACKFILL_KEY)

        self._set(phase="searching")
        raw, notes = discover.discover_all(sources, dry=self.dry)
        _remember_seeded(conn, apify_cfg.get("_seeded_terms"))
        _remember_seeded(conn, li_cfg.get("_seeded_terms"),
                         discover.LINKEDIN_BACKFILL_KEY)
        self._set(found=len(raw), notes=notes, phase="filtering")

        # Every raw row, including the ones dedup is about to drop:
        # "we saw this again today" is the whole basis for knowing what
        # is still open, and dedup throws those rows away.
        stamp = db.now()
        db.mark_seen(conn, [j.get("url") for j in raw], when=stamp)
        suspect = self._flag_missing(conn, raw, stamp)
        if suspect:
            notes.append(f"{suspect} listings no longer on their board "
                         f"— queued for checking")

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

        # The free closure above only covers boards that answer in full.
        # Sweep a slice of what is left, oldest first, so a backlog
        # drains over several scans instead of hammering anyone once.
        try:
            checked, gone = linkcheck.sweep(conn, limit=LINKCHECK_PER_SCAN)
            if gone:
                notes.append(f"{gone} more closed after checking "
                             f"{checked} links")
        except Exception as e:                          # noqa: BLE001
            # Never let housekeeping lose a scan that already succeeded.
            print(f"  [linkcheck] skipped: {e}")

        self._set(phase="idle")

    @staticmethod
    def _flag_missing(conn, raw, stamp):
        """
        Flag listings that have vanished from a board we just read in
        full, so the link checker asks about them first.

        This used to close them outright, on the reasoning that a
        Greenhouse or Lever board answers with the company's COMPLETE
        current list. Measured, that was wrong in the way that matters:
        it closed 278 of 963 listings and 42 of 60 probed were still
        answering HTTP 200 with a working apply button. Employers unlist
        postings while leaving the link live, and a board read cannot
        tell that apart from a deletion.

        So this raises a suspicion and nothing more. Only linkcheck,
        having actually asked the page, sets closed_at.

        The safety rule still holds: a company is only judged on a fetch
        that returned rows. An empty answer is what a 404, a timeout and
        a rename all look like, and flagging a whole board on one would
        send the checker after hundreds of live listings.

        Keyword sources — Indeed, LinkedIn, Naukri, Instahyre — return a
        slice of a search, never a company's full list, so absence there
        carries no information at all.
        """
        boards = {}
        for job in raw:
            src = job.get("source")
            company = (job.get("company") or "").strip().lower()
            if src in db.COMPLETE_LIST_SOURCES and company:
                boards[(src, company)] = boards.get((src, company), 0) + 1

        flagged = 0
        for (src, company), seen in boards.items():
            if seen:                # belt and braces; the key only exists
                flagged += db.mark_missing(conn, src, company, when=stamp)
        if flagged:
            conn.commit()
            print(f"  [expiry] {flagged} listings missing from "
                  f"{len(boards)} boards — queued for checking")
        return flagged

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
            # Each user scores a COPY. `fresh` is the shared scrape, and
            # scoring writes its verdict onto the dicts it is handed — so
            # passing the originals meant user B's listings arrived
            # carrying user A's score and angle, and any listing B's
            # batch failed to score kept A's opinion of it.
            candidates = prefilter.prefilter([dict(j) for j in fresh], profile)

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
                # Recorded as `dry`, NOT as `ok`. last_successful_run
                # drives the 3-hour cooldown, so booking a dry run as a
                # success meant one `serve --dry` — the command you run
                # precisely because you do not want to spend anything —
                # suppressed real scanning for the next three hours, and
                # the header cheerfully reported it as the last scan.
                db.finish_run(conn, run_id, "dry", len(candidates))
                return

            scored = score.score_jobs(candidates, profile)

            # Candidates in, nothing out, means every batch failed —
            # almost always a missing or rejected ANTHROPIC_API_KEY.
            # score_jobs catches per batch so one bad batch cannot lose
            # the rest, and the cost of that was a total failure looking
            # identical to a quiet market: the run was booked "ok, 0
            # found", the header went green, and the 3-hour cooldown
            # started. Say what happened instead.
            if candidates and not scored:
                raise RuntimeError(
                    f"scored 0 of {len(candidates)} candidates — every LLM "
                    f"call failed. Check ANTHROPIC_API_KEY.")

            for j in scored:
                jid = job_ids.get(j["url"])
                if jid:
                    db.upsert_user_job(conn, user_id, jid, j)
                reviews.enrich_from_listing(conn, j)

            db.finish_run(conn, run_id, "ok", len(scored))

            # OFF during scans by default, and this is the single
            # biggest saving available.
            #
            # Measured, it was two paid actor runs per company for the
            # top ten companies PER USER — 12 companies and $7.14 on one
            # scan, for review summaries of employers nobody had opened
            # yet. The detail page already has a "Look up reviews"
            # button, so the same money now buys reviews only for the
            # companies someone actually reads about.
            if (self.sources.get("reviews") or {}).get("enrich_during_scan"):
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
