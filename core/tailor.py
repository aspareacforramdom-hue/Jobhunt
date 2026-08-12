"""
STAGE 6 — TAILOR    (your sketch: "customise resume / make cover letter")
STAGE 7 — OUTREACH  (missing from your sketch — where interviews come from)

Everything here is DRAFT ONLY. Nothing sends, submits, or clicks.
"""

import json

from core.llm import WEB_SEARCH_TOOL, call_llm

TAILOR_SYSTEM = """Tailor an application for one specific role.

RULES:
- Never invent experience. Reframe real things; fabricate nothing.
- Lead with the candidate's unfair advantage, not their job title.
- Cover letter: under 150 words. Nobody reads more.
- If the candidate is a genuine stretch for this role, address it
  directly in one line rather than hiding it. Hiding reads as weak.

The output is copied by hand into a CV and a LinkedIn box. Write it as
finished text, not as advice about what to write.

Return ONLY JSON:
{
  "headline": "<one line to open the CV for this role>",
  "bullet_swaps": [
    {"replace": "<which existing bullet to swap out — quote its opening words>",
     "with": "<the rewritten bullet, ready to paste>"}
  ],
  "keywords_to_add": ["ATS terms from the JD they genuinely have"],
  "cover_letter": "<under 150 words, plain text, no placeholders>",
  "linkedin_note": "<under 300 characters — a connect note, not a cover letter>",
  "stretch_line": "<one line addressing the gap head-on, or null>"
}"""

OUTREACH_SYSTEM = """Find the human worth contacting for this role, and draft the message.

Search for: the hiring manager, the team lead, or the recruiter
who owns this req at this company.

The message must be under 300 characters. It is a LinkedIn connect
note, not a cover letter. It should reference something specific and
real about the role or the company — generic notes get ignored.

Return ONLY JSON:
{
  "likely_contacts": [
    {"name": "...", "title": "...", "why_them": "...", "source": "<url>"}
  ],
  "message": "<under 300 chars>",
  "follow_up": "<what to say if they accept but don't reply, under 200 chars>",
  "confidence": "<high|medium|low>"
}"""


def tailor(job, profile):
    prompt = f"""ROLE:
{job.get('title')} at {job.get('company')} — {job.get('location')}

JOB DESCRIPTION:
{(job.get('description') or '')[:2500]}

CANDIDATE ANGLES (real, verified):
{json.dumps(profile.get('angles', []), indent=2)}

CANDIDATE TARGET:
{json.dumps(profile.get('target', {}), indent=2)}

Scoring notes from earlier evaluation:
  verdict: {job.get('verdict')}
  strongest angle: {job.get('angle')}
  is stretch: {job.get('stretch')}"""

    return call_llm(TAILOR_SYSTEM, prompt, max_tokens=1800, as_json=True)


def find_contact(job, profile):
    prompt = f"""COMPANY: {job.get('company')}
ROLE: {job.get('title')}
LOCATION: {job.get('location')}
LISTING: {job.get('url')}

CANDIDATE'S STRONGEST ANGLE FOR THIS ROLE:
{job.get('angle')}

Search for who owns hiring for this role, then draft the note."""

    return call_llm(OUTREACH_SYSTEM, prompt, max_tokens=2000,
                    as_json=True, tools=WEB_SEARCH_TOOL)


def prepare_application(job, profile):
    """
    One job's worth of application material.

    Tailoring must succeed or the screen has nothing to show, so it is
    allowed to raise. Outreach is best-effort: it does a web search that
    can fail or time out, and the cover letter and LinkedIn note are
    still useful without knowing who the hiring manager is.
    """
    tailored = tailor(job, profile)

    outreach = None
    try:
        outreach = find_contact(job, profile)
    except Exception as e:
        print(f"    outreach failed (non-blocking): {e}")

    return tailored, outreach


def prepare_top(jobs, profile, n=3):
    """Only the top N get the expensive treatment. Focus beats volume."""
    out = []
    for j in jobs[:n]:
        print(f"  preparing: {j['title']} @ {j['company']}")
        try:
            j["tailored"] = tailor(j, profile)
        except Exception as e:
            print(f"    tailor failed: {e}")
        try:
            j["outreach"] = find_contact(j, profile)
        except Exception as e:
            print(f"    outreach failed: {e}")
        out.append(j)
    return out
