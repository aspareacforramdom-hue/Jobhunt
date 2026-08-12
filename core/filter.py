"""
LAYER 2 — FILTER
Dedup against everything already seen + drop obvious noise.
Cheap. Runs before any LLM call, so it saves you money.
"""

import json
import os
import re

SEEN_PATH = "data/seen.json"


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _key(job):
    """Same role at same company = same job, even across portals."""
    return f"{_norm(job.get('company'))}::{_norm(job.get('title'))}"


def load_seen():
    if os.path.exists(SEEN_PATH):
        with open(SEEN_PATH) as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    os.makedirs("data", exist_ok=True)
    with open(SEEN_PATH, "w") as f:
        json.dump(sorted(seen), f, indent=1)


def filter_jobs(jobs, target_cfg, seen=None):
    """
    `seen` defaults to the JSON-era file for the CLI. The portal passes
    an explicit set() instead: it dedups against the shared jobs table
    by URL, and consulting the old file as well would silently block
    every listing for anyone who had run the CLI before.
    """
    seen = load_seen() if seen is None else set(seen)
    excl = [e.lower() for e in target_cfg.get("exclude_keywords", [])]

    fresh, batch_keys = [], set()

    for j in jobs:
        k = _key(j)
        if k in seen or k in batch_keys:
            continue

        blob = f"{j.get('title','')} {j.get('description','')}".lower()
        if any(e in blob for e in excl):
            continue

        batch_keys.add(k)
        fresh.append(j)

    print(f"→ {len(fresh)} new after dedup + hard filters")
    return fresh, seen | batch_keys
