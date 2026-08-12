"""
PHASE 2 — what the board actually shows.

The rule that matters: thresholds are RELATIVE, not absolute.

A fixed minimum score is a trap. A first run against a thin market can
score everything 5.8, and a hardcoded 6.5 bar then renders an empty
board — which reads as "there are no jobs" when it actually means "the
bar was set before there was any data to set it from".

So:
  - 4.0 is an absolute floor, and it only kills obvious garbage
  - the board shows the top N for the day, whatever they scored
  - the score distribution is shown next to it, so the user can see
    where the real bar sits and stop trusting the number in isolation

The board is never empty while anything has been scored.
"""

from core import db

FLOOR = 4.0
BOARD_LIMIT = 15


def build_board(conn, user_id, limit=BOARD_LIMIT, floor=FLOOR):
    """
    The five columns, plus the context needed to read them honestly.

    Only the NEW column is capped. The others are already curated by the
    user — hiding something they marked `interested` would be wrong.

    Three queries, not six. This used to run one query per column plus
    another for the distribution, each pulling every job's 4KB
    description to render a title. NEW is capped in SQL because it is
    the only unbounded column; the other four are hand-curated and stay
    small, so one query covers them all and Python partitions the result.
    """
    new_rows = db.list_user_jobs(conn, user_id, statuses=["new"],
                                 min_score=floor, limit=limit)
    new_total = db.count_user_jobs(conn, user_id, statuses=["new"],
                                   min_score=floor)

    curated_statuses = [s for _, _, states in db.BOARD_COLUMNS
                        for s in states if s != "new"]
    curated = db.list_user_jobs(conn, user_id, statuses=curated_statuses)

    by_status = {}
    for row in curated:
        by_status.setdefault(row["status"], []).append(row)

    columns = []
    for key, label, statuses in db.BOARD_COLUMNS:
        if key == "new":
            columns.append({"key": key, "label": label, "jobs": new_rows,
                            "shown": len(new_rows), "total": new_total})
            continue
        rows = [r for s in statuses for r in by_status.get(s, [])]
        rows.sort(key=lambda r: -(r["score"] or 0))
        columns.append({"key": key, "label": label, "jobs": rows,
                        "shown": len(rows), "total": len(rows)})

    return {
        "columns": columns,
        "distribution": distribution(conn, user_id, floor),
        "floor": floor,
        "limit": limit,
    }


def distribution(conn, user_id, floor=FLOOR):
    """
    Where today's bar actually sits, for this user only.

    Shown on the board so a day where everything scored 5.8 looks like
    what it is — a flat day — rather than like a broken tool.
    """
    # One column, not eight hundred hydrated rows. See db.scores_for.
    scores = sorted(s for s in db.scores_for(conn, user_id)
                    if isinstance(s, (int, float)))

    if not scores:
        return {"count": 0, "buckets": [], "below_floor": 0}

    def pct(p):
        return scores[min(len(scores) - 1, int(len(scores) * p))]

    counts = {}
    for s in scores:
        counts[int(max(1, min(10, s)))] = counts.get(int(max(1, min(10, s))), 0) + 1
    peak = max(counts.values())

    buckets = [{"score": b, "count": counts.get(b, 0),
                "pct": round(counts.get(b, 0) / peak * 100)}
               for b in range(1, 11)]

    return {
        "count": len(scores),
        "min": round(scores[0], 1),
        "max": round(scores[-1], 1),
        "median": round(pct(0.5), 1),
        "p90": round(pct(0.90), 1),
        "below_floor": sum(1 for s in scores if s < floor),
        "buckets": buckets,
    }
