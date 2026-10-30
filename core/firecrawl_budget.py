"""Conservative local credit reservations. Never repeat an ambiguous paid POST."""
import hashlib
import json
import math
import sqlite3
import time
from pathlib import Path

LEDGER = Path(__file__).resolve().parents[1] / "data/shivani/firecrawl-budget.db"
DAILY_CREDITS = 8
MONTHLY_CREDITS = 200  # Rolling 30 days, independent of the provider billing cycle.
CACHE_SECONDS = 7 * 86400


class BudgetError(RuntimeError):
    pass


def connect():
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LEDGER, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY, fingerprint TEXT, created REAL,
        credits INTEGER, status TEXT, response TEXT)""")
    return conn


def cost(path, payload):
    if path == "/search" and not payload.get("scrapeOptions"):
        limit = int(payload.get("limit", 10))
        if not 1 <= limit <= 10 or payload.get("enterprise"):
            raise BudgetError("Firecrawl search options exceed the credit policy")
        return 2 * math.ceil(limit / 10)
    if path == "/scrape" and payload.get("proxy") == "basic" and payload.get("parsers") == []:
        formats = payload.get("formats", [])
        if any((f.get("type") if isinstance(f, dict) else f) not in {"markdown", "json"} for f in formats):
            raise BudgetError("Firecrawl format is outside the credit policy")
        return 5 if any(isinstance(f, dict) and f.get("type") == "json" for f in formats) else 1
    raise BudgetError("Firecrawl request is outside the credit policy")


def reserve(path, payload):
    credits = cost(path, payload)
    fingerprint = hashlib.sha256(json.dumps([path, payload], sort_keys=True).encode()).hexdigest()
    now = time.time()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ttl = 86400 if path == "/search" else CACHE_SECONDS
        cached = conn.execute("SELECT response FROM requests WHERE fingerprint=? AND status='succeeded' AND created>? ORDER BY id DESC LIMIT 1", (fingerprint, now - ttl)).fetchone()
        if cached:
            conn.commit()
            return None, json.loads(cached[0])
        if conn.execute("SELECT 1 FROM requests WHERE status='reserved'").fetchone():
            raise BudgetError("Firecrawl outcome unresolved; paid requests paused for review")
        if conn.execute("SELECT 1 FROM requests WHERE fingerprint=? AND status!='succeeded'", (fingerprint,)).fetchone():
            raise BudgetError("Firecrawl failed request retained; no automatic paid retry")
        for seconds, cap in ((86400, DAILY_CREDITS), (30 * 86400, MONTHLY_CREDITS)):
            used = conn.execute("SELECT COALESCE(SUM(credits),0) FROM requests WHERE created>?", (now - seconds,)).fetchone()[0]
            if used + credits > cap:
                raise BudgetError("Firecrawl credit allowance reached; free sources continue")
        row = conn.execute("INSERT INTO requests(fingerprint,created,credits,status) VALUES(?,?,?,'reserved')", (fingerprint, now, credits))
        conn.commit()
        return row.lastrowid, None
    finally:
        conn.close()


def finish(entry, body=None):
    conn = connect()
    try:
        conn.execute("UPDATE requests SET status=?,response=? WHERE id=?", ("succeeded" if body else "failed", json.dumps(body) if body else None, entry))
        conn.commit()
    finally:
        conn.close()


def snapshot():
    """Counts only; never return stored search pages or credential material."""
    conn = connect()
    try:
        now = time.time()
        used = conn.execute("SELECT COALESCE(SUM(credits),0) FROM requests WHERE created>?", (now - 86400,)).fetchone()[0]
        monthly = conn.execute("SELECT COALESCE(SUM(credits),0) FROM requests WHERE created>?", (now - 30 * 86400,)).fetchone()[0]
        unresolved = conn.execute("SELECT COUNT(*) FROM requests WHERE status='reserved'").fetchone()[0]
        last = conn.execute("SELECT created,status,credits FROM requests ORDER BY id DESC LIMIT 1").fetchone()
        return {"daily_reserved": used, "daily_limit": DAILY_CREDITS,
                "monthly_reserved": monthly, "monthly_limit": MONTHLY_CREDITS,
                "unresolved": unresolved, "last": dict(last) if last else None}
    finally:
        conn.close()
