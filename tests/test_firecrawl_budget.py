"""Paid-call safety: persistent cache, allowance, and ambiguous outcomes."""
import json
import urllib.error

import pytest

from core import firecrawl, firecrawl_budget as budget


def test_success_is_cached_across_connections():
    payload = {"query": "operations job", "limit": 5}
    entry, cached = budget.reserve('/search', payload)
    assert cached is None
    body = {"success": True, "data": {"web": []}}
    budget.finish(entry, body)
    assert budget.reserve('/search', payload) == (None, body)


def test_unknown_request_blocks_new_spending():
    budget.reserve('/search', {"query": "first", "limit": 5})
    with pytest.raises(budget.BudgetError, match="unresolved"):
        budget.reserve('/search', {"query": "second", "limit": 5})


def test_daily_allowance_survives_connections():
    for index in range(4):
        entry, _ = budget.reserve('/search', {"query": str(index), "limit": 5})
        budget.finish(entry, {"success": True})
    with pytest.raises(budget.BudgetError, match="allowance"):
        budget.reserve('/search', {"query": "fifth", "limit": 5})


def test_failed_request_is_not_retried():
    payload = {"query": "failed", "limit": 5}
    entry, _ = budget.reserve('/search', payload)
    budget.finish(entry)
    with pytest.raises(budget.BudgetError, match="retry"):
        budget.reserve('/search', payload)


def test_unknown_http_outcome_is_never_reposted(monkeypatch):
    monkeypatch.setenv('FIRECRAWL_API_KEY', 'test-key')
    calls = []
    def fail(*a, **kw):
        calls.append(1)
        raise urllib.error.URLError('timeout')
    monkeypatch.setattr(firecrawl.urllib.request, 'urlopen', fail)
    assert firecrawl._post('/search', {"query": "job", "limit": 5}) is None
    assert firecrawl._post('/search', {"query": "job", "limit": 5}) is None
    assert len(calls) == 1


def test_disabled_enrichment_does_not_spend(monkeypatch):
    monkeypatch.setenv('FIRECRAWL_API_KEY', 'test-key')
    monkeypatch.setattr(firecrawl, '_post', lambda *a: pytest.fail('Paid call'))
    rows = [{"url": "https://example.com/job", "needs_enrichment": True}]
    assert firecrawl.enrich_descriptions(rows, {"enabled": False}) == rows


def test_search_directories_are_not_saved_as_jobs(monkeypatch):
    monkeypatch.setenv('FIRECRAWL_API_KEY', 'test-key')
    monkeypatch.setattr(firecrawl, '_post', lambda *a: {"success": True, "data": {"web": [
        {"title": "1,000+ Operations Manager jobs in Bengaluru", "url": "https://linkedin.com/jobs/search"},
        {"title": "Operations Manager - Bank", "url": "https://bank.example/job/123"}]}})
    rows = firecrawl.search_jobs({}, ['operations manager'], ['Bengaluru'])
    assert len(rows) == 1
    assert rows[0]['location'] is None
    assert rows[0]['needs_enrichment']


def test_failed_enrichment_stays_unverified(monkeypatch):
    monkeypatch.setenv('FIRECRAWL_API_KEY', 'test-key')
    monkeypatch.setattr(firecrawl, '_post', lambda *a: {"success": True, "data": {"markdown": "Access denied"}})
    rows = [{"url": "https://bank.example/job/1", "needs_enrichment": True}]
    firecrawl.enrich_descriptions(rows, {})
    assert rows[0]['needs_enrichment']


def test_plain_directory_title_is_not_a_vacancy():
    assert firecrawl._collection_page('Execution Manager Jobs - Naukri.com', 'https://www.naukri.com/execution-manager-jobs')
