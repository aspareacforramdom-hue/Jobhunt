"""Free, bounded Scrapling HTML transport with a small persistent page cache."""
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

CACHE = Path(__file__).resolve().parents[1] / 'data/shivani/html-cache.db'
MAX_BYTES = 2_000_000
TTL = 6 * 3600


def available():
    if os.environ.get('CAREERLOOP_HTML_FETCHER', 'scrapling') == 'requests':
        return False
    try:
        from scrapling.fetchers import Fetcher  # noqa: F401
        return True
    except ImportError:
        return False


def _cache(url, body=None):
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(CACHE, timeout=5) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS pages (url TEXT PRIMARY KEY, fetched REAL, body TEXT)')
            if body is None:
                row = conn.execute('SELECT body FROM pages WHERE url=? AND fetched>?', (url, time.time()-TTL)).fetchone()
                return row[0] if row else None
            conn.execute('INSERT OR REPLACE INTO pages VALUES (?,?,?)', (url, time.time(), body))
            conn.execute('DELETE FROM pages WHERE url NOT IN (SELECT url FROM pages ORDER BY fetched DESC LIMIT 100)')
    except (sqlite3.Error, OSError):
        # Cache failure must not lose a successfully fetched page.
        return None
    return None


def _get(url):
    from curl_cffi import CurlError
    from scrapling.fetchers import Fetcher
    chunks, size = [], 0

    def receive(chunk):
        nonlocal size
        size += len(chunk)
        if size > MAX_BYTES:
            return 0  # Abort the transfer before buffering a large body.
        chunks.append(chunk)
        return len(chunk)

    try:
        response = Fetcher.get(url, timeout=18, retries=0, follow_redirects=False,
                               content_callback=receive, verify=True)
    except CurlError:
        if size > MAX_BYTES:
            raise ValueError('Page too large') from None
        raise requests.RequestException('Scrapling request failed') from None
    if size > MAX_BYTES:
        raise ValueError('Page too large')
    headers = {str(k).lower(): v for k, v in response.headers.items()}
    return response.status, headers, b''.join(chunks).decode('utf-8', 'replace')


def fetch(url, validate):
    """Validate every redirect; never escalate to paid scrapers or browsers."""
    original = validate(url)
    cached = _cache(original)
    if cached is not None:
        return cached
    for _ in range(4):
        status, headers, body = _get(validate(url))
        if status in (301, 302, 303, 307, 308):
            if not headers.get('location'):
                raise ValueError('Redirect missing location')
            url = urljoin(url, headers['location'])
            continue
        if status >= 400:
            response = requests.Response()
            response.status_code = status
            raise requests.HTTPError(f'Scrapling HTTP {status}', response=response)
        if status != 200:
            raise ValueError(f'Unexpected page status {status}')
        content_type = headers.get('content-type', '').lower()
        if content_type and not any(t in content_type for t in ('text/html', 'application/xhtml')):
            raise ValueError('Expected HTML page')
        _cache(original, body)
        return body
    raise ValueError('Too many redirects')
