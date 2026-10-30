# Using Career Loop

Double-click **Start Career Loop.cmd**. It opens the existing dashboard or starts
one hidden local server. Opening the app does not buy searches or consume Codex
usage. Keep the laptop awake while scanning.

1. Check the search brief before the first scan. Location, pay floor, sales,
   travel and shift preferences determine which roles reach the dashboard.
2. Choose **Free discovery** to gather public leads without paid scrapers or AI.
3. Choose **Scan now** to collect leads and ask the signed-in Codex account to
   judge up to twelve shortlisted jobs. This consumes the account's Codex usage
   allowance. Existing current ratings are reused.
   Every saved job receives a free preliminary résumé and trajectory score;
   listings blocked by Shivani’s constraints remain excluded from applications.
   **Deep-rate next 12 saved jobs** reviews promising roles through Codex
   without buying or running another scrape. Missing ratings do not become zero scores, and an
   incomplete rating cannot discard valid ratings for the rest of its batch.
4. Open each job and follow the employer link. Confirm that it is still open,
   the duties fit, and undisclosed salary or hours meet your requirements.
5. Mark promising jobs Interested. Track applications and interview outcomes
   manually. Nothing applies or sends messages on your behalf.

## Source and cost visibility

The Sources page distinguishes candidate career pages from sources that have
actually returned jobs. A large directory is not a guarantee of suitable jobs.
Each source count opens its filtered list. Awaiting first check means the address
has never been fetched; attention shows blocked requests, errors and unsupported
pages with their explanations and official links. Every normal scan includes up
to 1,000 due boards, using six workers; the header shows completed fetches.
Eligible jobs receive up to twelve new ratings per scan. Unsupported boards need
manual review, while due failures retry automatically with increasing delays.
Firecrawl's panel shows local credit reservations and any unresolved request.
Search results are candidate links until the individual posting is checked.

Apify is limited to the existing $5 billing-cycle ceiling, with a $0.50 buffer,
one normal reservation per day, and cached results reused. Firecrawl reserves at
most eight credits per rolling day and 200 per rolling month. The app records
unknown paid outcomes and pauses further requests rather than repeating them.
Provider balances include usage outside this app and may differ from local totals.

## If something stops

- An expired Codex login or exhausted usage allowance prevents new ratings.
  Open Codex, check its login/usage status, then scan after access is restored.
  Scraped jobs remain saved for the next attempt.
- A blocked source does not mean there are no jobs. Check source notes; the
  other collectors continue. Employer pages may require a manual visit.
- Draft generation is currently unavailable without a separately configured
  API key. Saved drafts and manual application tracking still work.
- If a scan reports an unknown paid request, check the provider activity before
  clearing any reservation. Do not delete the budget ledger to force a retry.
- Keep `data/shivani/` private. The database and profile hold the actual search;
  Git does not back them up. A verified pre-handover database snapshot is in
  `data/shivani/backups/`.

The app is a local research and tracking aid. Scores distinguish evidence from
unknowns; they cannot guarantee interviews, eligibility, or a job offer.
