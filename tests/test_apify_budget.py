import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from core import apify_budget as budget
from core.sources_apify import _from_naukri_item


def test_budgeted_naukri_keeps_city_when_remote_is_also_allowed(monkeypatch):
    from core import apify_budget, sources_apify

    captured = []

    def fake_run(payload):
        captured.append(payload)
        return [], "mocked paid run"

    monkeypatch.setattr(apify_budget, "run", fake_run)
    cfg = {"budget_guard": True, "results_wanted": 50,
           "backfill_results_wanted": 50}

    assert sources_apify.from_apify(
        cfg, ["operations manager"],
        ["Bengaluru, India", "Remote India"]) == []
    assert captured[0]["cities"] == ["97"]
    assert "workMode" not in captured[0]


def limits(used=0, cycle="2026-09-11T00:00:00Z"):
    return {"monthlyUsageCycle": {"startAt": cycle, "endAt": "2026-10-10T23:59:59Z"},
            "current": {"monthlyUsageUsd": used}, "limits": {"maxMonthlyUsageUsd": 5}}


def test_external_spend_and_buffer_stop_purchase(tmp_path):
    conn = budget.connect(tmp_path / "ledger.db")
    with pytest.raises(budget.BudgetError, match="Monthly"):
        budget.reserve(conn, "x", .25, limits(4.3))
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_concurrent_reservations_cannot_double_purchase(tmp_path):
    path = tmp_path / "ledger.db"
    budget.connect(path).close()

    def attempt(key):
        conn = budget.connect(path)
        try:
            budget.reserve(conn, key, .25, limits())
            return True
        except budget.BudgetError:
            return False
        finally:
            conn.close()
    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(attempt, ["a", "b"])) == 1


def test_explicit_second_run_stays_bounded(tmp_path):
    conn = budget.connect(tmp_path / "ledger.db")
    try:
        first = budget.reserve(conn, "bengaluru", .25, limits())
        conn.execute("UPDATE runs SET status='SUCCEEDED',charged=.15,items='[]' WHERE id=?", (first,))
        conn.commit()
        second = budget.reserve(conn, "mumbai", .25, limits(), daily_limit=2)
        conn.execute("UPDATE runs SET status='SUCCEEDED',charged=.15,items='[]' WHERE id=?", (second,))
        conn.commit()
        with pytest.raises(budget.BudgetError, match="Daily"):
            budget.reserve(conn, "third", .25, limits(), daily_limit=2)
        with pytest.raises(budget.BudgetError, match="Daily"):
            budget.reserve(conn, "fourth", .25, limits())
    finally:
        conn.close()


def test_one_cheap_empty_second_run_allows_one_targeted_retry(tmp_path):
    conn = budget.connect(tmp_path / "ledger.db")
    try:
        first = budget.reserve(conn, "bengaluru", .25, limits())
        conn.execute("UPDATE runs SET status='SUCCEEDED',charged=.15,items='[1]' WHERE id=?", (first,))
        conn.commit()
        second = budget.reserve(conn, "mumbai-narrow", .25, limits(), daily_limit=2)
        conn.execute("UPDATE runs SET status='SUCCEEDED',charged=.001,items='[]' WHERE id=?", (second,))
        conn.commit()
        third = budget.reserve(conn, "mumbai-broad", .25, limits(), daily_limit=3)
        conn.execute("UPDATE runs SET status='SUCCEEDED',charged=.15,items='[1]' WHERE id=?", (third,))
        conn.commit()
        with pytest.raises(budget.BudgetError, match="Daily"):
            budget.reserve(conn, "fourth", .25, limits(), daily_limit=3)
    finally:
        conn.close()


def test_lagging_remote_usage_cannot_erase_local_charges(tmp_path):
    conn = budget.connect(tmp_path / "ledger.db")
    entry = budget.reserve(conn, "a", .25, limits(4), interval=0)
    conn.execute("UPDATE runs SET status='SUCCEEDED',charged=.25 WHERE id=?", (entry,))
    conn.commit()
    entry = budget.reserve(conn, "b", .25, limits(4), interval=0)
    conn.execute("UPDATE runs SET status='SUCCEEDED',charged=.25 WHERE id=?", (entry,))
    conn.commit()
    with pytest.raises(budget.BudgetError, match="Monthly"):
        budget.reserve(conn, "c", .25, limits(4), interval=0)


def test_account_cycle_resets_budget(tmp_path):
    conn = budget.connect(tmp_path / "ledger.db")
    entry = budget.reserve(conn, "a", .25, limits(4), interval=0)
    conn.execute("UPDATE runs SET status='SUCCEEDED',charged=.25 WHERE id=?", (entry,))
    conn.commit()
    assert budget.reserve(conn, "b", .25, limits(0, "2026-10-11T00:00:00Z"), interval=0)


@pytest.mark.parametrize("value", [0, -.1, .26, float('nan'), float('inf')])
def test_invalid_ceiling_fails_closed(tmp_path, value):
    with pytest.raises(budget.BudgetError):
        budget.reserve(budget.connect(tmp_path / "ledger.db"), "x", value, limits())


def test_ambiguous_start_never_retries(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(budget, "validate_pricing", lambda *a: None)

    def api(path, payload=None, params=None):
        calls.append(path)
        if path == "users/me/limits":
            return limits()
        if payload is not None:
            raise budget.BudgetError("timeout")
        return {}
    monkeypatch.setattr(budget, "api", api)
    for _ in range(2):
        with pytest.raises(budget.BudgetError):
            budget.run({"keyword": "ops"}, tmp_path / "ledger.db")
    assert sum(p.endswith('/runs') for p in calls) == 1


def test_completed_search_reuses_dataset_without_network(monkeypatch, tmp_path):
    path = tmp_path / "ledger.db"
    conn = budget.connect(path)
    payload = {"keyword": "ops"}
    fingerprint = budget.hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    entry = budget.reserve(conn, fingerprint, .25, limits())
    conn.execute("UPDATE runs SET status='SUCCEEDED',items='[{\"title\":\"Ops\"}]',charged=.01 WHERE id=?", (entry,))
    conn.commit()
    monkeypatch.setattr(budget, "api", lambda *a, **k: pytest.fail("cache must avoid network"))
    rows, note = budget.run(payload, path)
    assert rows == [{"title": "Ops"}]
    assert "cached" in note


def test_naukri_preserves_full_evidence_and_included_ratings():
    row = _from_naukri_item({"jobDetails": {
        "title": "Banking Operations Manager", "staticUrl": "https://www.naukri.com/job-listings-example",
        "description": "<p>Manage operations &amp; people.</p>" * 300,
        "experienceText": "4-7 Yrs", "salaryDetail": {"salaryText": "25-30 Lacs PA"}},
        "ambitionBoxDetails": {"companyInfo": {"AggregateRating": "4.2", "ReviewsCount": "123"}}})
    assert len(row['description']) > 4000
    assert "&amp;" not in row['description']
    assert row['company_rating'] == "4.2"
    assert row['salary_text'] == "25-30 Lacs PA"
