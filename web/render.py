"""Template setup and the small helpers every page needs."""

import os
from datetime import datetime, timezone

from fastapi.templating import Jinja2Templates

from core import db, dimensions

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "templates")

templates = Jinja2Templates(directory=TEMPLATE_DIR)
templates.env.filters["dim_label"] = dimensions.label


def ago(value):
    """`12m ago` — for the scan status in the header."""
    ts = db.parse_ts(value)
    if not ts:
        return "never"
    secs = (datetime.now(timezone.utc) - ts).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def local_date(value):
    ts = db.parse_ts(value)
    return ts.astimezone(db.IST).strftime("%d %b") if ts else ""


templates.env.filters["ago"] = ago
templates.env.filters["local_date"] = local_date


def page(request, name, **context):
    """Render with the context every template expects to exist."""
    user = request.state.user
    context.setdefault("user", user)
    context.setdefault("msg", request.query_params.get("msg"))
    context.setdefault("scan", request.app.state.scanner.status())
    return templates.TemplateResponse(request, name, context)


def breakdown_rows(job, profile):
    """
    One row per dimension, in this user's own vocabulary.

    Sorted by weight so the dimensions the user said matter most are read
    first, and every row carries its definition — the detail page has to
    justify the score, not just assert it.
    """
    weights = (profile or {}).get("weights") or {}
    defs = dimensions.definitions_for(profile or {})
    breakdown = job.get("score_breakdown") or {}
    weakest = dimensions.weakest(breakdown, weights)

    rows = []
    for d in dimensions.ordered_ids(weights):
        if d not in breakdown:
            continue
        rows.append({
            "id": d,
            "label": dimensions.label(d),
            "score": breakdown[d],
            "weight": weights.get(d, 0),
            "definition": defs.get(d, ""),
            "is_weakest": d == weakest,
        })
    return rows
