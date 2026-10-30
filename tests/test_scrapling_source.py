import pytest
import requests

from core import enrich
from core import scrapling_source as source


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(source, 'CACHE', tmp_path / 'cache.db')


def test_enrichment_routes_through_scrapling(monkeypatch):
    monkeypatch.setattr(source, 'available', lambda: True)
    monkeypatch.setattr(source, 'fetch', lambda url, validate: '<p>Scrapling page</p>')
    assert enrich.fetch('https://example.com/job') == '<p>Scrapling page</p>'


def test_cache_avoids_repeated_requests(monkeypatch):
    calls = []
    monkeypatch.setattr(source, '_get', lambda url: (calls.append(url) or 200, {}, 'job description'))
    assert source.fetch('https://example.com/job', lambda x:x) == 'job description'
    assert source.fetch('https://example.com/job', lambda x:x) == 'job description'
    assert len(calls) == 1


def test_redirect_to_private_host_never_fetched(monkeypatch):
    calls = []
    def get(url):
        calls.append(url)
        return 302, {'location': 'http://127.0.0.1/secret'}, ''
    def validate(url):
        if '127.0.0.1' in url:
            raise ValueError('Private address refused')
        return url
    monkeypatch.setattr(source, '_get', get)
    with pytest.raises(ValueError, match='Private'):
        source.fetch('https://example.com/job', validate)
    assert calls == ['https://example.com/job']


def test_blocked_page_is_not_cached_as_job_data(monkeypatch):
    monkeypatch.setattr(source, '_get', lambda url:(403, {}, 'blocked'))
    with pytest.raises(requests.HTTPError) as error:
        source.fetch('https://example.com/job', lambda x:x)
    assert error.value.response.status_code == 403
    assert source._cache('https://example.com/job') is None


def test_transfer_limit_stops_buffering(monkeypatch):
    from types import SimpleNamespace

    from scrapling.fetchers import Fetcher
    def get(url, **kwargs):
        assert kwargs['retries'] == 0
        assert kwargs['follow_redirects'] is False
        assert kwargs['content_callback'](b'x' * (source.MAX_BYTES + 1)) == 0
        return SimpleNamespace(status=200, headers={})
    monkeypatch.setattr(Fetcher, 'get', get)
    with pytest.raises(ValueError, match='too large'):
        source._get('https://example.com/job')


def test_cache_expires_and_stays_bounded(monkeypatch):
    source._cache('https://example.com/old', 'old')
    monkeypatch.setattr(source.time, 'time', lambda: 9_999_999_999)
    assert source._cache('https://example.com/old') is None
    for i in range(105):
        source._cache(f'https://example.com/{i}', 'job')
    with source.sqlite3.connect(source.CACHE) as conn:
        assert conn.execute('SELECT COUNT(*) FROM pages').fetchone()[0] == 100
