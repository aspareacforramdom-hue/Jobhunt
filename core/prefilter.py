"""
STAGE 4 — PREFILTER
Free, no LLM. Kills the obvious noise before you pay to score it.

On the 497-listing test run, this is the difference between
~62 API calls and ~16. Cost control is a design feature, not
an afterthought.

Everything it blocks comes from the user's own profile. There is no
global blocklist: the old one killed anything containing "sales",
which is correct for one person and guts the entire search of anyone
moving into a sales-adjacent field.

It also no longer blocks senior titles. "Head of", "principal" and
"director" were being dropped as too senior — those are precisely the
stretch roles this tool is built to surface. A role is only dropped on
seniority if the user's own profile states a ceiling.
"""

import re


def _tokens(phrases):
    """Words worth matching a title against. Short words match noise."""
    out = set()
    for p in phrases or []:
        out |= {w for w in re.split(r"\W+", (p or "").lower()) if len(w) > 3}
    return out


def _wanted_tokens(profile):
    """
    The words this person's target is made of. Computed once per scan,
    not once per listing — it is identical for all of them.
    """
    target = profile.get("target", {}) or {}
    # search_terms come from calibrate — what the market actually calls
    # this role, which is often not what the user called it.
    return _tokens([target.get("role_family", "")]
                   + list(target.get("alt_titles", []) or [])
                   + list(profile.get("search_terms", []) or []))


def _title_signal(title, tokens):
    """Does the title look anything like what this person wants?"""
    if not tokens:
        return 0.0
    t = (title or "").lower()
    hits = sum(1 for tok in tokens if tok in t)
    return min(hits / 2.0, 1.0)


def _location_ok(loc, allowed):
    if not allowed:
        return True
    l = (loc or "").lower()
    if not l:
        return True
    if any("anywhere" in a.lower() or "remote" in a.lower() for a in allowed):
        return True
    return any(a.split(",")[0].strip().lower() in l for a in allowed)


def _years_required(text):
    """Largest 'N+ years' figure a listing asks for, or None."""
    found = [int(m) for m in
             re.findall(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?"
                        r"(?:years|yrs)\b", text or "", re.I)]
    return max(found) if found else None


def prefilter(jobs, profile, keep_ratio=0.25, min_keep=20):
    hf = profile.get("hard_filters", {}) or {}
    excl = [e.lower() for e in hf.get("exclude_keywords", []) if e]
    excl_titles = [t.lower() for t in hf.get("exclude_titles", []) if t]
    locs = hf.get("locations", [])

    # Only applied if this user's own profile sets a ceiling. Absent it,
    # nothing is dropped for being too senior.
    ceiling = hf.get("max_years_required")
    try:
        ceiling = int(ceiling) if ceiling is not None else None
    except (TypeError, ValueError):
        ceiling = None

    tokens = _wanted_tokens(profile)

    survivors = []
    for j in jobs:
        title = (j.get("title") or "")
        low_title = title.lower()
        blob = f"{title} {j.get('description','')}".lower()

        if any(e in blob for e in excl):
            continue
        if any(t in low_title for t in excl_titles):
            continue
        if not _location_ok(j.get("location"), locs):
            continue
        if ceiling is not None:
            need = _years_required(blob)
            if need is not None and need > ceiling:
                continue

        sig = _title_signal(title, tokens)
        if sig == 0:
            continue

        j["_prefilter_signal"] = sig
        survivors.append(j)

    survivors.sort(key=lambda x: -x["_prefilter_signal"])
    cap = max(min_keep, int(len(jobs) * keep_ratio))
    kept = survivors[:cap]

    print(f"→ prefilter: {len(jobs)} → {len(kept)} "
          f"(saved ~{(len(jobs)-len(kept))//8} LLM calls)")
    return kept
