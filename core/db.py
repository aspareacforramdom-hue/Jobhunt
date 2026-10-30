"""
PHASE 1 — storage.

JSON files died the moment the portal needed "all non-ignored jobs,
score > 7, this week, sorted by company rating".

The shape that matters:

  jobs        SHARED. Scraped once. When two users' searches overlap,
              the listing is stored once, not twice.
  user_jobs   NOT shared. Score, verdict, status, and the reason
              something was ignored are per-user and never cross over.
  companies   SHARED, because it is public data and caching it saves
              real API cost.

ISOLATION RULE: every helper that touches user_jobs, profiles,
applications or runs takes user_id as its FIRST argument and filters on
it. There is no variant that doesn't. tests/test_isolation.py proves it.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core import schema

DB_PATH = os.environ.get("CAREERLOOP_DB", "data/shivani/careerloop.db")

# India has no DST, so a fixed offset is exact and needs no tz database.
IST = timezone(timedelta(hours=5, minutes=30))

STATUSES = ["new", "interested", "ignored", "prepared", "applied",
            "no_response", "rejected", "screening", "interview", "offer"]

# Ignore is an action, not a column — ignored jobs leave the board.
BOARD_COLUMNS = [
    ("new", "New", ["new"]),
    ("interested", "Interested", ["interested"]),
    ("prepared", "Prepared", ["prepared"]),
    ("applied", "Applied", ["applied"]),
    ("outcome", "Outcome",
     ["no_response", "rejected", "screening", "interview", "offer"]),
]
COLUMN_OF = {s: col for col, _, states in BOARD_COLUMNS for s in states}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value):
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _j(value):
    return json.dumps(value if value is not None else None, default=str)


def _unj(value, default=None):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------
# Connection
# ------------------------------------------------------------
def get_conn(path=None):
    """A fresh connection. The boot scan runs on a background thread, so
    connections are never shared between threads."""
    path = path or DB_PATH
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


# Schema and migrations live in core/schema.py — a wall of DDL that
# changes on a different schedule from the queries below, and which
# pushed this file past the project's 500-line ceiling.
MIGRATIONS = schema.MIGRATIONS


def init_db(conn):
    """
    Idempotent. Safe to call on every boot — and it is called far more
    often than that: every HTTP request opens a connection through
    connect(), and the header polls scan status every 15 seconds.

    So the already-migrated path does one read and nothing else. It used
    to run CREATE TABLE IF NOT EXISTS plus a commit on every request.
    Measured on an idle database that is worth about 0.1ms, which is
    nothing — the reason to remove it is that both statements take a
    write lock to discover there is nothing to write, and the moment
    that matters is the one where the boot scan is holding it.
    """
    try:
        row = conn.execute(
            "SELECT MAX(version) v FROM schema_version").fetchone()
        current = row["v"] or 0
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        current = 0

    if current >= len(MIGRATIONS):
        return conn

    # Serialize first-start/upgrade callers and record each schema change
    # in the same transaction as its version. executescript implicitly
    # commits, leaving half-applied ALTERs behind after a crash or error.
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version "
                     "(version INTEGER NOT NULL)")
        current = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
        for i, sql in enumerate(MIGRATIONS, start=1):
            if i <= current:
                continue
            statement = ""
            for char in sql:
                statement += char
                if char == ";" and sqlite3.complete_statement(statement):
                    conn.execute(statement)
                    statement = ""
            if statement.strip():
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (i,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return conn


def connect(path=None):
    conn = get_conn(path)
    try:
        return init_db(conn)
    except Exception:
        conn.close()
        raise


# ------------------------------------------------------------
# Users
# ------------------------------------------------------------
def create_user(conn, username, password_hash, salt,
                telegram_chat_id=None, is_admin=0):
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, salt, "
        "telegram_chat_id, is_admin, created_at) VALUES (?,?,?,?,?,?)",
        (username, password_hash, salt, telegram_chat_id, int(is_admin), now()))
    conn.commit()
    return cur.lastrowid


def get_user_by_name(conn, username):
    return conn.execute("SELECT * FROM users WHERE username = ?",
                        (username,)).fetchone()


def get_user(conn, user_id):
    return conn.execute("SELECT * FROM users WHERE id = ?",
                        (user_id,)).fetchone()


def all_users(conn):
    return conn.execute("SELECT * FROM users ORDER BY id").fetchall()


def set_telegram_chat_id(conn, user_id, chat_id):
    conn.execute("UPDATE users SET telegram_chat_id = ? WHERE id = ?",
                 (chat_id or None, user_id))
    conn.commit()


# ------------------------------------------------------------
# Profiles  (per user)
# ------------------------------------------------------------
def save_profile(conn, user_id, profile):
    conn.execute(
        "INSERT INTO profiles (user_id, target_json, hard_filters_json, "
        "weights_json, dimension_definitions_json, angles_json, "
        "search_terms_json, dropped_json, updated_at, resume_text, "
        "leverage_target_json, track_weighting, inference_note, extra_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "target_json=excluded.target_json, "
        "hard_filters_json=excluded.hard_filters_json, "
        "weights_json=excluded.weights_json, "
        "dimension_definitions_json=excluded.dimension_definitions_json, "
        "angles_json=excluded.angles_json, "
        "search_terms_json=excluded.search_terms_json, "
        "dropped_json=excluded.dropped_json, "
        "updated_at=excluded.updated_at, "
        # COALESCE, so saving a profile without a CV — which every
        # intake-form upload does — never wipes a CV already stored.
        "resume_text=COALESCE(excluded.resume_text, profiles.resume_text), "
        "leverage_target_json=excluded.leverage_target_json, "
        "track_weighting=excluded.track_weighting, "
        "inference_note=excluded.inference_note, extra_json=excluded.extra_json",
        (user_id, _j(profile.get("target", {})),
         _j(profile.get("hard_filters", {})), _j(profile.get("weights", {})),
         _j(profile.get("dimension_definitions", {})),
         _j(profile.get("angles", [])), _j(profile.get("search_terms", [])),
         _j(profile.get("dropped", [])), now(),
         (profile.get("resume_text") or None),
         _j(profile.get("leverage_target", {})),
         profile.get("track_weighting"),
         profile.get("inference_note"), _j({k: profile[k] for k in (
             "candidate", "evidence", "uncertainties", "preferences",
             "search_strategy", "source_documents") if k in profile})))
    conn.commit()
    return profile


def load_profile(conn, user_id):
    r = conn.execute("SELECT * FROM profiles WHERE user_id = ?",
                     (user_id,)).fetchone()
    if not r:
        return None
    return {
        **(_unj(r["extra_json"], {}) or {}),
        "target": _unj(r["target_json"], {}),
        "hard_filters": _unj(r["hard_filters_json"], {}),
        "weights": _unj(r["weights_json"], {}),
        "dimension_definitions": _unj(r["dimension_definitions_json"], {}),
        "angles": _unj(r["angles_json"], []),
        "search_terms": _unj(r["search_terms_json"], []),
        "dropped": _unj(r["dropped_json"], []),
        "resume_text": r["resume_text"],
        "leverage_target": _unj(r["leverage_target_json"], {}),
        "track_weighting": r["track_weighting"],
        "inference_note": r["inference_note"],
        "updated_at": r["updated_at"],
    }


# ------------------------------------------------------------
# Jobs  (shared pool)
# ------------------------------------------------------------
# Params that identify a campaign, never a job. Anything not listed here
# is kept, because job boards routinely put the listing id in the query
# string (Indeed's `jk`, LinkedIn's `currentJobId`) and dropping those
# would merge unrelated roles into one row.
_TRACKING_PARAMS = ("utm_source", "utm_medium", "utm_campaign", "utm_term",
                    "utm_content", "utm_id", "fbclid", "gclid", "msclkid",
                    "mc_cid", "mc_eid", "_ga")


def normalize_url(url):
    """
    One canonical spelling per listing.

    `jobs` is the shared pool and `url` is its identity, so the same role
    reached from two sources — one with a trailing slash, one with an
    email campaign tag — must land on one row. Otherwise both users see
    it twice and it gets scored twice.

    Host and scheme are case-folded because they are case-insensitive;
    the path deliberately is not.
    """
    if not url:
        return url
    url = str(url).strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme or not parts.netloc:
        return url

    query = urlencode([(k, v) for k, v in parse_qsl(parts.query,
                                                    keep_blank_values=True)
                       if k.lower() not in _TRACKING_PARAMS])
    path = parts.path.rstrip("/") or "/"

    # Career pages without per-role links use a reserved fragment as the
    # listing identity. Ordinary navigation/tracking fragments still drop.
    fragment = parts.fragment if parts.fragment.startswith("careerloop-job=") else ""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                       path, query, fragment))


def upsert_job(conn, job):
    """Store a listing once. Returns its id whoever found it."""
    url = normalize_url(job.get("url"))
    if not url:
        return None
    # The follow-up SELECT looks like an obvious N+1 — a scan upserts the
    # whole scrape and this runs 900-odd times — so it was rewritten to
    # use RETURNING, which removes every one of those statements.
    #
    # Measured, that was 40% SLOWER (23.2ms vs 16.6ms for 900 rows, same
    # conflict clause, best of three). RETURNING on an upsert makes
    # SQLite materialise the row; a lookup on a UNIQUE index is close to
    # free. Fewer statements is not the same thing as faster, and the
    # cheap-looking query was the cheap one. Left as it is, deliberately.
    conn.execute(
        "INSERT INTO jobs (url, title, company, location, description, "
        "source, found_at, last_seen_at, needs_enrichment, metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(url) DO UPDATE SET "
        "title=COALESCE(excluded.title, jobs.title), "
        "company=COALESCE(excluded.company, jobs.company), "
        "location=COALESCE(excluded.location, jobs.location), "
        "description=CASE WHEN length(COALESCE(excluded.description,'')) >= "
        "length(COALESCE(jobs.description,'')) THEN excluded.description ELSE jobs.description END, "
        "needs_enrichment=CASE WHEN "
        "max(length(COALESCE(excluded.description,'')), length(COALESCE(jobs.description,''))) >= 350 "
        "THEN 0 ELSE excluded.needs_enrichment END, "
        "metadata_json=json_patch(COALESCE(jobs.metadata_json,'{}'), COALESCE(excluded.metadata_json,'{}'))",
        (url, job.get("title"), job.get("company"), job.get("location"),
         job.get("description"), job.get("source"),
         job.get("found_at") or now(), now(),
         int(bool(job.get("needs_enrichment"))),
         _j({k: job[k] for k in JOB_METADATA if k in job})
         if any(k in job for k in JOB_METADATA) else None))
    row = conn.execute("SELECT id FROM jobs WHERE url = ?", (url,)).fetchone()
    return row["id"] if row else None


JOB_METADATA = ("salary_min_lpa", "salary_max_lpa", "salary_text", "work_mode",
                "verified_at", "verification", "posted_at", "valid_through",
                "applicants", "easy_apply", "source_url", "source_note")


def get_job(conn, job_id):
    return conn.execute("SELECT * FROM jobs WHERE id = ?",
                        (job_id,)).fetchone()


def known_urls(conn):
    return {r["url"] for r in conn.execute("SELECT url FROM jobs")}


# ------------------------------------------------------------
# user_jobs  (per user — never shared)
# ------------------------------------------------------------
def upsert_user_job(conn, user_id, job_id, scored, commit=True):
    """Write one user's opinion of a job. Never touches another user's.

    `commit=False` lets a caller writing a whole board do it in one
    transaction. Each commit is an fsync, and a scan was doing one per
    scored listing — 60 syncs to write 60 rows that are useless
    individually anyway, since a half-written board is not a state
    anybody wants to see.
    """
    conn.execute(
        "INSERT INTO user_jobs (user_id, job_id, score, verdict, angle, "
        "risk, stretch, score_breakdown_json, evidence, track, status, "
        "updated_at, evaluation_json, profile_revision) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,'new',?,?,?) "
        "ON CONFLICT(user_id, job_id) DO UPDATE SET "
        "score=excluded.score, verdict=excluded.verdict, "
        "angle=excluded.angle, risk=excluded.risk, "
        "stretch=excluded.stretch, "
        "score_breakdown_json=excluded.score_breakdown_json, "
        "evidence=excluded.evidence, track=excluded.track, "
        "updated_at=excluded.updated_at, evaluation_json=excluded.evaluation_json, "
        "profile_revision=excluded.profile_revision",
        (user_id, job_id, scored.get("score"), scored.get("verdict"),
         scored.get("angle"), scored.get("risk"),
         int(bool(scored.get("stretch"))),
         _j(scored.get("score_breakdown", {})),
         scored.get("evidence") or None, scored.get("track") or None,
         now(), _j(scored.get("evaluation") or {}), scored.get("profile_revision")))
    if commit:
        conn.commit()


# `j.*` used to be here, which dragged the description column into every
# list view. Descriptions are capped at 4KB and the board renders none of
# them: on an 800-job corpus that was 3.5MB moved to display 15 titles,
# 96% of the payload. They are named explicitly now, and only the detail
# page — which actually shows the text — asks for it.
_JOB_COLS = ("j.id, j.url, j.title, j.company, j.location, j.source, "
             "j.found_at, j.closed_at, j.metadata_json, j.link_checked_at")

_JOIN_TAIL = """
         uj.score, uj.verdict, uj.angle, uj.risk, uj.stretch,
         uj.score_breakdown_json, uj.evidence, uj.track, uj.status,
         uj.ignored_reason,
         uj.ignored_at, uj.updated_at,
         uj.evaluation_json, uj.profile_revision,
         c.rating AS company_rating, c.review_count AS company_review_count,
         c.review_summary_json
    FROM user_jobs uj
    JOIN jobs j ON j.id = uj.job_id
    LEFT JOIN companies c ON LOWER(c.name) = LOWER(j.company)
   WHERE uj.user_id = ?
"""

_JOIN = f"SELECT {_JOB_COLS}, {_JOIN_TAIL}"
_JOIN_FULL = f"SELECT {_JOB_COLS}, j.description, {_JOIN_TAIL}"


def _row(r):
    d = dict(r)
    d.update(_unj(d.pop("metadata_json", None), {}) or {})
    d["evaluation"] = _unj(d.pop("evaluation_json", None), {}) or {}
    d["score_breakdown"] = _unj(d.pop("score_breakdown_json", None), {})
    d["review_summary"] = _unj(d.pop("review_summary_json", None), None)
    d["stretch"] = bool(d.get("stretch"))
    d["closed"] = bool(d.get("closed_at"))
    d["column"] = COLUMN_OF.get(d.get("status"), "new")
    return d


def get_user_job(conn, user_id, job_id):
    """One row, with the description — this backs the detail page."""
    r = conn.execute(_JOIN_FULL + " AND uj.job_id = ?",
                     (user_id, job_id)).fetchone()
    return _row(r) if r else None


def scores_for(conn, user_id, exclude_ignored=True):
    """
    Just the numbers, for the distribution strip.

    This used to go through list_user_jobs, which meant hydrating every
    row — descriptions, verdicts, company joins, JSON parsing — to
    compute a median and ten bucket counts. One column is enough.
    """
    sql = "SELECT score FROM user_jobs WHERE user_id = ? AND score IS NOT NULL"
    if exclude_ignored:
        sql += " AND status != 'ignored'"
    return [r["score"] for r in conn.execute(sql, (user_id,))]


def list_user_jobs(conn, user_id, *, statuses=None, exclude_ignored=True,
                   min_score=None, company=None, source=None, since=None,
                   search=None, order="score", limit=None,
                   include_closed=False, track=None, include_description=False,
                   eligible_only=False):
    sql, params = (_JOIN_FULL if include_description else _JOIN), [user_id]

    if statuses:
        sql += f" AND uj.status IN ({','.join('?' * len(statuses))})"
        params += list(statuses)
    elif exclude_ignored:
        sql += " AND uj.status != 'ignored'"
    if eligible_only:
        sql += " AND (uj.status != 'new' OR COALESCE(json_extract(uj.evaluation_json, '$.eligible'), 1) = 1)"
    if not include_closed:
        # A listing that has come down is noise on a board of things to
        # apply to — 29% of these boards were already HTTP 404 before
        # anything expired them. But once the user has acted on a role
        # it is part of their pipeline and stays visible however dead
        # the link is: you do not want the job you applied to on Monday
        # silently disappearing on Friday.
        sql += " AND (j.closed_at IS NULL OR uj.status != 'new')"
    if min_score is not None:
        sql += " AND uj.score >= ?"
        params.append(min_score)
    if company:
        sql += " AND LOWER(j.company) LIKE ?"
        params.append(f"%{company.lower()}%")
    if source:
        sql += " AND j.source = ?"
        params.append(source)
    if track:
        # The two-track intake is pointless if both land in one list
        # sorted by score. A well-paid lateral move scores LOW against
        # an ambitious target and that is the honest answer — it really
        # is the weaker career step — but it buries exactly the jobs
        # somebody with a salary floor asked to be shown. So they are
        # filtered apart rather than ranked together, and "both" belongs
        # in either list.
        sql += " AND (uj.track = ? OR uj.track = 'both')"
        params.append(track)
    if since:
        sql += " AND j.found_at >= ?"
        params.append(since)
    if search:
        sql += " AND (LOWER(j.title) LIKE ? OR LOWER(j.company) LIKE ?)"
        params += [f"%{search.lower()}%"] * 2

    sql += {"date": " ORDER BY j.found_at DESC, uj.score DESC",
            "company": " ORDER BY c.rating DESC NULLS LAST, uj.score DESC",
            "title": " ORDER BY j.title ASC"}.get(
                order, " ORDER BY uj.score DESC, j.found_at DESC")
    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    return [_row(r) for r in conn.execute(sql, params)]


def count_user_jobs(conn, user_id, *, statuses=None, min_score=None,
                    include_closed=False):
    """
    How many, without fetching any of them.

    The board says "15 of 200" — it needs the 200, not the rows behind it.
    """
    sql = ("SELECT COUNT(*) n FROM user_jobs uj "
           "JOIN jobs j ON j.id = uj.job_id WHERE uj.user_id = ?")
    params = [user_id]
    if statuses:
        sql += f" AND uj.status IN ({','.join('?' * len(statuses))})"
        params += list(statuses)
    else:
        sql += " AND uj.status != 'ignored'"
    if not include_closed:
        # Must match list_user_jobs exactly. A count that disagrees with
        # the rows underneath it is how a board ends up saying
        # "15 of 200" above twelve listings.
        sql += " AND (j.closed_at IS NULL OR uj.status != 'new')"
    if min_score is not None:
        sql += " AND uj.score >= ?"
        params.append(min_score)
    return conn.execute(sql, params).fetchone()["n"]


def set_status(conn, user_id, job_id, status):
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    conn.execute(
        "UPDATE user_jobs SET status=?, ignored_reason=NULL, ignored_at=NULL,"
        " updated_at=? WHERE user_id=? AND job_id=?",
        (status, now(), user_id, job_id))
    tracked = status in ("applied", "no_response", "rejected", "screening", "interview", "offer")
    exists = conn.execute("SELECT 1 FROM user_jobs WHERE user_id=? AND job_id=?",
                          (user_id, job_id)).fetchone()
    if tracked and exists:
        conn.execute("INSERT INTO outcome_events (user_id,job_id,status,happened_at) VALUES (?,?,?,?)",
                     (user_id, job_id, status, now()))
        conn.execute(
            "INSERT INTO applications (user_id,job_id,outcome,applied_at) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id,job_id) DO UPDATE SET outcome=excluded.outcome, "
            "applied_at=COALESCE(applications.applied_at,excluded.applied_at)",
            (user_id, job_id, status, now()))
    conn.commit()


def ignore_job(conn, user_id, job_id, reason=None):
    conn.execute(
        "UPDATE user_jobs SET status='ignored', ignored_reason=?, "
        "ignored_at=?, updated_at=? WHERE user_id=? AND job_id=?",
        (reason or None, now(), now(), user_id, job_id))
    conn.commit()


def restore_job(conn, user_id, job_id):
    """Restores to 'new' but keeps the reason — the ignore list is a
    record of judgement, not a bin."""
    conn.execute(
        "UPDATE user_jobs SET status='new', ignored_at=NULL, updated_at=? "
        "WHERE user_id=? AND job_id=? AND status='ignored'",
        (now(), user_id, job_id))
    conn.commit()


def status_counts(conn, user_id):
    rows = conn.execute(
        "SELECT status, COUNT(*) n FROM user_jobs WHERE user_id=? "
        "GROUP BY status", (user_id,))
    return {r["status"]: r["n"] for r in rows}


# ------------------------------------------------------------
# Applications  (per user)
# ------------------------------------------------------------
def save_application(conn, user_id, job_id, tailored=None, outreach=None):
    conn.execute(
        "INSERT INTO applications (user_id, job_id, tailored_json, "
        "outreach_json, prepared_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(user_id, job_id) DO UPDATE SET "
        "tailored_json=COALESCE(excluded.tailored_json, "
        "applications.tailored_json), "
        "outreach_json=COALESCE(excluded.outreach_json, "
        "applications.outreach_json), prepared_at=excluded.prepared_at",
        (user_id, job_id, _j(tailored) if tailored else None,
         _j(outreach) if outreach else None, now()))
    conn.commit()


def get_application(conn, user_id, job_id):
    r = conn.execute("SELECT * FROM applications WHERE user_id=? AND job_id=?",
                     (user_id, job_id)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["tailored"] = _unj(d.pop("tailored_json", None), None)
    d["outreach"] = _unj(d.pop("outreach_json", None), None)
    return d


def list_applications(conn, user_id):
    rows = conn.execute(
        "SELECT a.*, j.title, j.company, j.location, j.url, uj.score, "
        "uj.status FROM applications a "
        "JOIN jobs j ON j.id = a.job_id "
        "LEFT JOIN user_jobs uj ON uj.job_id = a.job_id "
        "AND uj.user_id = a.user_id "
        "WHERE a.user_id = ? ORDER BY a.prepared_at DESC", (user_id,))
    out = []
    for r in rows:
        d = dict(r)
        d["tailored"] = _unj(d.pop("tailored_json", None), None)
        d["outreach"] = _unj(d.pop("outreach_json", None), None)
        out.append(d)
    return out


def record_outcome(conn, user_id, job_id, outcome, note=None):
    conn.execute(
        "INSERT INTO applications (user_id, job_id, outcome, outcome_note, "
        "applied_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(user_id, job_id) DO UPDATE SET outcome=excluded.outcome, "
        "outcome_note=excluded.outcome_note, "
        "applied_at=COALESCE(applications.applied_at, excluded.applied_at)",
        (user_id, job_id, outcome, note, now()))
    conn.commit()


# ------------------------------------------------------------
# Companies  (shared cache — public data only)
# ------------------------------------------------------------
def get_company(conn, name):
    if not name:
        return None
    r = conn.execute("SELECT * FROM companies WHERE LOWER(name) = LOWER(?)",
                     (name,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["rating_breakdown"] = _unj(d.pop("rating_breakdown_json", None), {})
    d["review_summary"] = _unj(d.pop("review_summary_json", None), None)
    return d


def save_company(conn, name, rating=None, review_count=None,
                 rating_breakdown=None, review_summary=None,
                 sector_median_rating=None):
    conn.execute(
        "INSERT INTO companies (name, rating, review_count, "
        "rating_breakdown_json, review_summary_json, sector_median_rating, "
        "fetched_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET rating=excluded.rating, "
        "review_count=excluded.review_count, "
        "rating_breakdown_json=excluded.rating_breakdown_json, "
        "review_summary_json=excluded.review_summary_json, "
        "sector_median_rating=excluded.sector_median_rating, "
        "fetched_at=excluded.fetched_at",
        (name, rating, review_count, _j(rating_breakdown or {}),
         _j(review_summary) if review_summary else None,
         sector_median_rating, now()))
    conn.commit()


def company_is_fresh(conn, name, max_age_days=30):
    c = get_company(conn, name)
    ts = parse_ts(c["fetched_at"]) if c else None
    if not ts:
        return False
    return (datetime.now(timezone.utc) - ts).days < max_age_days


# ------------------------------------------------------------
# Runs  (per user)
# ------------------------------------------------------------
def start_run(conn, user_id, kind):
    cur = conn.execute(
        "INSERT INTO runs (user_id, kind, started_at, status) "
        "VALUES (?,?,?,'running')", (user_id, kind, now()))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id, status, jobs_found=0, error_text=None):
    conn.execute(
        "UPDATE runs SET finished_at=?, status=?, jobs_found=?, error_text=? "
        "WHERE id=?", (now(), status, jobs_found, error_text, run_id))
    conn.commit()


def last_successful_run(conn, user_id=None):
    """Across all users when user_id is None — the boot scan is a single
    shared scrape, so 'when did we last scan' is a global question."""
    sql = "SELECT * FROM runs WHERE status='ok'"
    params = []
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    sql += " ORDER BY finished_at DESC LIMIT 1"
    return conn.execute(sql, params).fetchone()


# ------------------------------------------------------------
# Settings  (global, not per user)
# ------------------------------------------------------------
def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?",
                       (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)))
    conn.commit()


# ------------------------------------------------------------
# Job boards  (discovered by probing — shared, like `jobs`)
# ------------------------------------------------------------
def save_job_board(conn, platform, slug, company_name=None, jobs_seen=0,
                   found_for=None, confidence="exact"):
    """
    Record a board that answered with live roles.

    `discovered_at` is preserved on re-discovery so the record keeps
    saying when this board first appeared, while `jobs_seen` and
    `last_seen_at` move — a board that has quietly gone to zero is worth
    being able to notice.
    """
    conn.execute(
        "INSERT INTO job_boards (platform, slug, company_name, jobs_seen, "
        "found_for, enabled, discovered_at, last_seen_at, confidence) "
        "VALUES (?,?,?,?,?,1,?,?,?) "
        "ON CONFLICT(platform, slug) DO UPDATE SET "
        "company_name=COALESCE(excluded.company_name, "
        "job_boards.company_name), "
        "jobs_seen=excluded.jobs_seen, last_seen_at=excluded.last_seen_at",
        (platform, slug, company_name, int(jobs_seen or 0), found_for,
         now(), now(), confidence))
    conn.commit()


def list_job_boards(conn, platform=None, enabled_only=True):
    sql = "SELECT * FROM job_boards WHERE 1=1"
    params = []
    if platform:
        sql += " AND platform = ?"
        params.append(platform)
    if enabled_only:
        sql += " AND enabled = 1"
    sql += " ORDER BY jobs_seen DESC, slug"
    return conn.execute(sql, params).fetchall()


def set_job_board_enabled(conn, platform, slug, enabled):
    conn.execute(
        "UPDATE job_boards SET enabled=? WHERE platform=? AND slug=?",
        (1 if enabled else 0, platform, slug))
    conn.commit()


def recent_runs(conn, user_id, limit=10):
    return conn.execute(
        "SELECT * FROM runs WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit)).fetchall()


# ------------------------------------------------------------
# Closing listings that are gone
# ------------------------------------------------------------
# Measured on the three live boards: 20 of 70 links were already HTTP
# 404 and the median card was 31 days old. A board where one link in
# three is dead is worse than no board, because the user only finds out
# after deciding to apply.
#
# The first attempt at fixing that inferred closure for free. A
# Greenhouse or Lever board answers with the company's complete current
# list, so anything missing had surely been taken down — no probing, no
# extra request. It closed 278 of 963 listings, and probing 60 of them
# found 42 still answering HTTP 200 with a working apply button. A 70%
# false positive rate, quietly deleting real opportunities.
#
# The cause was not pagination; the Lever API returns the same 19 rows
# for mindtickle at any limit. Employers can UNLIST a posting while
# leaving the direct link live and applicable, and a board read cannot
# see the difference.
#
# So absence is a suspicion, never a verdict. It decides what the link
# checker asks about first; only an HTTP answer sets closed_at.
COMPLETE_LIST_SOURCES = frozenset(
    {"greenhouse", "lever", "ashby", "smartrecruiters", "workable"})


def mark_seen(conn, urls, when=None):
    """Stamp last_seen_at on every listing this scan saw.

    Every RAW row, not just the new ones. The scanner drops already
    known listings before they reach upsert_job — which is precisely
    why "still open" could not be inferred before this existed.

    A listing seen again is not missing, so the suspicion is cleared.
    """
    stamp = when or now()
    rows = [(stamp, normalize_url(u)) for u in urls if normalize_url(u)]
    if not rows:
        return 0
    conn.executemany(
        "UPDATE jobs SET last_seen_at = ?, missing_since = NULL "
        "WHERE url = ?", rows)
    return len(rows)


def mark_missing(conn, source, company, when=None):
    """Flag this company's listings that today's full board omitted.

    Sets missing_since, NOT closed_at. See the note above: 70% of the
    listings this identified were still live and applicable. All it
    earns them is a place at the front of the link checker's queue.

    Caller must only call this for a fetch that succeeded and returned
    at least one row — an empty answer is what a 404, a timeout and a
    rename all look like. scheduler._flag_missing enforces that.
    """
    if source not in COMPLETE_LIST_SOURCES:
        raise ValueError(
            f"{source} does not return a complete list; absence from it "
            f"means nothing at all. Only {sorted(COMPLETE_LIST_SOURCES)} "
            f"can even raise a suspicion this way.")
    stamp = when or now()
    cur = conn.execute(
        "UPDATE jobs SET missing_since = ? "
        "WHERE closed_at IS NULL AND missing_since IS NULL "
        "AND source = ? AND LOWER(company) = ? "
        "AND (last_seen_at IS NULL OR last_seen_at < ?)",
        (stamp, source, (company or "").lower(), stamp))
    return cur.rowcount


def close_urls(conn, urls, when=None):
    """Close listings the link checker PROVED are gone — 404, 410, or a
    page that says so. This is the only thing that ever sets closed_at.
    """
    stamp = when or now()
    rows = [(stamp, normalize_url(u)) for u in urls if normalize_url(u)]
    if not rows:
        return 0
    before = closed_job_count(conn)
    conn.executemany(
        "UPDATE jobs SET closed_at = ? WHERE url = ? AND closed_at IS NULL",
        rows)
    return closed_job_count(conn) - before


def open_job_count(conn):
    return conn.execute(
        "SELECT COUNT(*) n FROM jobs WHERE closed_at IS NULL").fetchone()["n"]


def closed_job_count(conn):
    return conn.execute(
        "SELECT COUNT(*) n FROM jobs WHERE closed_at IS NOT NULL"
    ).fetchone()["n"]


def suspect_job_count(conn):
    return conn.execute(
        "SELECT COUNT(*) n FROM jobs "
        "WHERE missing_since IS NOT NULL AND closed_at IS NULL").fetchone()["n"]
