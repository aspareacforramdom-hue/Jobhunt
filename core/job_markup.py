"""Extract explicit job evidence, including nested Schema.org graphs."""
import json

from bs4 import BeautifulSoup


def postings(html):
    soup = BeautifulSoup(html, "html.parser")
    records = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            pending = [json.loads(script.string or script.get_text())]
        except (ValueError, TypeError):
            continue
        while pending:
            item = pending.pop()
            if isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, dict):
                kinds = item.get("@type", [])
                kinds = [kinds] if isinstance(kinds, str) else kinds
                if isinstance(kinds, list) and "JobPosting" in kinds:
                    records.append(item)
                else:
                    pending.extend(v for v in item.values() if isinstance(v, (dict, list)))
    return records


def clean_description(html):
    """Trafilatura cleans an already identified description, never a homepage."""
    plain = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    try:
        from trafilatura import extract
        extracted = extract(html, include_comments=False, include_tables=True, favor_precision=True)
        # Preserve short or bullet-heavy requirements when extraction loses content.
        if extracted and len(extracted) >= len(plain) * .8:
            return extracted[:18000]
    except ImportError:
        pass
    return plain[:18000]
