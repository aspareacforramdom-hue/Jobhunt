"""
Migration of the single-user JSON files into SQLite.

Runs once, on the first boot after the upgrade. It is idempotent — the
upserts mean running it twice changes nothing — and it never deletes the
JSON. If the import turns out wrong the originals are still on disk.

Everything it imports is attached to ONE user id, because the JSON era
had exactly one user and there is no way to infer otherwise.
"""

import json
import os

from core import db, intake

FILES = ["profile.json", "latest.json", "outcomes.json", "seen.json"]


def _read(data_dir, name):
    path = os.path.join(data_dir, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError) as e:
        print(f"  [migrate] {name} unreadable, skipped: {e}")
        return None


def pending(data_dir="data"):
    """Is there anything from the JSON era worth importing?"""
    return [f for f in FILES if os.path.exists(os.path.join(data_dir, f))]


def migrate_json(conn, user_id, data_dir="data", verbose=True):
    """Import data/*.json into the database, owned by `user_id`."""
    report = {"profile": False, "jobs": 0, "user_jobs": 0,
              "applications": 0, "outcomes": 0, "skipped": []}

    profile = _read(data_dir, "profile.json")
    if profile:
        # Old profiles carry the retired fixed dimensions. finalize_profile
        # re-normalises them: undefined ones are dropped, the rest rescaled.
        db.save_profile(conn, user_id, intake.finalize_profile(profile))
        report["profile"] = True

    by_url = {}
    for job in _read(data_dir, "latest.json") or []:
        if not isinstance(job, dict) or not job.get("url"):
            continue
        job_id = db.upsert_job(conn, job)
        if not job_id:
            continue
        by_url[job["url"]] = job_id
        report["jobs"] += 1

        if job.get("score") is not None:
            db.upsert_user_job(conn, user_id, job_id, {
                "score": job.get("score"),
                "verdict": job.get("verdict"),
                "angle": job.get("angle"),
                "risk": None,
                "stretch": job.get("stretch"),
                # The JSON era had no per-dimension breakdown. Leaving it
                # empty is honest; inventing one would be worse.
                "score_breakdown": job.get("score_breakdown") or {},
            })
            report["user_jobs"] += 1

        if job.get("tailored") or job.get("outreach"):
            db.save_application(conn, user_id, job_id,
                                job.get("tailored"), job.get("outreach"))
            report["applications"] += 1

    for entry in _read(data_dir, "outcomes.json") or []:
        if not isinstance(entry, dict):
            continue
        job_id = by_url.get(entry.get("url"))
        if not job_id:
            # An outcome logged against a listing that is no longer in
            # latest.json. Keep the record by re-creating a stub job.
            if not entry.get("url"):
                continue
            job_id = db.upsert_job(conn, {"url": entry["url"],
                                          "source": "imported"})
            by_url[entry["url"]] = job_id
            report["jobs"] += 1

        status = entry.get("status")
        db.record_outcome(conn, user_id, job_id, status, entry.get("note"))
        if status in db.STATUSES:
            conn.execute(
                "INSERT INTO user_jobs (user_id, job_id, status, updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(user_id, job_id) DO UPDATE SET "
                "status=excluded.status, updated_at=excluded.updated_at",
                (user_id, job_id, status, db.now()))
        report["outcomes"] += 1
    conn.commit()

    seen = _read(data_dir, "seen.json")
    if seen:
        # seen.json stores company::title hashes, not URLs, so there is
        # nothing to attach them to. Dedup now runs off jobs.url instead.
        report["skipped"].append(
            f"seen.json ({len(seen)} keys) — hashes only, no URLs to import; "
            f"dedup now runs off the jobs table")

    if verbose:
        print(f"→ migrated: profile={report['profile']} "
              f"jobs={report['jobs']} scored={report['user_jobs']} "
              f"applications={report['applications']} "
              f"outcomes={report['outcomes']}")
        for s in report["skipped"]:
            print(f"  skipped: {s}")

    return report
