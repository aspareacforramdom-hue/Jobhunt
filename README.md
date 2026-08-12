# Career Loop

A job intelligence machine for the India market, now running as a
local portal for three people with completely separate data.

Human-in-the-loop by design. It never applies for you.

---

## The one idea

This tool scores against **where you're going, not where you've been**.

`experience_fit` is weighted under 10%, so a role you can't quite do yet
scores high rather than getting filtered out. Stretch roles are flagged
as a positive and never suppressed.

Everything else in here follows from that.

---

## Scoring dimensions are generated, not fixed

The scorer used to have five hardcoded dimensions, one of which was
`ai_product_depth`. For anyone not chasing AI roles that scores zero on
every listing and skews their whole board for a reason that has nothing
to do with them.

So dimensions are now **data**. Three are universal because they're true
of every career move; the rest are invented by the compiler from that
person's own intake, each with a definition the scorer applies.

| Universal (everyone) | Generated (per user, 2-3) |
|---|---|
| `trajectory_fit` | whatever their intake compiles to |
| `company_quality` | |
| `experience_fit` (capped under 10) | |

Two real profiles end up looking like this:

```
someone moving into AI product   →  ai_product_depth · domain_leverage
someone moving into brand work   →  creative_ownership · brand_scale
```

Neither inherits the other's vocabulary. `core/dimensions.py` normalises
whatever the LLM produces: undefined dimensions are dropped (an
undefined dimension can't be scored consistently), more than three are
trimmed, `experience_fit` is capped, and the weights are rescaled to
exactly 100. Every change it makes is reported to the user rather than
applied silently.

No role, industry, or skill is named anywhere outside a user's own
profile. `tests/test_dimensions.py` enforces it.

---

## The interface

An editorial instrument. Warm paper, hairline rules instead of floating
cards, a serif you're meant to read slowly, and mono for anything
countable. There is no Tailwind and no CDN framework — one hand-written
stylesheet at `web/static/careerloop.css`.

The brief was "build for clarity, not persuasion", which is a mandate
for craft rather than for plainness. So there are no gradients, no glow,
and no colour-coded score badges — colouring a 6.2 green is persuasion
dressed as information. The accent colour appears about six times per
page, and one of them is the stretch mark, the only place it means
"good".

Two rules the design enforces on the product:

- **Fit and risk share one visual language.** "Why it fits" and "Why it
  might not" get the same type, the same rules and the same weight, and
  the risk section is never collapsed. An interface that dresses up the
  upside produces confident bad applications.
- **Inline numerals are mono, not the display serif.** Fraunces draws
  `4` with an open apex that thins out below ~30px and reads as a `1` —
  fatal in a tool whose job is comparing 8.4 against 4.6 down a column.
  Only the hero readout, large enough to render correctly, uses the serif.

Type: Fraunces (display), Newsreader (prose), IBM Plex Mono (data and
chrome). All three fall back to solid system stacks offline.

---

## Setup

```bash
pip install fastapi uvicorn jinja2 python-multipart openpyxl pyyaml pytest
```

Optional, for the free aggregator tier:

```bash
pip install python-jobspy pandas
```

Optional, for the browser end-to-end tests:

```bash
pip install playwright && playwright install chromium
```

```bash
export ANTHROPIC_API_KEY="sk-ant-..."      # required
export APIFY_TOKEN="apify_api_..."         # optional: Naukri + reviews
export FIRECRAWL_API_KEY="fc-..."          # optional: open web + career pages
export TELEGRAM_BOT_TOKEN="..."            # optional
export CAREERLOOP_SECRET="..."             # optional: else generated on disk
export CAREERLOOP_PORTAL_URL="http://..."  # optional: link sent to Telegram
```

Create the accounts, then start the portal:

```bash
python run.py seed-users alice bob carol
```

```bash
python run.py serve
```

Passwords are printed **once** and are not stored anywhere. Re-running
`seed-users` will not reset an account that already exists.

If `data/*.json` from the single-user era is present, it is imported on
first run and attached to the first user. The JSON is never deleted.

---

## Daily rhythm

1. Open the portal. If a scheduled slot has passed since the last
   successful scan, a catch-up scan starts in the background and the
   board is served immediately.
2. Read the board. Five columns: **New · Interested · Prepared ·
   Applied · Outcome**.
3. Open anything interesting. The detail page shows the score
   breakdown in your own dimension names, why it fits, and — always —
   why it might not.
4. Ignore what doesn't fit, with a reason. It leaves the board and
   lives at `/ignored`, restorable with the reason intact.
5. Hit **Prepare application** on the ones worth it. Three copy
   buttons: cover letter, CV bullet swaps, LinkedIn note.
6. Log what came back on `/applications`. That's what lets the scorer
   learn.

---

## Thresholds are relative, not absolute

There is no fixed pass mark, deliberately. A first run against a thin
market can score everything 5.8, and a hardcoded 6.5 bar would render an
empty board — which reads as "there are no jobs" when it actually means
"the bar was set before there was any data to set it from".

- **4.0** is an absolute floor. It kills obvious garbage, nothing else.
- The board shows the **top 15** for the day, whatever they scored.
- The **score distribution** sits above the board so you can see where
  the real bar is.

The board is never empty while anything has been scored.

---

## Scanning happens on boot, not on a clock

The portal runs on a laptop and won't be running at 07:00, so cron would
fire into a closed lid.

Slots (07:00 and 18:00 IST) are **targets, not triggers**. On startup the
scanner asks: *has a slot passed since the last successful scan?* If yes,
it runs **one** catch-up scan. Boot at 23:00 having last scanned
yesterday morning means two slots were missed — that's still one scan.
Missed slots never queue.

Debounce, all three enforced:

- minimum **3 hours** between automatic scans, so six debug restarts
  produce at most one scan
- a running scan blocks a second from starting
- `--no-scan` skips the boot scan entirely

The **Scan now** button is disabled during a run and during cooldown,
with the remaining time on hover — but the override is allowed. It's
your money.

```bash
python run.py serve --no-scan     # development
python run.py serve --dry         # zero LLM calls, zero sends
```

One scrape serves all three users: the union of everyone's search terms,
each listing stored once. Scoring is per user, and each user is wrapped
separately — if one user's scoring throws, the other two still finish and
the failure is recorded in their `runs` row.

---

## Data isolation

| Shared | Per user |
|---|---|
| `jobs` — public listings, scraped once | `user_jobs` — score, verdict, status, ignore reason |
| `companies` — public review data, cached 30 days | `profiles`, `applications`, `runs` |

Every helper touching per-user data takes `user_id` as its **first**
argument and filters on it. There is no variant that doesn't.

`tests/test_isolation.py` proves user A cannot read B's jobs through any
filter combination or by guessing a job id, and that A's Telegram
summary can never reach B's chat — a user with no chat id is skipped,
never quietly defaulted to whatever `TELEGRAM_CHAT_ID` is in the
environment.

Passwords are scrypt-hashed with a per-user salt and never written to a
file or a log. The session cookie is HMAC-signed, so a user id can't be
edited by hand.

---

## Source reliability, inverted

Naukri's bot protection changes monthly and returned `406 recaptcha
required` from a cloud IP during testing. A boot-scan tool that silently
breaks is worse than one that costs money, so the paid, maintained path
now leads.

| Tier | Source | Cost | Reliability |
|---|---|---|---|
| 1 | Greenhouse / Lever direct | free | never breaks — public JSON |
| 2 | JobSpy → LinkedIn, Indeed | free | medium |
| 2.5 | Firecrawl Cloud → open web + career pages | ~$16/mo | managed |
| 3 | Apify → Naukri | ~$7/mo | maintained by the actor author |

Each paid tier no-ops without its key, and the UI says which sources
actually ran rather than making a degraded scan look like a thin day in
the market.

### Firecrawl is cloud, deliberately

It covers the long tail neither the ATS JSON nor Apify reaches: job
boards this tool has no adapter for, and employers whose careers page is
bespoke (Darwinbox, Keka, Zoho Recruit, hand-rolled).

**Self-hosting was evaluated and rejected.** Firecrawl's anti-bot layer
("Fire-engine") is proprietary and absent from the open-source stack, so
a self-hosted instance fetches with plain Playwright and gets
fingerprinted by exactly the protection that makes Naukri hard — the one
job it would have been hired for. It also wants Postgres, Redis,
RabbitMQ, Playwright and an API container, on a laptop whose whole
scheduling model exists because it is *not* always on. Apify keeps
Naukri; Firecrawl takes the tail.

**Cost control is the design.** Search is billed per result set and
scraping per page, so the module searches cheaply and scrapes almost
nothing:

1. `/search` returns title, URL and a snippet — enough to dedup and run
   the free title heuristic on.
2. Only listings that **survive the prefilter** get their full text
   fetched, one credit each.

That is the same bargain `core/prefilter.py` already makes with the LLM
— spend nothing on the 80% you were about to discard — applied to crawl
credits. Scraping every search result up front would cost several times
more for pages the prefilter kills seconds later. `tests/test_firecrawl.py`
pins that ordering end to end: three listings found, one survives,
exactly one page credit spent.

Queries are built from each user's own calibrated `search_terms`, so no
company list and no role vocabulary is hardcoded — a test asserts the
extractor prompt and schema contain no role, industry or seniority words.

Caps live in `config/sources.yml` (`max_terms_per_scan`,
`max_enrich_per_scan`, `max_pages_per_scan`). With the defaults a scan
costs roughly 12 search credits plus at most 25 page credits.

---

## Company reviews are bias-corrected

Employer reviews skew negative — people write them on the way out;
content employees write nothing. A raw 3.2 next to a five-star scale
reads as "bad company" when it usually means "ordinary company".

So every summary compares to the **sector median** rather than an
absolute scale, separates current from former employees, weights recent
reviews higher, keeps genuine red flags (salary delays, layoffs,
litigation) apart from ordinary grumbling, and states the review count
prominently. Under ~20 reviews is an anecdote and marked low confidence.

Rendered with concerns at equal visual weight to strengths, never
collapsed.

---

## Commands

```bash
python run.py serve                   # the portal
python run.py serve --no-scan --dry   # development
python run.py seed-users a b c        # accounts, passwords shown once
python run.py migrate --user alice    # import old data/*.json

python run.py intake --cv cv.txt      # adaptive CLI intake (still works)
python run.py calibrate               # market reality check
python run.py scan                    # single-user CLI loop
python run.py log <url> interview     # record an outcome
python run.py review --apply          # diagnose and re-weight
```

---

## Verified

| Layer | Status |
|---|---|
| 192 tests | passing |
| Every page, in real Chromium, at 390 / 820 / 1400px | passing |
| Two profiles with disjoint dimensions score correctly | verified |
| User isolation across every filter, plus Telegram routing | verified |
| Boot-scan slot arithmetic + 3h debounce | verified |
| `--dry` makes zero LLM calls end to end | verified |
| Live scraping — 850 raw → 773 deduped → 193 prefiltered | verified |
| Live LLM scoring | **needs your ANTHROPIC_API_KEY** |
| Apify actor input schemas | **unverified — see below** |

`tests/test_hardening.py` and `tests/test_hardening_web.py` are the
adversarial suites — malformed LLM output, hostile scraped titles,
non-finite weights, racing scan threads, corrupt legacy JSON, open
redirects, spreadsheet formula injection, and CSV that doesn't match its
own header row. They found 24 real bugs; see "What the bug hunt found".

`tests/test_e2e_browser.py` boots a real uvicorn and walks Chromium
through the daily rhythm — sign in, read the board, open a listing,
ignore it, restore it, move columns, filter, export — then checks the
fonts resolved, no page scrolls sideways at phone width, hover-revealed
actions are still reachable by keyboard, and the console stayed clean.
It skips itself if playwright isn't installed.

---

## Performance

Measured before and after on an 800-job corpus, not guessed:

| Hot path | Before | After |
|---|---|---|
| Board render (`/`) | 308 ms, 6 queries | **3 ms, 4 queries** |
| Score distribution | 131 ms | **2 ms** |
| List payload | 96% descriptions the view never renders | **none** |
| ATS fan-out (8 boards) | 6.2 s serial | **1.3 s pooled** (4.7×) |
| Prefilter (800 jobs) | 33 ms | **8 ms** |

What each was:

- **The board issued one query per column** plus another for the
  distribution, and every one selected `j.*` — dragging each job's 4KB
  description into a view that renders none of them. On 800 jobs that
  was 3.5MB moved to display fifteen titles. Columns are now one capped
  query plus one for the curated four, descriptions are named out of the
  projection, and only the detail page — which shows the text — asks for
  it.
- **The distribution hydrated 800 full rows** — descriptions, company
  joins, JSON parsing — to compute a median and ten bucket counts. It
  reads one column now.
- **ATS boards were fetched one slug at a time**, ~960 ms each and
  essentially all of it waiting on a socket. Thirty boards cost ~29 s of
  idle CPU, landing squarely on the boot scan a user is waiting behind.
  A capped thread pool cuts that to seconds; a dead slug no longer takes
  the tier down with it.
- **The prefilter rebuilt the target's token set for every listing.**

`tests/test_efficiency.py` pins the *shape* of these — query counts,
columns fetched, concurrency — rather than wall-clock, so they stay
honest on a slow machine and fail if the gains silently revert.

### Measured, then deliberately not done

- **Per-request connection reuse.** Two connections per request at
  ~3.8 ms each is now larger than the 3 ms board render. It stays: the
  cost is attaching a WAL database's `-wal`/`-shm` files, which any
  connection pays, so the obvious fix (dropping the `journal_mode`
  pragma) measurably does not help. The real fix is sharing one
  connection across middleware and route — ~4 ms per request on a
  three-person localhost tool, against touching every handler. Not worth
  it before ship.
- **Prompt caching is wired but may not engage.** Scoring sends the same
  system block — instructions plus that user's dimensions — ahead of all
  ~25 batches in a scan, so it is marked cacheable with the volatile
  listings after the breakpoint. But the prefix measures ~3,000
  characters (roughly 800–1,000 tokens depending on tokenization) against
  a **1,024-token minimum** on `claude-sonnet-4-6`, and below that the API
  declines to cache *silently* — no error, just full price. The code is
  correct and costs nothing if it doesn't engage, and a scan now prints
  whether the cache actually read, so one real run settles it. If it
  reports "NOT engaging", the fix is `CAREERLOOP_MODEL=claude-opus-5`
  (512-token minimum) — a pricing decision, so it is left to you.

---

## What the bug hunt found

Two adversarial passes turned up 24 defects. The ones worth knowing about:

- **Telegram digests died on ordinary job titles.** Every scraped field
  was interpolated raw into `parse_mode: HTML`. A role called
  `C++ <Dev> & Co` makes Telegram reject the *whole* message with a 400,
  and the failure was swallowed — the user simply stopped hearing from
  the bot. Everything is escaped now.
- **Open redirect on every board action.** The `back` form field was
  echoed straight into the `Location` header, so `back=https://evil/`
  bounced the user off-site after clicking Ignore. Only same-origin
  paths are accepted now, including the protocol-relative `//host` and
  `/\host` forms.
- **The scorer trusted the model's numbers.** A dimension scored 47 on a
  1-10 scale rendered as 47 and silently dominated the weighted average,
  making every other listing look worse. Now clamped.
- **`inf` in a weight became `NaN` in the profile.** `float("inf")`
  survives parsing, then `inf/inf` in the rescale produces NaN, which
  loses every comparison it takes part in — so the sort deciding which
  dimensions to keep went arbitrary. Non-finite weights are rejected.
- **A picked-up-and-closed laptop built a 730-item list** and rendered
  every missed slot into the status string shown in the UI. Slot hunting
  is capped at 14 days; the answer was always "run one scan" anyway.
- **Excel formula injection in the export.** A scraped title beginning
  `=` executes when the file is opened. Values are now text-prefixed,
  and control characters — which make openpyxl refuse to save at all —
  are stripped.
- **The same job counted twice.** `jobs.url` is the shared pool's
  identity, so one role reached with an email campaign tag and again
  without it became two rows: reviewed twice, scored twice, paid for
  twice. URLs are canonicalised (case-folded host, no trailing slash,
  known tracking params dropped — listing ids in the query string are
  deliberately kept).
- **An invalid status was a 500**, not a rejection; junk filter values
  (`days=99999999999`) overflowed; and a CSV row wider than its header
  crashed intake, because `csv.DictReader` puts the overflow in a *list*
  under a `None` key.

Things that were already right, and now have tests pinning them: Jinja
autoescaping of scraped listing text, SQL parameterisation under
injection attempts, session cookies for deleted users, concurrent
read/write between the scan thread and the portal, and the scan lock
admitting exactly one of ten racing starts.

---

## What got cut, and why

**Nothing from the never-cut list was cut.** Phase 0 de-hardcoding,
SQLite, auth, user isolation, the ignore list, the score breakdown and
the boot scan are all in.

- **Adaptive follow-up questions in the web intake.** The CLI asks
  whichever dimension is most uncertain, up to seven; a static Google
  Form can't. `python run.py intake --cv cv.txt` still runs the adaptive
  version. Marked TODO in `web/intake_routes.py`.
- **DOCX / PDF generation.** Slow to build, slow to run, and the output
  gets hand-edited anyway. The prepare screen is three copy buttons.
- **Hiring manager outreach is best-effort, not dropped.** It does a web
  search that fails often, so it's wrapped — the cover letter, bullet
  swaps and LinkedIn note render regardless. The LinkedIn note comes from
  the tailoring call, not the outreach call, specifically so a failed
  search can't take it down.

## Known gaps

- **The Apify actor input schemas are unverified.** I couldn't exercise
  them without a token. `core/discover.py` and `core/reviews.py` send a
  tolerant payload and read results through a field-alias map, but the
  first live run may need the input keys adjusted to match the actor's
  actual schema. Failures are caught and logged, so this degrades rather
  than crashes.
- **No CSRF tokens.** Session cookies are `SameSite=Lax`, which blocks
  cross-site form posts. Fine for three people on localhost; not fine if
  this is ever exposed to a network.
- **`config/target.yml` was deleted.** It was read by no code and
  contained one person's hardcoded target — including the fixed
  `ai_product_depth` weight and an `exclude_keywords` list blocking
  "sales", which would have gutted a brand-marketing search. Leaving it
  around was a trap.
- Root-level duplicate copies of the `core/` modules were deleted. They
  were byte-identical to `core/` and imported by nothing.

---

## What it deliberately does not do

- **No auto-apply.** No form filling, no submitting, no clicking. The
  system drafts; you send. Violating this would risk your Naukri and
  LinkedIn accounts, and volume is the wrong strategy for narrow
  senior-track roles anyway.
- **No government jobs.** Different scrapers, different problem.
