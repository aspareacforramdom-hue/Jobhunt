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

    # 5 — how sure we are that a discovered board is the right company.
    #
    # "Tata Consumer Products" can only be probed as guesses, and the
    # last guess tried is the bare first word — which found the real
    # `zeta` board for "Zeta Suite" but would just as happily match some
    # other Tata on `tata`. Boards found that way are recorded as
    # `loose` so a person can glance at them, rather than being either
    # silently trusted or silently dropped.
    """
    ALTER TABLE job_boards ADD COLUMN confidence TEXT NOT NULL DEFAULT 'exact';
    """,

    # 6 — the résumé itself, kept rather than thrown away.
    #
    # It used to be read once during intake, compiled into a handful of
    # sentences, and discarded. Everything downstream then judged jobs
    # against that summary: the scorer saw ~3 sentences about a person
    # and the tailor was asked to quote "an existing bullet from your CV"
    # from a document it had never seen — so it invented one, and the
    # interface presented the invention as something the user had
    # written. This column is what makes that promise keepable.
    #
    # PRIVACY: this is the most sensitive text in the database. It never
    # leaves it — core/publish.py must never emit it (there is a test),
    # and it is not in any list query.
    """
    ALTER TABLE profiles ADD COLUMN resume_text TEXT;
    """,

    # 7 — the line from the candidate's own CV that makes the case for a
    # role. Stored per user, like every other opinion about a job, and
    # rendered on the detail page: the difference between "you would be
    # good at this" and "here is the thing you have already done that
    # proves it".
    """
    ALTER TABLE user_jobs ADD COLUMN evidence TEXT;
    """,
    # 8 - when a listing was last seen, and when it was found to be gone.
    #
    # Measured on the three live boards: 20 of 70 links were already HTTP
    # 404, a 29% dead rate, and the median card was 31 days old. Nothing
    # in the codebase had ever expired a job, so the pool only grew and
    # a person clicking through their board mostly met "page not found".
    #
    # last_seen_at is stamped on every raw row of every scan, including
    # ones the scan already knows about - the dedup drops those before
    # they reach upsert_job, which is exactly why this could not be
    # inferred before. closed_at is set once, and never cleared by a
    # later sighting; a reposted req arrives with a new url.
    """
    ALTER TABLE jobs ADD COLUMN last_seen_at TEXT;
    ALTER TABLE jobs ADD COLUMN closed_at TEXT;
    UPDATE jobs SET last_seen_at = found_at WHERE last_seen_at IS NULL;
    CREATE INDEX IF NOT EXISTS jobs_open ON jobs (closed_at, source, company);
    """,
    # 9 - absence from a board is a SUSPICION, not a verdict.
    #
    # Migration 8 was built on the assumption that a Greenhouse or Lever
    # board answers with the company's complete current list, so anything
    # missing had been taken down. Measured against reality, that closed
    # 278 of 963 listings and 60 of them were probed: 42 still answered
    # HTTP 200 with a working apply button. A 70% false positive rate.
    #
    # The cause is not pagination - the Lever API returns the same 19
    # rows for mindtickle at any limit - it is that employers can unlist
    # a posting while leaving the direct link live and applicable. So
    # absence proves nothing on its own, and acting on it removes real
    # opportunities from somebody's board.
    #
    # missing_since records the suspicion. It decides what the link
    # checker looks at FIRST; only an HTTP answer ever sets closed_at.
    """
    ALTER TABLE jobs ADD COLUMN missing_since TEXT;
    CREATE INDEX IF NOT EXISTS jobs_missing ON jobs (missing_since)
        WHERE closed_at IS NULL;
    """,
    # 10 - the second target, and the reasoning behind both.
    #
    # The intake became a preference quiz that never asks "what job do
    # you want", because most people cannot answer that - working out
    # the title that fits you is the thing this tool is for. The
    # compiler infers TWO targets from the pattern of answers: the
    # direction worth stretching for, and the best-paid job the CV
    # already sells.
    #
    # save_profile persisted neither. It writes a fixed column list, so
    # leverage_target, track_weighting and inference_note were computed,
    # returned, and dropped on the floor at the moment of saving - no
    # error, just half the feature quietly missing.
    #
    # inference_note is kept because an inferred career direction the
    # person cannot see is one they cannot disagree with, and this one
    # is a guess about somebody's life made from a questionnaire.
    """
    ALTER TABLE profiles ADD COLUMN leverage_target_json TEXT;
    ALTER TABLE profiles ADD COLUMN track_weighting INTEGER;
    ALTER TABLE profiles ADD COLUMN inference_note TEXT;
    """,
    # 11 - which of the two targets this job actually fits.
    #
    # The intake infers two: the job she wants to grow into, and the
    # best-paid job her CV already sells. Without recording which one a
    # listing matched, both land in one undifferentiated list and the
    # distinction the whole two-track intake exists to draw is lost at
    # the last step - she would be asked to work out for herself which
    # of 40 cards were the ambitious ones.
    """
    ALTER TABLE user_jobs ADD COLUMN track TEXT;
    """,
    # 12 - the link checker's queue was a full table scan.
    #
    # It asks for open listings on somebody's board, oldest first, and
    # the planner had no index for "this user's rows with status new",
    # so it read every row of user_jobs to find them. At 76 rows that is
    # 0.7ms and invisible; the table grows by the size of every board on
    # every scan, and this runs at the end of each one.
    """
    CREATE INDEX IF NOT EXISTS idx_user_jobs_new
        ON user_jobs (status, job_id) WHERE status = 'new';
    """,
    # 13 — persist enrichment work and rotate link checks across the pool.
    """
    ALTER TABLE jobs ADD COLUMN needs_enrichment INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE jobs ADD COLUMN link_checked_at TEXT;
    UPDATE jobs SET needs_enrichment = 1 WHERE source = 'firecrawl';
    """,
    # 14 — one-candidate brief, provenance and score revision.
    """
    ALTER TABLE profiles ADD COLUMN extra_json TEXT;
    ALTER TABLE jobs ADD COLUMN metadata_json TEXT;
    ALTER TABLE user_jobs ADD COLUMN evaluation_json TEXT;
    ALTER TABLE user_jobs ADD COLUMN profile_revision TEXT;
    CREATE TABLE outcome_events (
      id INTEGER PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(id),
      job_id INTEGER NOT NULL REFERENCES jobs(id),
      status TEXT NOT NULL,
      note TEXT,
      happened_at TEXT NOT NULL
    );
    """,
    # 15 — persistent, independently observable career-page coverage.
    """
    CREATE TABLE career_sources (
      id INTEGER PRIMARY KEY,
      company TEXT NOT NULL,
      url TEXT NOT NULL UNIQUE,
      platform TEXT NOT NULL,
      locator TEXT NOT NULL,
      provenance TEXT NOT NULL,
      priority INTEGER NOT NULL DEFAULT 0,
      enabled INTEGER NOT NULL DEFAULT 1,
      state TEXT NOT NULL DEFAULT 'queued',
      last_checked TEXT,
      next_check TEXT,
      failures INTEGER NOT NULL DEFAULT 0,
      jobs_seen INTEGER NOT NULL DEFAULT 0,
      relevant_seen INTEGER NOT NULL DEFAULT 0,
      detail TEXT
    );
    CREATE INDEX idx_career_sources_due ON career_sources(enabled, next_check, priority);
    """,
]
