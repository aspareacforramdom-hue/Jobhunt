"""
BUILDING THE GOOGLE FORM, instead of asking someone to type it.

The intake has always shipped a list of questions to paste into Google
Forms by hand. Twenty questions, each with its exact wording and its
right answer type, retyped without a slip — because match_answers pairs
the CSV back to the schema ON THE QUESTION TEXT, so one edited word
silently drops that answer and the profile compiles without it.

Google Forms cannot import a list. Apps Script can build one. So this
emits a script that creates the form, with the wording taken straight
from config/profile_schema.yml, which means the form and the importer
cannot disagree.
"""

import json

TYPES = {"multi": "Checkboxes", "single": "Multiple choice",
         "number": "Short answer", "text": "Paragraph"}

# Answers the schema asks for that we already know, so she is not made
# to re-type what has already been decided. Shown as a prefilled hint in
# the question rather than skipped, because she is allowed to disagree.
def _hint(dim, known):
    value = known.get(dim["id"])
    return f"  (we have: {value} — change it if that's wrong)" if value else ""


def apps_script(schema, title="Career Loop — job search intake",
                known=None):
    """A script the user pastes into script.google.com and runs once."""
    known = known or {}
    items = []
    for d in schema.get("dimensions", []):
        items.append({
            "id": d["id"],
            "ask": " ".join((d.get("ask") or "").split()),
            "help": ("Answer in your own words." if d.get("type") == "text"
                     else "") + _hint(d, known),
            "type": d.get("type", "text"),
            "options": d.get("options", []),
            "track": d.get("track", ""),
        })

    payload = json.dumps(items, indent=2, ensure_ascii=False)
    return f"""/**
 * CAREER LOOP — builds the intake form.
 *
 * WHAT TO DO
 *   1. Go to  script.google.com  and click  New project
 *   2. Delete whatever is in the editor, paste ALL of this in
 *   3. Click  Run  (the play button). Approve access when asked —
 *      it is your own account making your own form.
 *   4. The form's link is printed in the box at the bottom.
 *
 * Do not edit the question wording. Career Loop matches the answers
 * back to the questions by their text, so a changed word means that
 * answer is quietly dropped and the profile is built without it.
 */

var QUESTIONS = {payload};

function buildForm() {{
  var form = FormApp.create({title!r})
    .setDescription(
      'This builds a job search tuned to you.\\n\\n' +
      'There are two tracks, and they are scored separately:\\n' +
      '  TRACK 1 — the job you want to grow into.\\n' +
      '  TRACK 2 — the best-paid job your CV already qualifies you for.\\n\\n' +
      'Answer both honestly. Short answers are fine. The only wrong ' +
      'answer is a flattering one — this searches on what you write.')
    .setCollectEmail(false)
    .setProgressBar(true);

  QUESTIONS.forEach(function (q) {{
    var item;
    if (q.type === 'single') {{
      item = form.addMultipleChoiceItem();
      item.setChoiceValues(q.options);
    }} else if (q.type === 'multi') {{
      item = form.addCheckboxItem();
      item.setChoiceValues(q.options);
    }} else if (q.type === 'number') {{
      item = form.addTextItem();
    }} else {{
      item = form.addParagraphTextItem();
    }}
    item.setTitle(q.ask);
    if (q.help) {{ item.setHelpText(q.help); }}
    item.setRequired(false);
  }});

  Logger.log('FORM (fill this in):  ' + form.getPublishedUrl());
  Logger.log('EDIT (yours to change): ' + form.getEditUrl());
}}

function run() {{ buildForm(); }}
"""


def gemini_prompt(schema, known=None):
    """The same form, as an instruction for Gemini to build in Workspace.

    Third route to the same form, because the other two both have a
    cost: Apps Script asks a non-technical person to paste code and
    approve permissions, and building it by hand is 22 questions retyped
    without a slip. Gemini can create the form in the user's own Drive.

    The prompt is generated rather than written, for the same reason the
    script is: the question wording is the join key between the form and
    the profile, so it can only ever come from the schema.
    """
    known = known or {}
    out = [
        "Create a Google Form in my Google account with the title",
        '"Career Loop - job search intake".',
        "",
        "Set the form description to:",
        "  This builds a job search tuned to you. There are two tracks and",
        "  they are scored separately. TRACK 1 is the job you want to grow",
        "  into. TRACK 2 is the best-paid job your CV already qualifies you",
        "  for. Answer both honestly - short answers are fine. The only",
        "  wrong answer is a flattering one, because the search runs on",
        "  what you write here.",
        "",
        "RULES, and the first one matters more than the rest:",
        "",
        "1. Use each question's wording EXACTLY as written below, character",
        "   for character. Do not reword, shorten, correct, rephrase or",
        "   improve anything. A separate program reads the answers back by",
        "   matching on this text, so a single changed word means that",
        "   answer is silently discarded.",
        "2. Keep the questions in the order given.",
        "3. Use the answer type named in brackets for each question:",
        "   Paragraph = long answer text, Short answer = short answer text,",
        "   Multiple choice = pick one, Checkboxes = pick many.",
        "4. For Multiple choice and Checkboxes, use exactly the options",
        "   listed under the question, in that order, and add no others.",
        "5. Make every question OPTIONAL, not required.",
        "6. Do not collect email addresses. Turn the progress bar on.",
        "7. Do not add any questions of your own, and do not add section",
        "   headers, page breaks or images.",
        "",
        "When you are done, give me the link to fill in the form and the",
        "link to edit it.",
        "",
        "Here are the questions.",
        "",
    ]
    for i, d in enumerate(schema.get("dimensions", []), 1):
        ask = " ".join((d.get("ask") or "").split())
        out.append(f"{i}. [{TYPES.get(d.get('type'), 'Paragraph')}] {ask}")
        for o in d.get("options", []):
            out.append(f"     - {o}")
        hint = _hint(d, known).strip()
        if hint:
            out.append(f"     (help text: {hint.strip('() ')})")
        out.append("")
    return "\n".join(out)


def plain_text(schema):
    """The same questions as a list, for anyone building the form by hand
    or answering over a chat."""
    lines = ["CAREER LOOP INTAKE",
             "Do not edit the question wording — the importer matches on it.",
             ""]
    for i, d in enumerate(schema.get("dimensions", []), 1):
        ask = " ".join((d.get("ask") or "").split())
        lines.append(f"{i}. [{TYPES.get(d.get('type'), 'Paragraph')}] {ask}")
        for o in d.get("options", []):
            lines.append(f"      - {o}")
        lines.append("")
    return "\n".join(lines)
