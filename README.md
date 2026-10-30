# Shivani’s Career Loop

One local dashboard for Shivani’s job search. No account setup or public website.

## Run

On this configured laptop, double-click **Start Career Loop.cmd**.
See [the handover guide](docs/HANDOVER.md) for the daily workflow and recovery.

```powershell
py -3.11 -m pip install -r requirements.txt
py -3.11 run.py serve --no-scan
```

Open http://127.0.0.1:8000. Startup waits for a manual scan by default.
Run `py -3.11 run.py scan` for a manual search. An OS lock prevents overlapping
dashboard and command-line scans. `--scan-on-start` explicitly enables catch-up.

The private profile is `data/shivani/profile.json`; the database is
`data/shivani/careerloop.db`. Both are excluded from Git. The prior shared database
is preserved separately and is not imported into this workspace. Startup refuses
a personal database containing another account. Bind only to localhost.

## Search scope

Operations, customer experience, client onboarding and service delivery.
Bengaluru and India remote; 25 LPA minimum where salary is disclosed.
These provisional preferences can be changed in the dashboard search brief.
Undisclosed compensation remains explicitly unverified. Sales/revenue ownership
and heavy travel are excluded. Conflicting employment dates remain flagged.

`config/personal_sources.yml` controls free ATS/JobSpy discovery and caps model
scoring at 12 leads per scan. Public listing enrichment attempts fuller descriptions;
blocked or missing descriptions remain visible as uncertain. Search terms rotate.
A changed profile causes new leads to be reconsidered on the next search.

Scoring runs one bounded `gpt-6-luna` pass through the locally authenticated
Codex CLI. It uses the ChatGPT login already held by Codex and requires no
`OPENAI_API_KEY`. Optional application drafting still requires that API key.
Firecrawl uses five search results and at most four basic-proxy enrichments;
Naukri uses the budgeted Apify path below. Application drafts require review;
no submission or recruiter messaging is performed. Candidate evidence and the
twelve shortlisted descriptions are sent to Codex for scoring.

## Connector imports

Export a JSON array with `title`, `company`, `url`, `location`, and `description`.
Run `py -3.11 run.py import-jobs listings.json`, then run a scan. Imports are
bounded to 500 records / 5 MB. Import does not apply for jobs or contact anyone.

## Validate

```powershell
py -3.11 -m pytest -q
```

See [AUDIT.md](AUDIT.md) for engineering findings and limitations. Legacy account,
intake and publication implementations remain for compatibility; their commands
are absent from the active personal CLI. Older usage is archived in
[docs/LEGACY_README.md](docs/LEGACY_README.md).

## Worldwide career-source registry

Open `/sources` for the searchable inventory, observed listing counts, provenance,
last check, and failure state. Imported board identifiers are candidate addresses,
not verified employers or matches. The catalog currently imports Greenhouse,
Lever, Ashby and Workday identifiers, prioritising financial-services names.
Goldman Sachs has a direct public GraphQL collector; HSBC uses embedded public
listings; Standard Chartered uses its public SuccessFactors search feed.
Unsupported pages, including CRISIL, remain explicitly manual until connected.

```powershell
py -3.11 run.py sources --import
py -3.11 run.py sources --scan 1000
```

The **Scan now** button includes up to 1,000 due career sources alongside all
other configured discovery tools and bounded scoring. Source counts open filtered
lists; progress shows completed boards in the header. Six workers limit traffic;
source leases and retry backoff survive restart. Reads are capped per source and
partial coverage is labelled. Manual pages are revisited weekly. This is not a
continuously running cloud service: the laptop/server must remain awake and scans
start from the existing startup/manual triggers. Candidate sources worldwide do
not override Shivani's location preferences or establish visa eligibility.

Catalog attribution: Riley Dorrington / Feashliaa, [Job Board Aggregator](https://github.com/Feashliaa/job-board-aggregator),
company datasets under [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/).
Used for this private, non-commercial job search. Identifiers are normalised,
validated and prioritised locally; raw data stays under ignored `data/shivani`.
Commercial reuse of that dataset needs separate permission from its creator.

## Lower-cost operation

Use **Free discovery** or `py -3.11 run.py scan --discover-only` to collect public
listings without AI tokens or paid scraper runs. **Scan now** collects and rates
at most 12 eligible, unscored jobs. Existing ratings are reused until the profile
or rating version changes. Saved application drafts are reused unless you press
**Regenerate draft (uses AI)**. Token counts are stored in
`data/shivani/llm-usage.db`; this meter does not include earlier calls.

Compact Codex scoring uses short job IDs and bounded descriptions. Fit excludes
unknown compensation and work-life conditions; evidence coverage exposes those
gaps separately. Missing mandatory expertise caps fit at 5/10. No custom model
training was performed: this is prompt and pipeline optimization.

For future coding in Codex, choose Sol with medium reasoning for maintenance;
Luna handles the bounded twelve-job scoring pass. This uses the Codex login;
Apify and Firecrawl retain their separate budgets.
[Official model guidance](https://developers.openai.com/api/docs/guides/model-selection).

## Apify allowance

The existing account limit is $5 per billing cycle. Before a new run, the app
checks account usage and current actor prices; $0.50 remains reserved for storage
and billing lag. Each Naukri run has an API-enforced $0.25 charge ceiling and a
durable local reservation. One focused search per day, plus one low-cost retry
after a confirmed empty result, limits accidental repeated clicks. Identical
search payloads reuse local datasets for seven days. Search terms rotate; city
and India-remote searches alternate when both locations are enabled. Full job
descriptions, salary text, experience and included company ratings are retained.

Unknown run-start outcomes block new purchases. Never delete the budget ledger
to retry: check the Apify console and reconcile the run first. Known running jobs
resume on the next scan. External account spending is included at the next check;
other applications using the key are also subject to the account's existing cap.
The local allowance is conservative, not an accounting invoice.

Live check on September 23: two searches returned 50 full descriptions, costing
$0.152 in actor charges together. Four survived the free initial profile filter;
these are candidates for review, not guaranteed matches. Account usage at the
check was approximately $0.821 including previous activity.
[Apify run charge limits](https://docs.apify.com/api/v2/actors-runs-post).

## Scrapling public-page scraping

Scrapling 0.4.15 now fetches HTML career pages and shortlisted job descriptions.
The existing direct ATS APIs remain in place. This uses the local HTTP fetcher,
without AI calls, paid proxies, browser automation or Apify runs. The source panel
shows the active HTML fetcher. Live HSBC verification returned ten listings.

Successful HTML pages are cached for six hours in `data/shivani/html-cache.db`,
limited to 100 pages. Transfers stop above 2 MB, requests time out after 18 seconds,
automatic retries are disabled, and every redirect is checked against private
addresses. Blocked pages stay visible as failures; JavaScript-only sites may
still need manual review. Missing Scrapling dependencies fall back to Requests.
Set `CAREERLOOP_HTML_FETCHER=requests` before starting to select that fallback.

[Official Scrapling HTTP documentation](https://scrapling.readthedocs.io/en/latest/fetching/static.html).

## Parallel public scraping

Direct ATS and Scrapling collectors share six workers. Up to two generic career pages per batch receive Playwright fallback. Trafilatura cleans identified job descriptions without AI. Source failures stay visible. See [tool comparison and operating limits](docs/OPEN_SOURCE_SCRAPING.md). Run `py -3.11 run.py sources --scan 30` for a free bounded batch.
#   J o b h u n t  
 