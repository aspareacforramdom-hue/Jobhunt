"""Small, public-only JavaScript fallback. No paid services or model calls."""
from threading import BoundedSemaphore

import requests

from core.enrich import public_url

SLOTS = BoundedSemaphore(2)
MAX_BYTES = 2_000_000


def fetch(url):
    from playwright.sync_api import Error, sync_playwright

    public_url(url)
    try:
        with SLOTS, sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True)
            try:
                context = browser.new_context(service_workers="block", accept_downloads=False)
                requests_seen = 0

                def route_request(route):
                    nonlocal requests_seen
                    requests_seen += 1
                    request = route.request
                    blocked_resource = request.resource_type in {"image", "media", "font"}
                    if requests_seen > 100 or blocked_resource or request.method not in {"GET", "HEAD"}:
                        return route.abort()
                    try:
                        public_url(request.url)
                    except (ValueError, OSError):
                        return route.abort()
                    return route.continue_()

                context.route("**/*", route_request)
                page = context.new_page()
                response = page.goto(url, wait_until="domcontentloaded", timeout=20000)
                if response is None:
                    raise ValueError("Browser returned no response")
                if response.status >= 400:
                    failed = requests.Response()
                    failed.status_code = response.status
                    raise requests.HTTPError(f"Browser HTTP {response.status}", response=failed)
                try:
                    page.wait_for_selector('script[type="application/ld+json"]', state="attached", timeout=3000)
                except Error:
                    pass
                public_url(page.url)
                html = page.content()
                if len(html.encode("utf-8")) > MAX_BYTES:
                    raise ValueError("Rendered page too large")
                return html
            finally:
                browser.close()
    except Error as error:
        raise ValueError(f"Browser fetch failed: {type(error).__name__}") from None
