"""
Schema and migrations, kept apart from the query layer.

core/db.py was 564 lines, over this project's own 500-line ceiling, and
roughly a third of it was a wall of DDL that changes on a completely
different schedule from the queries. Nothing but db.py imports this, so
splitting it moved no call sites.

Migrations are append-only and run in order. A migration that has already
been applied is identified by its INDEX in this list, so entries are never
reordered or removed — only added.
"""

# Schema + migrations
# ------------------------------------------------------------
MIGRATIONS = [
    # 1 — initial schema
    """
    CREATE TABLE users (
      id               INTEGER PRIMARY KEY AUTOINCREMENT,
      username         TEXT UNIQUE NOT NULL,
      password_hash    TEXT NOT NULL,
      salt             TEXT NOT NULL,
      telegram_chat_id TEXT,
      is_admin         INTEGER NOT NULL DEFAULT 0,
      created_at       TEXT NOT NULL
    );

    CREATE TABLE profiles (
      user_id                    INTEGER PRIMARY KEY
                                 REFERENCES users(id) ON DELETE CASCADE,
      target_json                TEXT,
      hard_filters_json          TEXT,
      weights_json               TEXT,
      dimension_definitions_json TEXT,
      angles_json                TEXT,
      search_terms_json          TEXT,
      dropped_json               TEXT,
      updated_at                 TEXT
    );

    CREATE TABLE jobs (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      url         TEXT UNIQUE NOT NULL,
      title       TEXT,
      company     TEXT,
      location    TEXT,
      description TEXT,
      source      TEXT,
      found_at    TEXT
    );

    CREATE TABLE user_jobs (
      user_id              INTEGER NOT NULL
                           REFERENCES users(id) ON DELETE CASCADE,
      job_id               INTEGER NOT NULL
                           REFERENCES jobs(id) ON DELETE CASCADE,
      score                REAL,
      verdict              TEXT,
      angle                TEXT,
      risk                 TEXT,
      stretch              INTEGER NOT NULL DEFAULT 0,
      score_breakdown_json TEXT,
      status               TEXT NOT NULL DEFAULT 'new',
      ignored_reason       TEXT,
      ignored_at           TEXT,
      updated_at           TEXT,
      PRIMARY KEY (user_id, job_id)
    );

    CREATE TABLE companies (
      name                  TEXT PRIMARY KEY,
      rating                REAL,
      review_count          INTEGER,
      rating_breakdown_json TEXT,
      review_summary_json   TEXT,
      sector_median_rating  REAL,
      fetched_at            TEXT
    );

    CREATE TABLE applications (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      job_id        INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
      tailored_json TEXT,
      outreach_json TEXT,
      prepared_at   TEXT,
      applied_at    TEXT,
      outcome       TEXT,
      outcome_note  TEXT,
      UNIQUE (user_id, job_id)
    );

    CREATE TABLE runs (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
      kind        TEXT NOT NULL,
      started_at  TEXT,
      finished_at TEXT,
      status      TEXT,
      jobs_found  INTEGER NOT NULL DEFAULT 0,
      error_text  TEXT
    );

    CREATE INDEX idx_user_jobs_board  ON user_jobs(user_id, status, score);
    CREATE INDEX idx_user_jobs_score  ON user_jobs(user_id, score);
    CREATE INDEX idx_jobs_company     ON jobs(company);
    CREATE INDEX idx_jobs_found       ON jobs(found_at);
    CREATE INDEX idx_runs_user        ON runs(user_id, started_at);
    """,

    # 2 — the company join is on LOWER(name), which no plain index can
    # serve, so every list view scanned the companies table once per row.
    # SQLite indexes expressions, so this needs no column and no backfill.
    """
    CREATE INDEX IF NOT EXISTS idx_companies_lower ON companies(LOWER(name));
    """,

    # 3 — somewhere to remember one number between scans.
    #
    # The paid search tier can only afford a handful of keywords per
    # scan, but three people between them have eighteen. Taking the
    # first six every time means the last twelve are never searched at
    # all — so the slice rotates, and rotation needs to survive a
    # restart. Not per-user: this is one cursor over the shared list.
    """
    CREATE TABLE IF NOT EXISTS settings (
      key   TEXT PRIMARY KEY,
      value TEXT
    );
    """,

    # 4 — job boards discovered by probing, rather than typed into YAML.
    #
    # SHARED, not per user, for the same reason `jobs` is shared: the
    # scrape happens once and every user's scorer reads the result. The
    # user who surfaced a board is recorded because it is useful to
    # know, not because it scopes anything.
    """
    CREATE TABLE IF NOT EXISTS job_boards (
      platform      TEXT NOT NULL,
      slug          TEXT NOT NULL,
      company_name  TEXT,
      jobs_seen     INTEGER NOT NULL DEFAULT 0,
      found_for     INTEGER REFERENCES users(id) ON DELETE SET NULL,
      enabled       INTEGER NOT NULL DEFAULT 1,
      discovered_at TEXT,
      last_seen_at  TEXT,
      PRIMARY KEY (platform, slug)
    );
    """,
]
