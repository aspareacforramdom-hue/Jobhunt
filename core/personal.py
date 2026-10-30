"""Private, local-only workspace for one candidate. No account setup."""

import hashlib
import json
import os
from pathlib import Path
import yaml

from core import db

PROFILE_PATH = Path(os.environ.get("CAREERLOOP_PROFILE", "data/shivani/profile.json"))


SCORING_CONFIG = Path(__file__).resolve().parents[1] / "config" / "personal_sources.yml"


def revision(profile):
    fields = ("target", "leverage_target", "hard_filters", "weights",
              "dimension_definitions", "resume_text", "preferences", "evidence", "candidate")
    payload = {k: profile.get(k) for k in fields}
    payload["rating_version"] = "evidence-fit-v4"
    scoring = profile.get("_scoring")
    if scoring is None:
        try:
            scoring = (yaml.safe_load(SCORING_CONFIG.read_text(encoding="utf-8")) or {}).get("scoring", {})
        except (OSError, yaml.YAMLError):
            scoring = {}
    payload["scorer"] = {key: scoring.get(key) for key in ("provider", "model", "description_chars")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:20]


def ensure_owner(conn):
    users = db.all_users(conn)
    if users:
        if len(users) != 1 or users[0]["username"] != "shivani":
            raise RuntimeError("Choose Shivani's private database, not the previous shared database.")
        return users[0]
    uid = db.create_user(conn, "shivani", "login-disabled", "", is_admin=1)
    return db.get_user(conn, uid)


def bootstrap(conn):
    owner = ensure_owner(conn)
    if not db.load_profile(conn, owner["id"]) and PROFILE_PATH.exists():
        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        db.save_profile(conn, owner["id"], profile)
    return owner


def save(conn, profile):
    owner = ensure_owner(conn)
    db.save_profile(conn, owner["id"], profile)
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = PROFILE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(PROFILE_PATH)
