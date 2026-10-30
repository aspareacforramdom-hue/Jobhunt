"""Personal Apify runner: durable reservations, account usage and cached datasets.

Reservations survive crashes. Unknown POST outcomes block further spending; they
are never automatically retried. Apify's billing cycle, not calendar months,
defines the allowance. A $0.50 buffer covers storage and usage reporting delay.
"""
import hashlib
import json
import math
import os
import sqlite3
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "data/shivani/apify-budget.db"
ACTOR = "muhammetakkurtt~naukri-job-scraper"
CAP = 5.0
BUFFER = 0.5
RUN_CAP = 0.25
TERMINAL = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}


class BudgetError(RuntimeError):
    pass


def api(path, payload=None, params=None):
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise BudgetError("Apify key missing")
    try:
        response = requests.request(
            "POST" if payload is not None else "GET",
            "https://api.apify.com/v2/" + path,
            headers={"Authorization": "Bearer " + token},
            json=payload, params=params, timeout=(10, 40))
        if not response.ok:
            raise BudgetError(f"Apify HTTP {response.status_code}; no automatic retry")
        result = response.json()
        return result.get("data", result) if isinstance(result, dict) else result
    except (requests.RequestException, ValueError):
        raise BudgetError("Apify connection/response failed; no automatic retry") from None


def connect(path=None):
    path = Path(path or LEDGER)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=45)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cycles (
            cycle TEXT PRIMARY KEY, ends TEXT, baseline REAL, observed REAL,
            checked REAL);
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY, cycle TEXT, fingerprint TEXT, created REAL,
            reserved REAL, charged REAL, run_id TEXT, status TEXT,
            dataset TEXT, items TEXT, note TEXT);
    """)
    return conn


def number(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise BudgetError("Invalid Apify cost; spending stopped") from None
    if not math.isfinite(value) or value < 0:
        raise BudgetError("Invalid Apify cost; spending stopped")
    return value


def reserve(conn, fingerprint, cap, limits, cooldown=7 * 86400, interval=86400,
            daily_limit=1):
    """Serialize reservations across web, CLI and restarted processes."""
    cap = number(cap)
    if not 0 < cap <= RUN_CAP:
        raise BudgetError("Per-run ceiling must be between $0 and $0.25")
    if daily_limit not in (1, 2, 3):
        raise BudgetError("Daily run limit must be one, two, or three")
    cycle = limits["monthlyUsageCycle"]["startAt"]
    ends = limits["monthlyUsageCycle"]["endAt"]
    used = number(limits["current"]["monthlyUsageUsd"])
    allowance = min(CAP, number(limits["limits"]["maxMonthlyUsageUsd"])) - BUFFER
    now = time.time()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT OR IGNORE INTO cycles VALUES (?,?,?,?,?)",
                     (cycle, ends, used, used, now))
        conn.execute("UPDATE cycles SET observed=?, checked=? WHERE cycle=?",
                     (used, now, cycle))
        if conn.execute("SELECT 1 FROM runs WHERE status IN ('reserved','running')").fetchone():
            raise BudgetError("Unresolved Apify run; resume it before another purchase")
        if conn.execute("SELECT 1 FROM runs WHERE fingerprint=? AND created>?",
                        (fingerprint, now - cooldown)).fetchone():
            raise BudgetError("Search already purchased this week; cached results retained")
        today = conn.execute("SELECT * FROM runs WHERE created>? ORDER BY created,id", (now - interval,)).fetchall()
        # One cheap, confirmed empty search may be followed by a broader term.
        empty_retry = (len(today) == 1 and today[0]['status'] == 'SUCCEEDED' and today[0]['items'] == '[]'
                       and today[0]['charged'] is not None and today[0]['charged'] <= .01)
        extra_empty_retry = (daily_limit == 3 and len(today) == 2
                             and today[-1]['status'] == 'SUCCEEDED'
                             and today[-1]['items'] == '[]'
                             and today[-1]['charged'] is not None
                             and today[-1]['charged'] <= .01)
        if (len(today) >= daily_limit
                or (daily_limit == 3 and len(today) == 2 and not extra_empty_retry)) \
                and not (daily_limit == 1 and empty_retry):
            raise BudgetError("Daily Apify allowance used; free sources remain available")
        baseline = conn.execute("SELECT baseline FROM cycles WHERE cycle=?", (cycle,)).fetchone()[0]
        local = conn.execute("SELECT COALESCE(SUM(COALESCE(charged,reserved)),0) FROM runs WHERE cycle=?", (cycle,)).fetchone()[0]
        if max(used, baseline + local) + cap > allowance + 1e-9:
            raise BudgetError("Monthly Apify allowance exhausted; $0.50 buffer protected")
        row = conn.execute("INSERT INTO runs (cycle,fingerprint,created,reserved,status) VALUES (?,?,?,?, 'reserved')",
                           (cycle, fingerprint, now, cap))
        conn.commit()
        return row.lastrowid
    except Exception:
        conn.rollback()
        raise


def validate_pricing(actor, cap):
    # Reject rentals, future/unknown prices or changed events before purchasing.
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    try:
        prices = []
        for price in actor.get("pricingInfos", []):
            started = price.get("startedAt") or price.get("createdAt")
            timestamp = datetime.fromisoformat(started.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError("Pricing timestamp lacks timezone")
            if timestamp <= now:
                prices.append((timestamp, price))
    except (AttributeError, TypeError, ValueError):
        raise BudgetError("Actor pricing metadata invalid; review prices first") from None
    if not prices:
        raise BudgetError("Actor pricing unavailable")
    price = max(prices, key=lambda entry: entry[0])[1]
    if price.get("pricingModel") != "PAY_PER_EVENT" or number(price.get("minimalMaxTotalChargeUsd", 0)) > cap:
        raise BudgetError("Actor pricing no longer fits this budget")
    expected = {"actor-start": .001, "job_item": .0015, "job_item_detailed": .003}
    events = price.get("pricingPerEvent", {}).get("actorChargeEvents", {})
    if set(events) != set(expected):
        raise BudgetError("Actor billing events changed; review prices first")
    for name, maximum in expected.items():
        actual = events[name].get("eventTieredPricingUsd", {}).get("FREE", {}).get("tieredEventPriceUsd")
        if actual is None or number(actual) > maximum:
            raise BudgetError("Actor price increased; review prices first")


def finish(conn, record, wait_seconds=180):
    deadline = time.monotonic() + wait_seconds
    while True:
        run = api("actor-runs/" + record["run_id"], params={"waitForFinish": 10})
        status = run["status"]
        if status in TERMINAL:
            # Keep the entire reservation if actual billing is absent.
            charge = number(run.get("usageTotalUsd", record["reserved"]))
            # Apify may initially report zero after success. Keep the full
            # reservation until a later read has allowed billing to settle.
            settled = time.time() - record['created'] >= 60
            dataset = run.get("defaultDatasetId")
            conn.execute("UPDATE runs SET charged=?,status=?,dataset=? WHERE id=?",
                         (charge if settled else None, status, dataset, record["id"]))
            conn.commit()
            items = api(f"datasets/{dataset}/items", params={"clean": "true", "limit": 100}) if dataset else []
            if not isinstance(items, list):
                raise BudgetError("Unexpected Apify dataset format")
            conn.execute("UPDATE runs SET items=? WHERE id=?", (json.dumps(items), record["id"]))
            conn.commit()
            billing = f"${charge:.4f}" if settled else "billing pending, $0.25 reserved"
            return items, f"Apify {status}: {len(items)} rows, {billing}; dataset cached"
        if time.monotonic() >= deadline:
            raise BudgetError("Apify still running; next scan resumes without repurchasing")


def run(payload, path=None, *, daily_limit=1):
    conn = connect(path)
    try:
        for previous in conn.execute("SELECT * FROM runs WHERE run_id IS NOT NULL AND status NOT IN ('running','reserved') AND (charged IS NULL OR items IS NULL)").fetchall():
            if time.time() - previous['created'] >= 60:
                finish(conn, previous, wait_seconds=0)
        pending = conn.execute("SELECT * FROM runs WHERE status='running' ORDER BY id LIMIT 1").fetchone()
        if pending:
            return finish(conn, pending)
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        cached = conn.execute("SELECT * FROM runs WHERE fingerprint=? AND created>? AND status='SUCCEEDED' AND items IS NOT NULL ORDER BY id DESC LIMIT 1",
                              (fingerprint, time.time() - 7 * 86400)).fetchone()
        if cached:
            return json.loads(cached["items"]), "Apify cached search reused; no new actor charge"
        actor = api("acts/" + ACTOR)
        validate_pricing(actor, RUN_CAP)
        limits = api("users/me/limits")
        entry = reserve(conn, fingerprint, RUN_CAP, limits,
                        daily_limit=daily_limit)
        # Never retry this POST. A lost response leaves a durable reservation.
        started = api("acts/" + ACTOR + "/runs", payload, {
            "maxTotalChargeUsd": RUN_CAP, "timeout": 180, "memory": 128,
            "build": "0.0.56"})
        conn.execute("UPDATE runs SET run_id=?,status='running' WHERE id=?", (started["id"], entry))
        conn.commit()
        return finish(conn, conn.execute("SELECT * FROM runs WHERE id=?", (entry,)).fetchone())
    finally:
        conn.close()


def snapshot(path=None):
    conn = connect(path)
    try:
        cycle = conn.execute("SELECT * FROM cycles ORDER BY checked DESC LIMIT 1").fetchone()
        last = conn.execute(
            "SELECT status,charged,reserved,created, "
            "CASE WHEN items IS NULL THEN NULL ELSE json_array_length(items) END AS item_count "
            "FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if not cycle:
            return {"used": None, "remaining": None, "last": None}
        local = conn.execute("SELECT COALESCE(SUM(COALESCE(charged,reserved)),0) FROM runs WHERE cycle=?", (cycle["cycle"],)).fetchone()[0]
        used = max(cycle["observed"], cycle["baseline"] + local)
        return {"used": used, "remaining": max(0, CAP - BUFFER - used),
                "ends": cycle["ends"][:10], "last": dict(last) if last else None}
    finally:
        conn.close()
