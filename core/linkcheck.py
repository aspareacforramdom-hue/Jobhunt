"""
THE ONLY THING THAT CLOSES A JOB — asking the page.

The first design inferred closure for free. A Greenhouse or Lever board
answers with the company's complete current list, so anything missing
from today's answer had surely been taken down: no probing, no extra
request, exact. It closed 278 of 963 listings on the live corpus.

Then 60 of those were probed. 42 still answered HTTP 200 with a working
apply button — a 70% false positive rate, quietly deleting real
opportunities from somebody's board. The cause was not pagination; the
Lever API returns the same 19 rows for mindtickle at any limit.
Employers UNLIST a posting while leaving the direct link live and
applicable, and a board read cannot tell that apart from a deletion.

So absence became a suspicion and this became the verdict. The board
read still earns its keep: it decides what gets asked about FIRST,
which is worth a great deal when the pool is 1,600 rows and a sweep is
a few hundred.

Two rules hold it together:

  Only ask about listings someone actually has on a board. A dead link
  costs nobody anything until it is in front of them.

  Only close on a definite answer. 404 and 410 mean gone, and so does a
  page that says it in words. A timeout, a connection reset, a 403 from
  a bot-check and a 500 all mean "could not find out today". Closing on
  those would empty a board the first time someone's wifi dropped.
"""

import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from core import db

WORKERS = 8
TIMEOUT = 15

# Some boards answer 200 and say it in the page instead. Taken from real
# responses, not invented.
GONE_PHRASES = (
    "no longer accepting applications",
    "this job is no longer available",
    "position has been filled",
    "posting is closed",
    "job posting is no longer",
    "this job has expired",
    "no longer accepting",
)


def _verdict(url):
    """(is_gone, why). is_gone is only ever True on a definite answer."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; careerloop/1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read(60_000).decode("utf-8", "replace").lower()
            status = r.status
        for phrase in GONE_PHRASES:
            if phrase in body:
                return True, f"page says: {phrase}"
        return False, f"live {status}"
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return True, f"HTTP {e.code}"
        # 403 is usually a bot check, 429 is rate limiting, 5xx is theirs.
        # None of them is evidence about the job.
        return False, f"HTTP {e.code} (not evidence)"
    except Exception as e:                              # noqa: BLE001
        return False, f"{type(e).__name__} (not evidence)"


def candidates(conn, limit=200):
    """Links worth asking about, most suspicious first.

    Ordering is the whole value the free board read still provides:
    something that fell off its company's board today is far likelier to
    be gone than something that merely looks old, so it goes to the
    front of a queue that is always shorter than the pool.
    """
    return [r["url"] for r in conn.execute(
        "SELECT DISTINCT j.url, j.missing_since, j.found_at, j.link_checked_at FROM jobs j "
        "JOIN user_jobs uj ON uj.job_id = j.id "
        "WHERE j.closed_at IS NULL AND uj.status = 'new' AND j.url IS NOT NULL "
        "ORDER BY j.link_checked_at ASC, (j.missing_since IS NULL), j.found_at ASC, j.url "
        "LIMIT ?", (limit,))]


def sweep(conn, limit=200, verbose=True):
    """Ask about the most suspicious open board links; close what is proven
    gone. Returns (checked, closed).

    The limit means a long backlog drains over several runs rather than
    hammering anyone in one.
    """
    urls = candidates(conn, limit)
    if not urls:
        return 0, 0

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(lambda u: (u, *_verdict(u)), urls))

    gone = [u for u, is_gone, _ in results if is_gone]
    closed = db.close_urls(conn, gone)
    # Record attempts, including timeouts: otherwise the same live or
    # unreachable prefix monopolises every bounded sweep indefinitely.
    stamp = db.now()
    conn.executemany("UPDATE jobs SET link_checked_at=? WHERE url=?",
                     [(stamp, u) for u in urls])
    conn.commit()

    if verbose:
        print(f"  [linkcheck] {len(urls)} checked, {closed} closed")
        for url, is_gone, why in results:
            if is_gone:
                print(f"    closed  {why:34} {url[:60]}")
    return len(urls), closed
