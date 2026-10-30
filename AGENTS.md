# Career Loop: private Shivani workspace

Use Python 3.11: `py -3.11`. Default `py` may select incompatible Python 3.14.
Single local dashboard, loopback only. No multi-user product work needed.
Never print credentials or whole API account/storage responses.
Private resumes, profile, jobs, API datasets and reports belong in ignored `data/`.

## Map
- `run.py`: serve, scan, sources, import-jobs, check-links, warm-up.
- `config/personal_sources.yml`: active discovery and compact scoring settings.
- `core/scheduler.py`: discover, persist, filter, score; at most 12 jobs per scan.
- `core/constraints.py`: location, salary, sales, shifts and experience constraints.
- `core/score.py`: compact evidence scoring, qualification caps, unknown dimensions.
- `core/apify_budget.py`: durable run reservations, billing cycle, cache, $5 ceiling.
- `core/source_registry.py`: free employer career-page discovery.
- `core/scrapling_source.py`: free HTML fetching, bounded transfers, six-hour cache.
- `core/db.py`, `core/schema.py`: private SQLite store and migrations.
- `web/app.py`, `web/jobs.py`, `web/templates/`: local UI and actions.
- `data/shivani/profile.json`: actual candidate evidence and pending clarifications.

## Cheap operation
- `py -3.11 run.py scan --discover-only`: free discovery, zero AI calls.
- `py -3.11 run.py scan`: bounded Naukri and Codex scoring; existing scores reused.
- `py -3.11 run.py serve --no-scan`: start without spending on boot.
- Apify: one $0.25 reservation per day, one cheap empty-result retry; $0.50 reserve.
- Failed/unknown paid POSTs must never be blindly retried.
- Billing and model costs are separate. No OpenAI API key is currently configured.
- Default scoring uses the authenticated Codex CLI. No OpenAI API key is needed.
- Do not pad prompts to force prompt caching. Prefer compact inputs and saved results.
- Drafts are prepared for review only. Never send applications or outreach automatically.

## Checks
Use `py -3.11 -m pytest -q --tb=short`. It includes real Chromium route/action tests.
Unset live API credentials in the test subprocess. Mock paid calls in new tests.
Run focused tests after minor edits; rerun full suite after integration changes.
Compile templates before exposing template edits to the running server.
Restart the exact verified app process after Python changes: old Python context
plus freshly loaded templates can otherwise break live pages. Start hidden.
Inspect desktop and phone layouts after changing navigation.

## Rating rules
Unknown compensation or work-life balance is unverified, not low fit.
Evidence coverage is separate from fit. Missing mandatory expertise caps fit at 5.
Never fabricate experience, salary, work authorization or location suitability.
Keep source failures visible. Candidate sources are not verified job listings.
