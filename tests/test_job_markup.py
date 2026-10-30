import json
from threading import Barrier, Lock

import pytest

from core import constraints, db, enrich, source_registry
from core.job_markup import clean_description, postings


def markup(data):
    return '<script type="application/ld+json">' + json.dumps(data) + '</script>'


def test_nested_graph_and_type_array():
    job = {"@type": ["Thing", "JobPosting"], "title": "Operations manager", "description": "Real requirements"}
    assert postings(markup({"@graph": [{"itemListElement": [{"item": job}]}]})) == [job]
    assert enrich.description(markup({"@graph": [job]})) == "Real requirements"


def test_search_page_never_enriches_one_job_with_another():
    job = {"@type": "JobPosting", "description": "Another role"}
    assert enrich.description(markup([job, job])) == ""
    assert enrich.description('<main>Careers. Find your future with us.</main>') == ""


def test_cleanup_keeps_requirements():
    html = '<div><p>Requirements</p><ul><li>Five years banking experience</li><li>KYC operations expertise</li></ul></div>'
    result = clean_description(html)
    assert 'Five years banking experience' in result
    assert 'KYC operations expertise' in result


def test_browser_only_after_successful_static_fetch(monkeypatch):
    from core import browser_source
    source = {"company": "Bank", "platform": "career_page", "locator": "", "url": "https://example.com/careers", "_browser_fallback": True}
    monkeypatch.setattr(source_registry, 'fetch', lambda url: '<main>Loading</main>')
    monkeypatch.setattr(browser_source, 'fetch', lambda url: markup({"@type": "JobPosting", "title": "Operations manager", "url": "/job/1", "jobLocation": {"address": {"addressLocality": "Bengaluru"}}}))
    jobs, state, _ = source_registry.collect(source)
    assert state == 'partial'
    assert jobs[0]['url'] == 'https://example.com/job/1'
    assert jobs[0]['location'] == 'Bengaluru'
    assert jobs[0]['needs_enrichment'] is True


def test_browser_does_not_turn_missing_job_url_into_homepage(monkeypatch):
    monkeypatch.setattr(source_registry, 'fetch', lambda url: markup({"@type": "JobPosting", "title": "Operations manager"}))
    jobs, state, _ = source_registry.collect({"company": "Bank", "platform": "career_page", "locator": "", "url": "https://example.com"})
    assert jobs == []
    assert state == 'manual'


def test_browser_refuses_private_initial_url():
    from core.browser_source import fetch
    with pytest.raises(ValueError, match='Private'):
        fetch('http://127.0.0.1/')


def test_public_url_uses_scheme_default_port(monkeypatch):
    ports = []

    def addresses(host, port):
        ports.append(port)
        return [(None, None, None, None, ('93.184.216.34', port))]

    monkeypatch.setattr(enrich.socket, 'getaddrinfo', addresses)
    assert enrich.public_url('http://example.com/job')
    assert enrich.public_url('https://example.com/job')
    assert ports == [80, 443]


def test_explicit_foreign_remote_title_is_not_unknown_location():
    profile = {"hard_filters": {"locations": ["Bengaluru, India", "Remote India"]}}
    result = constraints.assess(
        {"title": "Operations Manager - Remote US", "location": "", "description": ""},
        profile)
    assert result["eligible"] is False
    assert "Location outside your search" in result["blockers"]

    contradictory = constraints.assess(
        {"title": "Operations Manager - Remote US", "location": "Bengaluru",
         "description": ""}, profile)
    assert contradictory["eligible"] is False


def test_parallel_batch_caps_browser_fallbacks(tmp_path, monkeypatch):
    monkeypatch.setattr(source_registry, 'CURATED', [])
    conn = db.connect(tmp_path / 'jobs.db')
    for i in range(6):
        conn.execute('INSERT INTO career_sources(company,url,platform,locator,provenance) VALUES (?,?,?,?,?)',
                     (str(i), f'https://example.com/{i}', 'career_page', '', 'test'))
    conn.commit()
    barrier, lock, selected = Barrier(6), Lock(), []
    def collect(source, pages):
        with lock:
            selected.append(source)
        barrier.wait(timeout=10)
        return [], 'manual', 'No job evidence'
    monkeypatch.setattr(source_registry, 'collect', collect)
    source_registry.scan_batch(conn, limit=6)
    assert sum(bool(s.get('_browser_fallback')) for s in selected) == 2
    conn.close()
