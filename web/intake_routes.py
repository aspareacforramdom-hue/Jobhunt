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
import re
from urllib.parse import quote

import yaml
from fastapi import Form, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse

from core import calibrate as calib
from core import db, intake, resume
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


# Exact string matching on a question is brittle in exactly the way
# that matters: the form is built by a person or a model, and both tidy
# wording. Measured on the first real form, built by Gemini from the
# generated prompt: 19 of 22 questions came back character-perfect and
# three had the help text appended INTO the title —
#
#   Below what CTC would you say no, even to a great role? (help text:
#   we have: 25 — change it if that's wrong)
#
# Under exact matching those three answers are dropped in silence and
# the profile compiles with no salary floor, no cities and no notice
# period. So the matcher normalises first and falls back to close
# matching, and says out loud when it had to.
_TRAILING_PAREN = re.compile(r"\s*\((?:help text|hint|note)\s*:.*?\)\s*$",
                             re.I | re.S)
_PUNCT_FOLD = {0x2018: "'", 0x2019: "'", 0x201c: '"', 0x201d: '"',
               0x2013: "-", 0x2014: "-", 0x2026: "...", 0x00a0: " "}


def normalise_question(text):
    """Fold a question to the form two spellings of it can be compared in."""
    text = (text or "").translate(_PUNCT_FOLD)
    text = _TRAILING_PAREN.sub("", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def match_answers(schema, row):
    """
    Map CSV headers back to schema dimension ids.

    Google Forms exports the question text as the header, so the match is
    on the question: exactly first, then normalised, then on close
    resemblance, then on the dimension id for a hand-edited sheet.

    Returns (answers, unmatched). Anything matched loosely is recorded in
    answers and reported by `fuzzy_matches` so a rewritten question is
    visible rather than a hole in the profile.
    """
    from difflib import SequenceMatcher

    answers, unmatched, fuzzy = {}, [], []
    dims = schema.get("dimensions", [])
    by_question = {d["ask"].strip().lower(): d["id"] for d in dims}
    by_norm = {normalise_question(d["ask"]): d["id"] for d in dims}
    by_id = {d["id"]: d["id"] for d in dims}

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

        bare = header.strip()
        key = bare.lower()
        norm = normalise_question(bare)

        dim = (by_question.get(key) or by_norm.get(norm)
               or by_id.get(bare))

        if not dim and norm:
            # Close enough that a human would call it the same question.
            # 0.9 is deliberately tight: this is meant to survive a
            # tidied word, not to guess at a question nobody asked.
            best, score = None, 0.0
            for cand, cand_id in by_norm.items():
                if cand_id in answers:
                    continue
                r = SequenceMatcher(None, norm, cand).ratio()
                if r > score:
                    best, score = cand_id, r
            if score >= 0.90:
                dim, _ = best, fuzzy.append((bare, best, round(score, 3)))

        if dim:
            answers[dim] = value.strip()[:2000]
        elif key not in ("timestamp", "email address", "score"):
            unmatched.append(bare)

    match_answers.fuzzy_matches = fuzzy
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
        tailor has nothing real to quote.

        It took plain text only, on the reasoning that parsing PDF meant
        a new dependency and "save as .txt" is one step. But nobody's CV
        is a .txt file, and a step between a person and the feature is a
        step at which they stop. core/resume.py now reads PDF, .docx,
        .rtf and text, identified by magic bytes rather than by the
        extension — an extension is a claim, the first four bytes are a
        fact.
        """
        if cv_file is None:
            return RedirectResponse("/intake?msg=No+file+chosen",
                                    status_code=303)

        raw = await cv_file.read(resume.MAX_BYTES + 1)
        try:
            text, note = resume.extract(raw, cv_file.filename or "")
        except ValueError as e:
            # Written for the person holding the file, not for a log.
            return RedirectResponse(
                "/intake?msg=" + quote(str(e), safe=""), status_code=303)

        conn = db.connect(request.state.db_path)
        try:
            user_id = request.state.user["id"]
            profile = db.load_profile(conn, user_id) or {}
            profile["resume_text"] = text
            db.save_profile(conn, user_id, profile)
        finally:
            conn.close()

        kind = resume.sniff(raw)
        msg = (f"CV saved — {len(text):,} characters from your "
               f"{kind.upper()}. Scoring and drafts now use it.")
        if note:
            msg += f" Note: {note}"
        return RedirectResponse("/intake?msg=" + quote(msg, safe=""),
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
