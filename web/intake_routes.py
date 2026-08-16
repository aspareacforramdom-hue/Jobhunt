"""
PHASE 4 — Google Form intake.

Replaces the CLI questionnaire. The form spec is generated from
config/profile_schema.yml so the questions and the compiler never drift
apart, and the CSV export is fed straight into the existing
intake.compile_profile().

Dropped answers are shown, prominently. Vague input producing confident
output is the failure mode of every AI career tool, so an answer that
compiled to nothing is reported rather than quietly absorbed.

TODO: adaptive follow-up questions. The CLI loop asks whichever
dimension is most uncertain; a static form cannot. v1 ships without it.
"""

import csv
import io
from urllib.parse import quote

import yaml
from fastapi import Form, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse

from core import calibrate as calib
from core import db, intake
from web.render import page

SCHEMA_PATH = "config/profile_schema.yml"


def load_schema(path=SCHEMA_PATH):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def form_spec(schema):
    """The questions to paste into Google Forms, in schema order."""
    out = []
    for d in schema.get("dimensions", []):
        out.append({
            "id": d["id"],
            "question": d["ask"],
            "type": {"multi": "Checkboxes", "single": "Multiple choice",
                     "number": "Short answer", "text": "Paragraph"}
                    .get(d.get("type"), "Paragraph"),
            "options": d.get("options", []),
            "compiles_to": d.get("compiles_to"),
        })
    return out


def match_answers(schema, row):
    """
    Map CSV headers back to schema dimension ids.

    Google Forms exports the question text as the header, so the match is
    on the question, with a fallback to the id for a hand-edited sheet.
    """
    answers, unmatched = {}, []
    by_question = {d["ask"].strip().lower(): d["id"]
                   for d in schema.get("dimensions", [])}
    by_id = {d["id"]: d["id"] for d in schema.get("dimensions", [])}

    for header, value in (row or {}).items():
        # csv.DictReader hands back a None key holding a LIST when a row
        # has more fields than the header, and None values when it has
        # fewer. Neither is a string, and both arrive from a sheet three
        # people have been editing by hand.
        if not isinstance(header, str) or not header.strip():
            continue
        if isinstance(value, (list, tuple)):
            value = " ".join(str(v) for v in value if v is not None)
        value = "" if value is None else str(value)
        if not value.strip():
            continue

        key = header.strip().lower()
        dim = by_question.get(key) or by_id.get(header.strip())
        if dim:
            answers[dim] = value.strip()[:2000]
        elif key not in ("timestamp", "email address", "score"):
            unmatched.append(header.strip())

    return answers, unmatched


def register(app, db_path=None):

    @app.get("/intake")
    def intake_page(request: Request):
        conn = db.connect(db_path)
        try:
            profile = db.load_profile(conn, request.state.user["id"])
        finally:
            conn.close()
        resume = ((profile or {}).get("resume_text") or "").strip()
        return page(request, "intake.html", spec=form_spec(load_schema()),
                    profile=profile,
                    # The length, not the text. The page needs to say
                    # whether a CV is on file, not carry a copy of it.
                    resume_chars=len(resume))

    @app.get("/intake/form-spec.txt")
    def form_spec_text(request: Request):
        """Plain text to paste into Google Forms."""
        lines = ["CAREER LOOP INTAKE",
                 "Paste each question into a new Google Form.",
                 "Do not edit the question text — the importer matches on it.",
                 ""]
        for i, q in enumerate(form_spec(load_schema()), 1):
            lines.append(f"{i}. [{q['type']}] {q['question']}")
            for o in q["options"]:
                lines.append(f"      - {o}")
            lines.append("")
        return PlainTextResponse("\n".join(lines))

    @app.post("/intake/resume")
    async def upload_resume(request: Request, cv_file: UploadFile = None):
        """
        Store the CV text against this user's profile.

        This is the whole point of the feature: until a résumé is here,
        the scorer judges jobs against a three-sentence summary and the
        tailor has nothing real to quote. Plain text only — parsing PDF
        and DOCX would mean a new dependency and a new class of failure,
        and "save as .txt" is one step for three people.
        """
        if cv_file is None:
            return RedirectResponse("/intake?msg=No+file+chosen",
                                    status_code=303)

        raw = (await cv_file.read())[:200_000]
        text = raw.decode("utf-8", errors="replace").strip()
        if len(text) < 200:
            return RedirectResponse(
                "/intake?msg=" + quote("That file had almost no text in it. "
                                       "Export your CV as plain text (.txt) "
                                       "and try again.", safe=""),
                status_code=303)

        conn = db.connect(request.state.db_path)
        try:
            user_id = request.state.user["id"]
            profile = db.load_profile(conn, user_id) or {}
            profile["resume_text"] = text
            db.save_profile(conn, user_id, profile)
        finally:
            conn.close()

        return RedirectResponse(
            "/intake?msg=" + quote(f"CV saved — {len(text):,} characters. "
                                   f"Scoring and drafts now use it.",
                                   safe=""),
            status_code=303)

    @app.post("/intake")
    async def intake_upload(request: Request, csv_file: UploadFile = None,
                            row_index: int = Form(0)):
        if request.app.state.dry:
            return RedirectResponse("/intake?msg=Dry+run+—+no+LLM+calls+made",
                                    status_code=303)
        if csv_file is None:
            return RedirectResponse("/intake?msg=No+file+uploaded",
                                    status_code=303)

        raw = (await csv_file.read()).decode("utf-8-sig", errors="replace")
        rows = list(csv.DictReader(io.StringIO(raw)))
        if not rows:
            return RedirectResponse("/intake?msg=That+CSV+had+no+rows",
                                    status_code=303)

        # A shared form collects all three users' responses in one sheet,
        # so the importer takes one row — the latest by default.
        row = rows[min(max(row_index, 0), len(rows) - 1)] if row_index \
            else rows[-1]
        schema = load_schema()
        answers, unmatched = match_answers(schema, row)

        if not answers:
            return RedirectResponse(
                "/intake?msg=No+column+matched+a+schema+question",
                status_code=303)

        user_id = request.state.user["id"]
        conn = db.connect(db_path)
        try:
            existing = db.load_profile(conn, user_id) or {}
            parsed = {"current_role": answers.get("current_role"),
                      "implied_trajectory": None,
                      "prior_profile_target": existing.get("target")}

            profile = intake.compile_profile(parsed, answers, save=False)

            # Keep the search terms a previous calibrate produced; a new
            # intake does not invalidate what the market calls the role.
            if existing.get("search_terms"):
                profile["search_terms"] = existing["search_terms"]
            else:
                calib.attach_search_terms(profile, None)

            if unmatched:
                profile["dropped"] = list(profile.get("dropped", [])) + [
                    f"[import] column '{c}' matched no schema question"
                    for c in unmatched]

            db.save_profile(conn, user_id, profile)
        except Exception as e:
            # Flattened and capped. The compiler's failure is an API
            # error body — multi-line and long — and it used to go into
            # the URL whole, so the flash was a wall of %0A and JSON.
            detail = quote(" ".join(f"Compile failed: {e}".split())[:240],
                           safe="")
            return RedirectResponse(f"/intake?msg={detail}", status_code=303)
        finally:
            conn.close()

        return RedirectResponse("/intake?msg=Profile+compiled", status_code=303)
