# Claude Code Build: Career Loop Portal

**Deadline: Tuesday. This is a real deadline, not aspirational.**
Read the "If you fall behind" section before you start — knowing what
to cut is what makes the date.

---

## Context

You are extending **Career Loop**, an existing Python job-intelligence
pipeline for the Indian market. The code is attached. Read `README.md`,
then `run.py`, then `core/`.

Today it is a single-user CLI. By Tuesday it must be a **local web portal
for three users**, with scheduled scanning, company review data, and an
ignore list.

Do not rewrite working code. Extend it.

---

## What exists

```
core/llm.py          shared Anthropic caller
core/intake.py       CV parse + adaptive Q loop + constraint compiler
core/calibrate.py    market reality check (web search)
core/discover.py     JobSpy + Greenhouse/Lever fetchers
core/filter.py       dedup
core/prefilter.py    free heuristic kill (497 → 124 in testing)
core/score.py        trajectory-weighted LLM scoring
core/tailor.py       CV tailoring + hiring manager outreach
core/notify.py       Telegram
core/feedback.py     outcome logging + diagnostic
```

The scoring philosophy matters and must survive: `experience_fit` is
weighted at 8%. This tool scores against where the user is *going*, not
where they've been. Stretch roles are the goal.

---

## PHASE 1 — SQLite (do this first, everything depends on it)

JSON files break the moment the portal needs "all non-ignored jobs,
score > 7, this week, sorted by company rating." Migrate to SQLite.

### Schema

```sql
users (
  id, username UNIQUE, password_hash, salt,
  telegram_chat_id, is_admin, created_at
)

profiles (
  user_id FK, target_json, hard_filters_json,
  weights_json, angles_json, updated_at
)

jobs (
  id, user_id FK, url, title, company, location,
  description, source, found_at,
  score REAL, verdict, angle, stretch BOOL,
  score_breakdown_json,          -- per-dimension, for the UI
  status,                        -- see enum below
  ignored_reason, ignored_at,
  updated_at,
  UNIQUE(user_id, url)
)

companies (                      -- SHARED across users, public data only
  name PRIMARY KEY,
  ambitionbox_rating REAL, review_count INT,
  rating_breakdown_json,         -- worklife, salary, growth, culture...
  review_summary_json,           -- the balanced summary
  sector_median_rating REAL,     -- for bias correction
  fetched_at
)

applications (
  id, job_id FK, user_id FK,
  tailored_json, outreach_json,
  prepared_at, applied_at,
  outcome, outcome_note
)
```

**Status enum:** `new` `interested` `ignored` `prepared` `applied`
`no_response` `rejected` `screening` `interview` `offer`

**Isolation rule:** every query against `jobs`, `profiles`, and
`applications` MUST filter by the session user's id. No exceptions. The
`companies` table is deliberately shared — it is public data and sharing
it saves real API cost.

Write `core/db.py` with the schema, migrations, and a `get_conn()`
helper. Migrate any existing `data/*.json` on first run.

---

## PHASE 2 — The portal (this is what makes it usable)

FastAPI + Jinja2 templates + Tailwind via CDN. **Not React.** Server-
rendered ships in a day and looks the same for a 3-person tool.

### Auth

- `hashlib.scrypt` password hashing. Never plaintext, never logged.
- Signed session cookie.
- Every route except `/login` requires a session.
- `python run.py seed-users` creates 3 accounts, prints passwords once.

### Routes

```
GET  /                  kanban board
GET  /jobs              list + filters (score, company, date, source)
GET  /jobs/{id}         full detail page
POST /jobs/{id}/status  move between columns
POST /jobs/{id}/ignore  with optional reason
POST /jobs/{id}/restore un-ignore
GET  /ignored           ignored list, restorable
POST /jobs/{id}/prepare generate tailored application
GET  /applications      prepared + applied tracking
GET  /export.xlsx       Excel export of current filter
GET  /intake            upload Google Form CSV
POST /login  /logout
```

### Kanban board

```
NEW  │  INTERESTED  │  PREPARED  │  APPLIED  │  OUTCOME
```

Drag or button to move. Ignore is a separate action, not a column —
ignored jobs leave the board entirely and live at `/ignored`.

### The job card — design brief

The user asked for a "confidence-inducing" frontend. **Build for clarity
instead.** Confidence should be earned by showing the reasoning, not
manufactured by the styling. A UI that makes every listing feel great
causes bad applications.

Concretely, every job detail page shows:

- **Score with its breakdown**, not just a number:
  `trajectory 9 · domain leverage 8 · AI depth 4 · company 7 · exp fit 6`
- **Why it fits** — the `angle` field
- **Why it might not** — surface the weakest dimension explicitly. Every
  job gets a "reasons against" section. If there are none, say that.
- **Stretch badge** if above their current level — framed as a positive
- **Company reviews** (Phase 4)
- Buttons: `Interested` `Ignore` `Prepare application`

Use restrained, professional styling. Good typography and whitespace.
No gradient hero banners, no confetti, no "🔥 PERFECT MATCH" badges.
The data should carry the persuasion.

---

## PHASE 3 — Google Form intake

Replace the CLI questionnaire.

1. Generate a **Google Form template spec** the user can paste in —
   questions mapped to the dimensions in `config/profile_schema.yml`
2. `/intake` accepts the Form's CSV export (or a Sheets URL)
3. Run the existing `intake.compile_profile()` on those answers
4. Show the user which answers were **dropped as unusable** — that is a
   feature, not an error. Vague input producing vague output is the
   failure mode of every AI career tool.

Skip the adaptive follow-up questions for v1. Note it as a TODO.

---

## PHASE 4 — Company reviews (the highest-value addition)

Enrich each job with employer review data from **AmbitionBox** — India's
largest employer review platform, 100K+ companies, owned by
Naukri/InfoEdge so its coverage maps onto the listings already pulled.

### Data source

Use Apify. Working actors:

| Actor | Use |
|---|---|
| `muhammetakkurtt/naukri-job-scraper` | **Preferred.** Naukri listings *with AmbitionBox ratings attached.* Solves the 406 block AND review data in one call. |
| `thirdwatch/ambitionbox-scraper` | Company ratings + per-dimension breakdown, no login |
| `getdataforme/ambitionbox-reviews-scraper` | Individual reviews with pros/cons |

Requires `APIFY_TOKEN` env var. Use India residential proxies (default
config). Cache aggressively in the `companies` table — refetch only if
`fetched_at` is older than 30 days. Review data barely moves; paying for
it twice is waste.

### The summary — bias correction is mandatory

Employer reviews skew negative. Angry ex-employees write reviews; content
employees do not. A 3.2 on AmbitionBox is often an ordinary company, not
a bad one.

The LLM summary must therefore:

- Compare against the **sector median**, not an absolute scale.
  Say "3.2, typical for Indian banking" — never "3.2, low."
- Separate **current vs former** employee sentiment where available
- Weight **recent reviews higher** — a 2022 review of a restructured
  company is noise
- Surface **genuine red flags** as a distinct section: salary delays,
  mass layoffs, litigation. These are different from grumbling.
- State **review count** prominently. 8 reviews is an anecdote, not data.

Return shape:

```json
{
  "rating": 3.4,
  "review_count": 412,
  "sector_context": "slightly above median for Indian fintech (3.2)",
  "strengths": ["3-4 themes, each with rough frequency"],
  "concerns": ["3-4 themes, each with rough frequency"],
  "red_flags": ["only genuine ones, or empty"],
  "current_vs_former": "how sentiment differs between the two",
  "confidence": "high|medium|low based on review count and recency"
}
```

Render it Amazon-style: rating, distribution bar, themed pros and cons
side by side. **Show the concerns at equal visual weight as the
strengths.** Do not bury them.

---

## PHASE 5 — Boot-triggered catch-up scanning

The portal runs on localhost and will **not** be running at 07:00. A
plain cron schedule would fire into a closed laptop and silently do
nothing. So the trigger is startup, not the clock.

### The model

Scheduled *slots* still exist — 07:00 and 18:00 Asia/Kolkata — but they
are **targets, not triggers**. On startup the app asks: "has a slot
passed since the last successful scan?" If yes, it runs one catch-up
scan. If no, it does nothing.

Boot at 11pm having last scanned yesterday morning → both slots missed →
**one** catch-up scan, not two. Missed slots never queue up.

### `python run.py serve`

Starts the portal. On startup:

1. Read the most recent successful run from the `runs` table
2. Decide: is a catch-up scan due?
3. **Serve the portal immediately** — never block the UI on a scan.
   Run the scan in a background thread or asyncio task.
4. Show live status in the UI header: `Scanning… 34 found` → `Last
   scan: 12 min ago`

### Debounce — this is the part that protects your wallet

Hard rules, all of them enforced:

- **Minimum 3 hours between automatic scans**, regardless of missed
  slots. Restarting the app six times while debugging must produce at
  most one scan.
- A scan already in progress blocks a second from starting.
- `--no-scan` flag skips the boot scan entirely. Use it while developing.
- `--dry` still means zero LLM calls and zero sends.

### `runs` table

```sql
runs (
  id, user_id FK, kind,          -- deep | delta | manual | catchup
  started_at, finished_at,
  status,                        -- running | ok | failed
  jobs_found INT, error_text
)
```

This table is what makes the debounce and catch-up logic possible.
Build it before the scan logic.

### Manual scan

A **Scan now** button in the portal header. Disabled while a scan is
running and during the 3-hour debounce window — but with an override for
the user who really wants it, since it is their money. Show the
remaining cooldown on hover.

### Multi-user behaviour

A boot scan runs for **all three users**, not just whoever started the
app. Otherwise user B's board goes stale whenever user A is the one
opening the laptop.

Per-user isolation still holds: if user B's scan throws, A and C must
complete. Wrap each user in its own try/except and record the failure in
that user's `runs` row.

### Telegram

Send after a scan completes, to each user with results. **Short** —
count and top 3 titles plus a portal link. The detail lives in the
portal.

Guard: if a user has no chat id, skip and warn. Never route one user's
results to another's chat.

### Keep cron as an option, not the default

Still generate a crontab entry and a launchd plist for the case where the
machine *is* left on — `python run.py scan` should work standalone. But
the boot-scan path is the primary mechanism and must work with no
scheduler installed at all.

**If you do wire cron:** it runs with a minimal environment. Source env
vars explicitly in the script. That is the single most common reason
scheduled jobs silently do nothing.

---

## PHASE 6 — Excel export

`GET /export.xlsx` — respects current filters. Use `openpyxl`.
Columns: score, breakdown, title, company, rating, review count,
location, status, angle, URL, found date.

30 minutes of work. Do it last.

---

## Hard constraints

- **Never auto-apply.** No form filling, no submitting, no clicking.
  The system drafts; the human sends. Deliberate, not an oversight.
- **Never write plaintext passwords** to any file or log.
- **Never let one user's data reach another.** Write a test that proves
  it: user A cannot read B's jobs, and A's Telegram never hits B's chat.
- Keep a `--dry` path with zero LLM calls and zero sends, so testing
  three users daily costs nothing.
- Preserve `experience_fit` at low weight.

---

## Build order — checkpoints, not phases

| Day | Ship | Checkpoint |
|---|---|---|
| 1 | Phase 1 + auth | 3 users log in, existing pipeline writes to SQLite |
| 2 | Phase 2 portal | Kanban renders real jobs; ignore and restore work |
| 3 | Phase 4 reviews + Phase 3 intake | A job detail page shows a balanced review summary |
| 4 | Phase 5 scheduler + Phase 6 export + tests | 7am run fires; isolation test passes |

Run the pipeline end to end after every checkpoint. If it breaks, fix it
before moving on. A half-finished Phase 4 on top of a broken Phase 2 is
worse than stopping at Phase 2.

---

## If you fall behind

Cut in this order. Cutting is how you make Tuesday.

1. **Hiring manager outreach** — flakiest component, depends on web
   search hitting LinkedIn. Make it non-blocking already; drop entirely
   if needed.
2. **Excel export** — the portal shows the same data.
3. **Individual review text** — keep the aggregate rating and themed
   summary, drop per-review scraping.
4. **Cron / launchd files** — boot-scan alone covers the use case.
5. **The 18:00 slot** — one slot a day still works with catch-up.

**Never cut:** SQLite, auth, user isolation, the ignore list, or the
score breakdown on the job card. Those are the product.

---

## Deliverables

1. `core/db.py` + schema + migration from existing JSON
2. `core/session.py` — auth, sessions, user scoping
3. `web/` — FastAPI app, Jinja templates, routes above
4. `core/reviews.py` — Apify enrichment + bias-corrected summary
5. `core/scheduler.py` — boot catch-up logic, `runs` table, debounce,
   background execution; plus optional crontab / plist
6. `python run.py seed-users` — 3 working accounts
7. `tests/test_isolation.py` — proves users cannot see each other
8. Updated `README.md` — setup, daily rhythm, what got cut and why
