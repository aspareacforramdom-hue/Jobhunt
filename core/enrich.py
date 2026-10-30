"""Bounded public-page enrichment. Failed fetches remain explicitly unknown."""

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from core import db
from core.job_markup import clean_description, postings


def public_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Public HTTP URL required")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("Nonstandard port refused")
    default_port = 443 if parsed.scheme == "https" else 80
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or default_port)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Private address refused")
    return url


def fetch(url):
    from core import scrapling_source
    if scrapling_source.available():
        return scrapling_source.fetch(url, public_url)
    for _ in range(4):
        with requests.get(public_url(url), timeout=(8, 18), allow_redirects=False,
                          stream=True, headers={"User-Agent": "Mozilla/5.0"}) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers["Location"])
                continue
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(32768):
                size += len(chunk)
                if size > 2_000_000:
                    raise ValueError("Page too large")
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8", "replace")
    raise ValueError("Too many redirects")


def description(html):
    soup = BeautifulSoup(html, "html.parser")
    records = postings(html)
    # Multi-job search pages must never enrich a single job with another role.
    if len(records) == 1 and isinstance(records[0].get("description"), str):
        return clean_description(records[0]["description"])
    if len(records) > 1:
        return ""
    node = soup.select_one('.show-more-less-html__markup, .description__text, #job-description, .job-description, [itemprop="description"]')
    return clean_description(str(node)) if node else ""



def enrich(jobs):
    def one(job):
        if len(job.get("description") or "") >= 350:
            return
        try:
            text = description(fetch(job["url"]))
            if len(text) >= 350:
                job.update(description=text, needs_enrichment=False, verified_at=db.now(),
                           verification="Description fetched from listing")
        except (requests.RequestException, ValueError, OSError):
            return
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(one, jobs))
