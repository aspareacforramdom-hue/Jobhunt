"""
READING THE CV PEOPLE ACTUALLY HAVE.

Upload accepted plain text only. Nobody's CV is a .txt file, and a step
between a person and a feature is a step at which they stop — so an
empty resume_text was the biggest quality hole in the tool.

Two lessons from measuring this against real PDFs printed by Chromium
and real .docx files, both pinned below:

  Furniture removal nearly ate the CV. Stripping lines that "appear on
  every page" also matched EXPERIENCE, EDUCATION, SKILLS, the degree
  and a whole job — 57% of a two-page document, including three of the
  seven facts that made the person employable. On two pages, "every
  page" only means "twice". Position is what separates a running header
  from a heading.

  A corrupt file was accepted as text. A .docx with a broken zip fell
  through to the plain-text branch and saved 282 characters of
  "PKgarbagegarbage..." as somebody's CV.
"""

import io
import zipfile

import pytest

from core import resume

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def make_docx(paragraphs):
    """A real .docx — the parts Word itself writes."""
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>'
        for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("_rels/.rels", "<Relationships/>")
        z.writestr("word/document.xml",
                   f'<w:document {W}><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


CV_LINES = [
    "SREEHARI VILAS", "Senior Product Manager, Payments", "",
    "EXPERIENCE",
    "Paytm - Senior Product Manager (2022-2026)",
    "Owned reconciliation across 2M transactions per day.",
    "Led the migration off a payments monolith to 14 services.",
    "Grew UPI merchant activation 3x across 40,000 merchants.",
    "Kotak Mahindra Bank - Product Manager (2019-2022)",
    "Built the first in-app lending flow, 400,000 applications.",
    "EDUCATION", "B.Tech Computer Science, NIT Calicut, 2017",
]


# ============================================================
# Knowing what a file is
# ============================================================
def test_a_file_is_identified_by_its_bytes_not_its_name():
    """An extension is a claim; the first four bytes are a fact. People
    rename files, and mail clients rename them too."""
    assert resume.sniff(b"%PDF-1.7\nrest") == "pdf"
    assert resume.sniff(make_docx(["hello"])) == "docx"
    assert resume.sniff(b"{\\rtf1\\ansi text}") == "rtf"
    assert resume.sniff(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 10) == "doc"
    assert resume.sniff(b"Sreehari Vilas\nProduct Manager") == "text"


def test_a_legacy_doc_is_refused_with_something_to_do_about_it():
    raw = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 900

    with pytest.raises(ValueError, match="save as .docx"):
        resume.extract(raw)


def test_a_damaged_file_is_never_guessed_at():
    """A .docx with a broken zip fell through to the text branch and
    saved 282 characters of "PKgarbagegarbage..." as a CV."""
    raw = b"PK\x03\x04" + b"garbage" * 60

    with pytest.raises(ValueError, match="damaged|readable text"):
        resume.extract(raw)


def test_a_binary_file_that_decodes_is_still_refused():
    with pytest.raises(ValueError, match="readable text|characters"):
        resume.extract(bytes(range(1, 256)) * 4)


def test_an_enormous_file_is_refused_before_it_is_parsed():
    with pytest.raises(ValueError, match="probably not one"):
        resume.extract(b"x" * (resume.MAX_BYTES + 1))


def test_an_empty_file_is_refused():
    with pytest.raises(ValueError, match="empty"):
        resume.extract(b"")


# ============================================================
# DOCX, with no dependency at all
# ============================================================
def test_a_docx_is_read_without_any_library():
    text, note = resume.extract(make_docx(CV_LINES))

    assert "Paytm" in text and "NIT Calicut" in text
    assert note is None


def test_every_bullet_stays_on_its_own_line():
    """The tailor is asked to rewrite individual bullets and quote them
    back verbatim. Running a job's bullets together into one paragraph
    would take that feature away."""
    text, _ = resume.extract(make_docx(CV_LINES))

    assert "2M transactions per day." in text.splitlines()[5]
    assert "14 services." in text.splitlines()[6]


def test_a_docx_with_no_document_part_is_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", "<x/>")

    with pytest.raises(ValueError):
        resume.extract(buf.getvalue())


# ============================================================
# Page furniture — the one that nearly ate the CV
# ============================================================
def test_a_running_header_is_dropped():
    """It eats the 6,000 characters the scorer is given, and repeats
    itself inside the prompt."""
    header = "Sreehari Vilas - Curriculum Vitae"
    pages = [f"{header}\n{'EXPERIENCE'}\nPaytm, reconciliation at 2M/day\n1",
             f"{header}\nEDUCATION\nNIT Calicut, 2017\n2"]

    out = resume._strip_repeated_furniture(pages)

    assert header not in out
    assert "Paytm" in out and "NIT Calicut" in out


def test_a_section_heading_is_never_mistaken_for_furniture():
    """
    THE lesson. "Appears on every page" also matches EXPERIENCE on a CV
    whose sections repeat — and on two pages, "every page" just means
    "twice". It threw away 57% of a document, three of seven key facts
    included. Furniture lives at the EDGE of a page; content does not.
    """
    pages = ["top\nEXPERIENCE\nPaytm, 2M transactions per day\nfoot",
             "top\nEXPERIENCE\nKotak, 400,000 applications\nfoot"]

    out = resume._strip_repeated_furniture(pages)

    assert "EXPERIENCE" in out, "a repeated heading is content, not furniture"
    assert "Paytm" in out and "Kotak" in out
    assert "top" not in out.split("\n") and "foot" not in out.split("\n")


def test_page_numbers_go():
    pages = ["Paytm, reconciliation\nPage 1 of 2",
             "Kotak, lending flow\nPage 2 of 2"]

    out = resume._strip_repeated_furniture(pages)

    assert "Page 1 of 2" not in out
    assert "Paytm" in out and "Kotak" in out


def test_a_one_page_cv_is_left_completely_alone():
    """With one page there is no repetition to reason from, so touching
    anything would be a guess."""
    page = "SREEHARI VILAS\nEXPERIENCE\nPaytm\n1"

    assert resume._strip_repeated_furniture([page]) == page


# ============================================================
# Keeping the shape of the text
# ============================================================
def test_bullet_characters_become_something_a_model_can_read():
    text = resume.tidy("• Owned reconciliation\n• Led migration")

    assert text.startswith("- Owned")
    assert "- Led migration" in text


def test_blank_lines_are_collapsed_but_not_removed():
    """Paragraph breaks separate one job from the next."""
    text = resume.tidy("Paytm\n\n\n\n\nKotak")

    assert text == "Paytm\n\nKotak"


def test_a_short_file_is_refused_rather_than_saved():
    """Saving forty characters means the scorer judges this person on
    nothing, and never says so."""
    with pytest.raises(ValueError, match="characters"):
        resume.extract(b"Sreehari Vilas, PM")


# ============================================================
# PDF
# ============================================================
@pytest.mark.skipif(not resume.pdf_available(), reason="pypdf not installed")
def test_a_broken_pdf_says_so():
    with pytest.raises(ValueError, match="could not be opened"):
        resume.extract(b"%PDF-1.4\n%%EOF\n")


@pytest.mark.skipif(not resume.pdf_available(), reason="pypdf not installed")
def test_a_real_pdf_round_trips(tmp_path):
    """Built by Chromium, so this is a real PDF with real fonts rather
    than something hand-assembled to pass."""
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    html = "<h1>SREEHARI VILAS</h1>" + "".join(
        f"<p>{line}</p>" for line in CV_LINES if line)
    out = tmp_path / "cv.pdf"
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page()
        page.set_content(html)
        page.pdf(path=str(out), format="A4")
        b.close()

    text, note = resume.extract(out.read_bytes())

    assert "Paytm" in text
    assert "NIT Calicut" in text
    assert "400,000 applications" in text
    assert note is None
