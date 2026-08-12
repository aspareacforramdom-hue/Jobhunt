"""
TIER 2.5 — Firecrawl Cloud.

The tests that matter here are the cost ones. Search is billed per
result set and scraping per page, so the whole design rests on
searching cheaply and scraping only what survives the prefilter. That
is invisible in normal use and would regress silently, taking the
credit bill with it.
"""

import json
import re

import pytest

from core import discover, firecrawl


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")


def _cfg(**over):
    cfg = {"enabled": True, "search_enabled": True, "results_per_term": 10,
           "max_terms_per_scan": 6, "max_enrich_per_scan": 25,
           "max_pages_per_scan": 15, "career_pages": []}
    cfg.update(over)
    return cfg


def _search_body(urls):
    return {"success": True,
            "data": {"web": [{"url": u, "title": f"Title {u}",
                              "description": "snippet"} for u in urls]}}


class Recorder:
    """Stands in for the API and records what it was asked to do."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, path, payload):
        self.calls.append((path, payload))
        value = self.responses.get(path)
        return value(payload) if callable(value) else value

    def paths(self):
        return [p for p, _ in self.calls]


# ============================================================
# Cost — the reason this module is shaped the way it is
# ============================================================
def test_search_does_not_scrape_its_results(monkeypatch):
    """scrapeOptions on /search would bill a page credit for every
    result, most of which the prefilter is about to discard."""
    rec = Recorder({"/search": _search_body(["https://a/1"])})
    monkeypatch.setattr(firecrawl, "_post", rec)

    firecrawl.search_jobs(_cfg(), ["Brand Manager"], ["Mumbai"])

    assert rec.paths() == ["/search"], "search triggered a paid scrape"
    _, payload = rec.calls[0]
    assert "scrapeOptions" not in payload


def test_enrichment_happens_only_after_the_prefilter(monkeypatch):
    """The scan must not buy full text for listings it then discards."""
    rec = Recorder({"/scrape": {"success": True,
                                "data": {"markdown": "full text"}}})
    monkeypatch.setattr(firecrawl, "_post", rec)

    survivors = [{"url": "https://a/1", "needs_enrichment": True,
                  "description": "snippet"}]
    firecrawl.enrich_descriptions(survivors, _cfg())

    assert rec.paths() == ["/scrape"]
    assert survivors[0]["description"] == "full text"
    assert survivors[0]["needs_enrichment"] is False


def test_enrichment_respects_its_budget(monkeypatch):
    rec = Recorder({"/scrape": {"success": True,
                                "data": {"markdown": "x" * 50}}})
    monkeypatch.setattr(firecrawl, "_post", rec)

    jobs = [{"url": f"https://a/{i}", "needs_enrichment": True,
             "description": ""} for i in range(40)]
    firecrawl.enrich_descriptions(jobs, _cfg(max_enrich_per_scan=5))

    assert len(rec.calls) == 5, "blew through the credit budget"


def test_enrichment_skips_listings_that_already_have_text(monkeypatch):
    rec = Recorder({"/scrape": {"success": True, "data": {"markdown": "x"}}})
    monkeypatch.setattr(firecrawl, "_post", rec)

    firecrawl.enrich_descriptions(
        [{"url": "https://a/1", "description": "already full"}], _cfg())

    assert rec.calls == [], "paid to re-fetch a description it had"


def test_search_is_capped_by_term_count(monkeypatch):
    rec = Recorder({"/search": _search_body([])})
    monkeypatch.setattr(firecrawl, "_post", rec)

    firecrawl.search_jobs(_cfg(max_terms_per_scan=2),
                          ["a", "b", "c", "d", "e"], ["Mumbai"])

    assert len(rec.calls) == 2


def test_career_page_scraping_is_capped(monkeypatch):
    rec = Recorder({"/scrape": {"success": True, "data": {"json": {"jobs": []}}}})
    monkeypatch.setattr(firecrawl, "_post", rec)

    firecrawl.scrape_career_pages(
        _cfg(max_pages_per_scan=3,
             career_pages=[f"https://c{i}/careers" for i in range(10)]))

    assert len(rec.calls) == 3


# ============================================================
# --dry and a missing key must both cost exactly nothing
# ============================================================
def test_dry_mode_spends_nothing(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(firecrawl, "_post", rec)

    firecrawl.search_jobs(_cfg(), ["Brand Manager"], ["Mumbai"], dry=True)
    firecrawl.scrape_career_pages(
        _cfg(career_pages=["https://c/careers"]), dry=True)
    firecrawl.enrich_descriptions(
        [{"url": "https://a/1", "needs_enrichment": True}], _cfg(), dry=True)

    assert rec.calls == [], "--dry made a billable call"


def test_without_an_api_key_everything_no_ops(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    rec = Recorder()
    monkeypatch.setattr(firecrawl, "_post", rec)

    assert firecrawl.available() is False
    assert firecrawl.search_jobs(_cfg(), ["x"], ["Mumbai"]) == []
    assert firecrawl.scrape_career_pages(_cfg(career_pages=["https://c"])) == []
    assert rec.calls == []


def test_a_missing_key_degrades_the_scan_with_a_note(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setattr(discover, "from_greenhouse", lambda slug: [])
    monkeypatch.setattr(discover, "from_lever", lambda slug: [])

    rows, notes = discover.discover_all({
        "ats_targets": {}, "firecrawl": _cfg(), "apify": {"enabled": False},
        "jobspy": {"enabled": False, "search_terms": ["x"]}})

    assert rows == []
    assert any("FIRECRAWL_API_KEY not set" in n for n in notes)


# ============================================================
# Failures must degrade, never raise
# ============================================================
@pytest.mark.parametrize("body", [
    None,
    {"success": False},
    {"success": True},
    {"success": True, "data": None},
    {"success": True, "data": {"web": None}},
    {"success": True, "data": {"web": ["a string", 42, None]}},
    {"success": True, "data": {"web": [{"no_url": 1}]}},
])
def test_search_survives_any_response_shape(monkeypatch, body):
    monkeypatch.setattr(firecrawl, "_post", lambda p, d: body)
    assert firecrawl.search_jobs(_cfg(), ["x"], ["Mumbai"]) == []


@pytest.mark.parametrize("body", [
    None,
    {"success": True, "data": {}},
    {"success": True, "data": {"json": None}},
    {"success": True, "data": {"json": {"jobs": None}}},
    {"success": True, "data": {"json": {"jobs": ["str", 42, {}, {"x": 1}]}}},
])
def test_career_pages_survive_any_response_shape(monkeypatch, body):
    monkeypatch.setattr(firecrawl, "_post", lambda p, d: body)
    assert firecrawl.scrape_career_pages(
        _cfg(career_pages=["https://c/careers"])) == []


def test_a_transport_error_returns_none_rather_than_raising(monkeypatch):
    import urllib.error

    def _boom(req, timeout=None):
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr(firecrawl.urllib.request, "urlopen", _boom)
    assert firecrawl._post("/search", {}) is None


def test_an_http_error_returns_none_rather_than_raising(monkeypatch):
    import io
    import urllib.error

    def _boom(req, timeout=None):
        raise urllib.error.HTTPError("u", 402, "Payment Required", {},
                                     io.BytesIO(b"out of credits"))

    monkeypatch.setattr(firecrawl.urllib.request, "urlopen", _boom)
    assert firecrawl._post("/scrape", {}) is None


def test_enrichment_survives_a_failing_scrape(monkeypatch):
    monkeypatch.setattr(firecrawl, "_post", lambda p, d: None)
    jobs = [{"url": "https://a/1", "needs_enrichment": True,
             "description": "snippet"}]

    firecrawl.enrich_descriptions(jobs, _cfg())

    assert jobs[0]["description"] == "snippet", "lost the snippet it had"


# ============================================================
# Listing identity and the de-hardcoding rule
# ============================================================
def test_roles_on_one_career_page_do_not_collapse_into_one_row(monkeypatch):
    """url is the shared pool's identity. A page that lists roles without
    per-role links must still yield distinct jobs."""
    monkeypatch.setattr(firecrawl, "_post", lambda p, d: {
        "success": True, "data": {"json": {"jobs": [
            {"title": "Brand Manager"}, {"title": "Brand Designer"}]}}})

    rows = firecrawl.scrape_career_pages(
        _cfg(career_pages=["https://acme.test/careers"]))

    assert len({r["url"] for r in rows}) == 2
    assert all(r["company"] == "acme.test" for r in rows)


def test_the_search_query_comes_from_the_profile_not_from_this_module(
        monkeypatch):
    """No role, industry or seniority word may be baked in here — the
    same constraint the scorer lives under."""
    rec = Recorder({"/search": _search_body([])})
    monkeypatch.setattr(firecrawl, "_post", rec)

    firecrawl.search_jobs(_cfg(), ["Brand Marketing Manager"], ["Mumbai"])

    query = rec.calls[0][1]["query"]
    assert "Brand Marketing Manager" in query
    assert "Mumbai" in query

    source = (firecrawl.EXTRACT_PROMPT + json.dumps(firecrawl.JOB_SCHEMA)).lower()
    for banned in ("product", "engineer", "fintech", "marketing", "ai",
                   "senior", "manager", "banking", "software"):
        # Whole words only — "ai" lives inside "available", and matching
        # on substrings would fail this test for the wrong reason.
        assert not re.search(rf"\b{banned}\b", source), \
            f"'{banned}' is hardcoded in the extractor"


def test_search_results_are_marked_for_later_enrichment(monkeypatch):
    monkeypatch.setattr(firecrawl, "_post",
                        lambda p, d: _search_body(["https://a/1"]))

    rows = firecrawl.search_jobs(_cfg(), ["x"], ["Mumbai"])

    assert rows[0]["needs_enrichment"] is True
    assert rows[0]["source"] == "firecrawl"
    assert len(rows[0]["description"]) <= 600


def test_the_scan_enriches_survivors_only_not_everything_found(
        tmp_path, monkeypatch):
    """
    The integration seam, end to end.

    Discovery finds 3 listings; the prefilter keeps 1. Exactly one page
    credit may be spent. Wiring enrichment into discovery instead would
    spend three and no unit test would notice.
    """
    import yaml

    from core import db, scheduler, session

    path = str(tmp_path / "fc.db")
    conn = db.connect(path)
    uid, _ = session.create_account(conn, "alice", "pw")
    db.save_profile(conn, uid, {
        "target": {"role_family": "Brand Manager", "alt_titles": []},
        "hard_filters": {}, "search_terms": ["Brand Manager"],
        "weights": {"trajectory_fit": 60, "company_quality": 32,
                    "experience_fit": 8},
        "dimension_definitions": {}})

    sources = tmp_path / "sources.yml"
    sources.write_text(yaml.safe_dump({
        "ats_targets": {}, "apify": {"enabled": False},
        "jobspy": {"enabled": False},
        "firecrawl": _cfg(max_enrich_per_scan=25)}))

    found = [
        {"url": "https://a/1", "title": "Brand Manager", "company": "A",
         "description": "snippet", "source": "firecrawl",
         "needs_enrichment": True},
        {"url": "https://a/2", "title": "Warehouse Packer", "company": "B",
         "description": "snippet", "source": "firecrawl",
         "needs_enrichment": True},
        {"url": "https://a/3", "title": "Line Cook", "company": "C",
         "description": "snippet", "source": "firecrawl",
         "needs_enrichment": True},
    ]

    monkeypatch.setattr(scheduler.discover, "discover_all",
                        lambda s, dry=False: ([dict(j) for j in found], []))
    monkeypatch.setattr(scheduler.notify, "send_scan_summary",
                        lambda *a, **kw: True)
    monkeypatch.setattr(scheduler.score, "score_jobs",
                        lambda jobs, profile, **kw: [])

    scraped = []

    def _post(p, payload):
        scraped.append(payload.get("url"))
        return {"success": True, "data": {"markdown": "the full posting"}}

    monkeypatch.setattr(firecrawl, "_post", _post)

    s = scheduler.Scanner(db_path=path, sources_path=str(sources))
    s._scan(conn, "manual")
    conn.close()

    assert scraped == ["https://a/1"], \
        f"paid to scrape {len(scraped)} pages; only 1 survived the prefilter"


def test_duplicate_urls_across_terms_are_returned_once(monkeypatch):
    monkeypatch.setattr(firecrawl, "_post",
                        lambda p, d: _search_body(["https://a/1", "https://a/2"]))

    rows = firecrawl.search_jobs(_cfg(), ["term one", "term two"], ["Mumbai"])

    assert len(rows) == 2, "the same listing was returned once per term"
