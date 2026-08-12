"""
Export the current filtered view.

Columns are built from the user's own dimensions, so two users' exports
have different columns — the same reason the scorer has no fixed
vocabulary.

Falls back to CSV if openpyxl is not installed rather than 500ing. The
portal shows the same data; the export is a convenience.
"""

import csv
import io
import re

from core import dimensions

# Everything except tab, newline and carriage return. openpyxl refuses
# these outright, and scraped descriptions carry them regularly.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Excel and Sheets execute a cell that opens with any of these. The
# value came off a job board, so it is not ours to trust.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _cell(value):
    """Make one scraped value safe to put in a spreadsheet."""
    if value is None or isinstance(value, (int, float, bool)):
        return value

    text = _CONTROL.sub("", str(value))
    if text.startswith(_FORMULA_LEAD):
        # A leading apostrophe is Excel's own "treat as text" marker and
        # is not shown in the cell.
        text = "'" + text
    return text[:32000]          # Excel's hard per-cell limit is 32767

BASE = ["Score", "Title", "Company", "Location", "Status", "Stretch",
        "Verdict", "Angle", "Reason against", "Found", "Source", "URL"]


def _rows(jobs, profile):
    dims = dimensions.ordered_ids((profile or {}).get("weights") or {})
    header = BASE + [dimensions.label(d) for d in dims]

    out = [header]
    for j in jobs:
        breakdown = j.get("score_breakdown") or {}
        out.append([_cell(v) for v in [
            j.get("score"), j.get("title"), j.get("company"),
            j.get("location"), j.get("status"),
            "yes" if j.get("stretch") else "",
            j.get("verdict"), j.get("angle"),
            j.get("risk") or j.get("ignored_reason") or "",
            j.get("found_at"), j.get("source"), j.get("url"),
        ] + [breakdown.get(d) for d in dims]])
    return out


def build(jobs, profile):
    """Returns (bytes, media_type, extension)."""
    rows = _rows(jobs, profile)
    try:
        return _xlsx(rows), (
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"), "xlsx"
    except ImportError:
        return _csv(rows), "text/csv", "csv"


def _xlsx(rows):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Career Loop"

    for row in rows:
        ws.append(row)

    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"

    widths = {1: 8, 2: 38, 3: 22, 4: 18, 5: 12, 6: 9, 7: 52, 8: 52, 9: 52}
    for i in range(1, len(rows[0]) + 1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(i, 16)
    for col in "GHI":
        for cell in ws[col]:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _csv(rows):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue().encode("utf-8")
