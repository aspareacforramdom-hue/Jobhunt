# Open-source scraping options

Research date: 25 September 2026.

| Tool | License | Best use | JavaScript | Cost here | Decision |
|---|---|---|---|---|---|
| [Scrapling](https://github.com/D4Vinci/Scrapling) | BSD-3-Clause | Public HTML; adaptive selectors; concurrent spiders | Optional browser fetchers | Local CPU/network | Existing static transport retained |
| [Playwright Python](https://github.com/microsoft/playwright-python) | Apache-2.0 | Render dynamic career pages | Chromium, Firefox, WebKit | Higher local CPU/RAM | Chromium fallback implemented |
| [Trafilatura](https://github.com/adbar/trafilatura) | Apache-2.0; older pre-1.8 versions differ | Extract readable main text | No browser renderer | Local CPU | Description cleanup implemented |
| [Scrapy](https://github.com/scrapy/scrapy) | BSD-3-Clause | Large structured crawling projects | Requires browser integration | Local infrastructure | Defer additional crawler framework |
| [Crawlee Python](https://github.com/apify/crawlee-python) | Apache-2.0 | Persistent queues; HTTP/browser crawlers | Playwright integration | Local library free; hosting separate | Defer duplicate orchestration layer |

These tools need no LLM. Local execution consumes no Apify credits. Hosting and proxies can cost extra. Performance rankings require representative benchmarks.

## Capabilities and limits

**Scrapling handles static fetching here.** Existing cache lasts six hours. HTML transfers cap at 2MB. Redirect targets receive public-address validation. Failed fetches never become cached jobs. Browser and adaptive capabilities exist upstream. This integration uses HTTP fetching only. [Official fetching documentation](https://scrapling.readthedocs.io/en/latest/fetching/static.html).

**Playwright renders selected dynamic pages.** Six registry workers operate concurrently. Two generic sources receive browser eligibility. Chromium starts after successful static fetching. Existing HTTP failures keep their status. Browser navigation times out after twenty seconds. Structured-data waiting adds three seconds. Images, fonts, and media stay blocked. Non-GET/HEAD requests also stay blocked. Service workers and downloads stay disabled. Each page allows at most 100 routed requests. Final rendered HTML caps at 2MB. This is not a network-transfer cap. Dynamic POST search APIs need dedicated adapters. Browser rendering cannot guarantee job coverage. [Official network documentation](https://playwright.dev/python/docs/network).

**Trafilatura cleans identified job descriptions.** Installed version: 2.0.0. Existing Scrapling version: 0.4.15. Extraction runs locally without model calls. Short descriptions keep BeautifulSoup fallback. Cleanup must retain 80% text length. This heuristic cannot guarantee semantic preservation. Homepages never become job descriptions. Multiple JobPosting records prevent single-job enrichment. Nested graphs now expose structured vacancies. [Official extraction documentation](https://trafilatura.readthedocs.io/en/latest/usage-python.html).

**Scrapy offers mature crawl scheduling.** AutoThrottle adjusts delays using latency. Settings constrain concurrency and download delays. This app already maintains SQLite scheduling. Another scheduler adds integration overhead. Reconsider when maintaining many custom spiders. [Official AutoThrottle documentation](https://docs.scrapy.org/en/latest/topics/autothrottle.html).

**Crawlee supports parallel crawler queues.** HTTP and browser crawlers exchange requests. Separate queues can use separate storage. It can run on local infrastructure. Apify hosting remains a separate choice. Existing scheduling currently covers this workload. Reconsider for persistent multi-stage crawling. [Official parallel crawler example](https://crawlee.dev/python/docs/examples/run-parallel-crawlers).

## Implemented operating policy

1. Direct ATS feeds run first.
2. Scrapling fetches other public HTML.
3. Eligible pages receive Chromium fallback.
4. Explicit JobPosting records become candidates.
5. Existing filters check candidate titles.
6. Database writes remain single-threaded.
7. Failures retain visible source states.
8. Paid scoring remains separately bounded.

Browser eligibility caps at two sources per batch. Other workers continue during rendering. No recursive website crawling occurs. Sources still need relevant adapters. Government PDFs require separate notice extraction. Thousands of source candidates remain unverified. A rendered homepage proves no vacancy.

## Run commands

Free source batch, existing due queue:

```powershell
py -3.11 run.py sources --scan 30
```

Broader free discovery, zero scoring:

```powershell
py -3.11 run.py scan --discover-only
```

Start dashboard without paid boot scanning:

```powershell
py -3.11 run.py serve --no-scan
```

Restore scraper dependencies when needed:

```powershell
py -3.11 -m pip install -r requirements.txt
py -3.11 -m playwright install chromium
```

The source command persists discovered candidates. Dashboard ratings require the normal bounded scan. Existing Apify reservations remain unchanged. Existing scores continue being reused.

## Next useful expansion

Prioritize adapters using actual failure counts. Add employer-specific search feeds where exposed. Add government notice and deadline extraction. Compare browser yield before increasing limits. Retire engines producing no additional evidence.

## Observed validation

Live batch checked 30 due sources. It returned 86 role-relevant listings. These are worldwide discovery candidates. They are not 86 approved matches. Outcomes: 18 live, three partial, two empty, one manual, six errors. Both browser slots actually ran. UBS yielded no structured vacancies. BNP Paribas returned browser HTTP 403. HTTP blocks now retain blocked classification. No paid API or AI ran.

Full suite: 506 passing tests. Four dependency deprecation warnings remain. Dependency consistency check passed. Focused checks cover subsequent status refinements.
