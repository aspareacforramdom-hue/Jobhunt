import json

import pytest

from core import db
from core import source_registry as registry


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "sources.db")
    yield connection
    connection.close()


def test_catalog_validation_and_idempotent_import(conn, tmp_path):
    values = {"ashby": ["acme", "../secret", "https://evil.invalid", "acme"],
              "lever": ["example"], "greenhouse": ["bank"],
              "workday": ["tenant|wd5|Careers", "wd1|wd1|noise", "tenant|internal|bad"]}
    for platform, rows in values.items():
        (tmp_path / f"{platform}_companies.json").write_text(json.dumps(rows))
    assert registry.import_catalog(conn, tmp_path) == 4
    assert registry.import_catalog(conn, tmp_path) == 0
    row = conn.execute("SELECT * FROM career_sources WHERE company='tenant'").fetchone()
    assert row["url"] == "https://tenant.wd5.myworkdayjobs.com/Careers"
    assert row["state"] == "queued"


def test_source_batch_preserves_failures_and_advances_queue(conn, monkeypatch):
    monkeypatch.setattr(registry, "CURATED", [])
    for name in ["bank-one", "bank-two", "bank-three"]:
        conn.execute("INSERT INTO career_sources(company,url,platform,locator,provenance) VALUES (?,?,?,?,?)", (name, "https://example.com/" + name, "ashby", name, "test"))
    conn.commit()
    def collect(source, pages):
        if source["company"] == "bank-one":
            raise ValueError("bad board")
        return [{"title": "Operations Manager", "url": source["url"], "company": source["company"], "location": "Bengaluru", "source": "ashby"}], "live", "returned listings"
    monkeypatch.setattr(registry, "collect", collect)
    jobs, _ = registry.scan_batch(conn, limit=2, persist=True)
    assert len(jobs) == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert registry.inventory(conn)["counts"] == {"error": 1, "live": 1, "queued": 1}
    jobs, _ = registry.scan_batch(conn, limit=2)
    assert len(jobs) == 1
    assert jobs[0]["company"] == "bank-three"


def test_greenhouse_extracts_description_and_provenance(monkeypatch):
    monkeypatch.setattr(registry, "_json", lambda url: {"jobs": [{"title":"Client Operations Manager", "absolute_url":"https://example.com/1", "location":{"name":"Bangalore"}, "content":"<p>Real responsibilities</p>"}]})
    source = {"platform":"greenhouse", "locator":"bank", "company":"Bank", "url":"https://example.com"}
    jobs, state, _ = registry.collect(source)
    assert state == "live"
    assert jobs[0]["description"] == "Real responsibilities"
    assert jobs[0]["source_url"] == source["url"]
    assert jobs[0]["needs_enrichment"] is True


def test_rich_description_clears_enrichment_flag(conn):
    thin = {"url": "https://example.com/job", "title": "Operations Manager",
            "company": "Bank", "description": "", "needs_enrichment": True}
    jid = db.upsert_job(conn, thin)
    db.upsert_job(conn, dict(thin, description="Detailed responsibilities. " * 30,
                             needs_enrichment=False))
    assert db.get_job(conn, jid)["needs_enrichment"] == 0
    db.upsert_job(conn, thin)
    assert db.get_job(conn, jid)["needs_enrichment"] == 0


def test_dynamic_career_page_never_claims_verified_jobs(monkeypatch):
    monkeypatch.setattr(registry, "fetch", lambda url: "<html><h1>Careers</h1></html>")
    jobs, state, _ = registry.collect({"platform":"career_page", "locator":"", "url":"https://example.com", "company":"Bank"})
    assert jobs == []
    assert state == "manual"


def test_successfactors_uses_observed_feed(monkeypatch):
    calls = []
    def fetch(url, body):
        calls.append(body)
        return {"jobSearchResult":[{"response":{"id":"123", "unifiedUrlTitle":"Operations", "unifiedStandardTitle":"Operations Manager", "jobLocationShort":["Bengaluru, IND"]}}]}
    monkeypatch.setattr(registry, "_json", fetch)
    jobs, state, _ = registry.collect({"platform":"successfactors", "locator":"", "url":"https://jobs.standardchartered.com/search/", "company":"Standard Chartered"}, pages=2)
    assert [x["pageNumber"] for x in calls] == [0, 1]
    assert state == "partial"
    assert jobs[0]["url"].endswith("/job/Operations/123-en_GB")


def test_goldman_uses_external_source_identifier(monkeypatch):
    monkeypatch.setattr(registry, "_json", lambda url, body: {"data":{"roleSearch":{"items":[{"roleId":"456_GS_PROFESSIONAL", "externalSource":{"sourceId":"456"}, "jobTitle":"Client Operations Associate", "locations":[{"city":"Bengaluru", "country":"India"}], "status":"POSTED"}]}}})
    jobs, state, _ = registry.collect({"platform":"goldman", "locator":"", "url":"https://higher.gs.com/", "company":"Goldman Sachs"}, pages=1)
    assert state == "partial"
    assert jobs[0]["url"] == "https://higher.gs.com/roles/456"


def test_inventory_pagination_and_filters(conn):
    registry.seed_curated(conn)
    result = registry.inventory(conn, "Goldman", page=999)
    assert result["total"] == 1
    assert result["page"] == 1
    assert result["rows"][0]["company"] == "Goldman Sachs"


def test_grouped_count_filters_and_progress(conn, monkeypatch):
    monkeypatch.setattr(registry, "CURATED", [])
    for state in ["queued", "live", "partial", "manual", "blocked", "error", "empty"]:
        conn.execute("INSERT INTO career_sources(company,url,platform,locator,provenance,state) VALUES (?,?,?,?,?,?)",
                     (state, "https://example.com/" + state, "ashby", state, "test", state))
    conn.commit()
    assert registry.inventory(conn, state="attention")["total"] == 3
    assert registry.inventory(conn, state="returned")["total"] == 2
    assert registry.inventory(conn, query="blocked", state="attention")["total"] == 1
    monkeypatch.setattr(registry, "collect", lambda *args: ([], "empty", "No postings"))
    progress = []
    registry.scan_batch(conn, progress=lambda *args: progress.append(args))
    assert progress[0] == (0, 7, 0)
    assert progress[-1] == (7, 7, 0)
    assert [p[0] for p in progress] == list(range(8))
