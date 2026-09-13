"""
GETTING THE CV OUT OF WHATEVER THEY HAVE.

This used to accept plain text only, on the reasoning that parsing PDF
meant a new dependency and a new class of failure, and that "save as
.txt" is one step. That reasoning was wrong about the thing that
matters: nobody's CV is a .txt file, and a step between a person and
the feature is a step at which they stop. An empty resume_text is the
single biggest quality hole in this tool — without it the scorer judges
roles against a three-sentence summary and the tailor has never seen a
bullet it is asked to rewrite.

So: PDF, DOCX, RTF and plain text, sniffed by MAGIC BYTES rather than
by the extension, because an extension is a claim and the first four
bytes are a fact. People rename files.

WHAT IS HARD HERE is not getting characters out, it is getting them out
in the right ORDER. score.py sends resume[:6000] inside its cached
prefix; tailor.py quotes from the same slice. If a two-column CV comes
back with the skills sidebar interleaved into the job history, those
6000 characters are noise and every judgement downstream is made on
noise — silently, because the text looks fine at a glance.

That is why extraction reports a `note` and the caller shows it, and
why anything suspicious is described to the user rather than saved
quietly.
"""

import io
import re
import zipfile

# Word stores text in a namespaced XML part inside an ordinary zip. No
# dependency needed for the format most CVs actually arrive in.
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

MAX_BYTES = 5_000_000
MIN_CHARS = 200

# Enough of a CV to be worth keeping. Below this something went wrong:
# a scanned PDF with no text layer, an encrypted file, or a parser that
# found the wrong part of the document.
_WS_RUN = re.compile(r"[ \t\u00a0]+")
_BLANK_RUN = re.compile(r"\n{3,}")
_PAGE_NO = re.compile(r"^\s*(?:page\s*)?\d+\s*(?:/|of)?\s*\d*\s*$", re.I)


def sniff(raw):
    """What this file actually is, from its first bytes."""
    if raw[:4] == b"%PDF":
        return "pdf"
    if raw[:4] == b"PK\x03\x04":
        # Every Office and OpenDocument file is a zip. Look inside.
        try:
            names = set(zipfile.ZipFile(io.BytesIO(raw)).namelist())
        except zipfile.BadZipFile:
            return "unknown"
        if "word/document.xml" in names:
            return "docx"
        if "content.xml" in names:
            return "odt"
        return "zip"
    if raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        # OLE2 compound file: legacy .doc, or .xls.
        return "doc"
    if raw[:5] == b"{\\rtf":
        return "rtf"
    return "text"


# ------------------------------------------------------------
# DOCX — stdlib only
# ------------------------------------------------------------
def _docx_text(raw):
    import xml.etree.ElementTree as ET

    zf = zipfile.ZipFile(io.BytesIO(raw))
    parts = [n for n in ("word/document.xml",) if n in zf.namelist()]
    if not parts:
        raise ValueError("no word/document.xml in this .docx")

    out = []
    for part in parts:
        root = ET.fromstring(zf.read(part))
        # A paragraph is a line. Walking <w:t> alone would run every
        # bullet of a job together into one unreadable paragraph, which
        # is exactly the kind of damage that survives a glance.
        for para in root.iter(f"{_W}p"):
            line = []
            for node in para.iter():
                tag = node.tag
                if tag == f"{_W}t":
                    line.append(node.text or "")
                elif tag == f"{_W}tab":
                    line.append("\t")
                elif tag in (f"{_W}br", f"{_W}cr"):
                    line.append("\n")
            out.append("".join(line))
    return "\n".join(out)


# ------------------------------------------------------------
# PDF — pypdf, optional
# ------------------------------------------------------------
def pdf_available():
    try:
        import pypdf                                    # noqa: F401
        return True
    except ImportError:
        return False


def _pdf_text(raw):
    """(text, note). Raises ValueError with something a person can act on."""
    try:
        import pypdf
    except ImportError:
        raise ValueError(
            "PDF support needs one package: "
            "py -3.11 -m pip install pypdf") from None

    try:
        reader = pypdf.PdfReader(io.BytesIO(raw))
    except Exception as e:                              # noqa: BLE001
        raise ValueError(f"that PDF could not be opened ({e})") from None

    if reader.is_encrypted:
        # An empty user password is common on "protected" CVs and costs
        # nothing to try.
        try:
            if not reader.decrypt(""):
                raise ValueError(
                    "that PDF is password protected. Save an unprotected "
                    "copy and upload that.")
        except ValueError:
            raise
        except Exception:                               # noqa: BLE001
            raise ValueError(
                "that PDF is password protected. Save an unprotected "
                "copy and upload that.") from None

    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:                               # noqa: BLE001
            pages.append("")

    text = _strip_repeated_furniture(pages)
    note = None
    if len(text.strip()) < MIN_CHARS and len(reader.pages):
        # Almost certainly a scan: a picture of a CV has no text layer.
        # Saying so beats saving forty characters and letting the scorer
        # judge this person on nothing.
        note = ("that PDF has almost no selectable text in it, which "
                "usually means it is a scan or an export of images. "
                "Upload a PDF you can select text in, or a .docx.")
    return text, note


# A running header sits in the first line or two of a page and a footer
# in the last. Repetition ALONE is not enough to identify one: measured
# on a two-page CV, "appears on every page" also matched EXPERIENCE,
# EDUCATION, SKILLS, the degree line and a whole job, and threw away 57%
# of the document — including three of the seven facts that made the
# person employable. Position is what separates furniture from content.
# ONE line, not two. With two, a four-line page has no middle at all,
# so the bands cover everything and a section heading sitting second
# from the top is read as furniture. A running header is almost always
# the very first line. Losing a header is cheap; losing a job is not.
_EDGE_LINES = 1


def _strip_repeated_furniture(pages):
    """Drop running headers, footers and page numbers. Nothing else.

    A header repeated on three pages is harmless to read and actively
    damaging to keep: it eats the 6,000 characters score.py is given and
    repeats itself inside the prompt. But a section heading is also
    repeated, and losing that costs far more than the header does, so a
    line is only ever furniture if it appears at the EDGE of its page —
    never in the body, however often it recurs.
    """
    if len(pages) < 2:
        return "\n".join(pages)

    edges, bodies = {}, set()
    for page in pages:
        lines = [ln.strip() for ln in page.splitlines() if ln.strip()]
        if not lines:
            continue
        if len(lines) < 2 * _EDGE_LINES + 2:
            # Too short for "edge" to mean anything; it is all body.
            bodies.update(lines)
            continue
        for line in set(lines[:_EDGE_LINES] + lines[-_EDGE_LINES:]):
            edges[line] = edges.get(line, 0) + 1
        bodies.update(lines[_EDGE_LINES:-_EDGE_LINES])

    # Repeated on most pages, always at an edge, never in a body, and
    # short enough to be a header rather than a sentence of experience.
    threshold = max(2, len(pages) - 1)
    furniture = {ln for ln, n in edges.items()
                 if n >= threshold and ln not in bodies and len(ln) < 90}

    kept, dropped = [], 0
    for page in pages:
        lines = page.splitlines()
        edge_idx = set(range(min(_EDGE_LINES, len(lines)))) | \
            set(range(max(0, len(lines) - _EDGE_LINES), len(lines)))
        for i, line in enumerate(lines):
            bare = line.strip()
            if bare and i in edge_idx and (bare in furniture
                                           or _PAGE_NO.match(bare)):
                dropped += 1
                continue
            kept.append(line)
    return "\n".join(kept)


# ------------------------------------------------------------
# RTF — enough of it
# ------------------------------------------------------------
_RTF_CMD = re.compile(r"\\par[d]?\b|\\line\b|\\[a-z]+-?\d*\s?|[{}]", re.I)
_RTF_HEX = re.compile(r"\\'([0-9a-f]{2})", re.I)


def _rtf_text(raw):
    s = raw.decode("latin-1", errors="replace")
    s = _RTF_HEX.sub(lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"\\par[d]?\b|\\line\b", "\n", s, flags=re.I)
    s = _RTF_CMD.sub(" ", s)
    return s


# ------------------------------------------------------------
def tidy(text):
    """Normalise spacing without destroying line structure.

    The lines ARE the information: the tailor is asked to rewrite
    individual bullets and quote them back verbatim, so collapsing a CV
    into one paragraph would take the feature away.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u2022", "- ").replace("\uf0b7", "- ")
    text = "".join(c for c in text if c == "\n" or c == "\t" or c >= " ")
    lines = [_WS_RUN.sub(" ", ln).strip() for ln in text.split("\n")]
    return _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()


def _looks_like_prose(text, sample=4000):
    """Is this actually writing, or a binary file that decoded?

    A CV is overwhelmingly letters and spaces. Anything that is mostly
    replacement characters, control bytes or punctuation got here by
    accident.
    """
    head = text[:sample]
    if not head:
        return False
    letters = sum(c.isalpha() or c.isspace() for c in head)
    return (letters / len(head)) > 0.75 and head.count("\ufffd") < len(head) * 0.02


def extract(raw, filename=""):
    """
    (text, note) from an uploaded CV of any supported kind.

    `note` is a warning worth showing the user even on success — a PDF
    that yielded suspiciously little, say. Raises ValueError with a
    message written for the person who uploaded the file, not for a log.
    """
    if not raw:
        raise ValueError("that file was empty.")
    if len(raw) > MAX_BYTES:
        raise ValueError(
            f"that file is {len(raw) / 1e6:.1f}MB. A CV should be well "
            f"under {MAX_BYTES // 1_000_000}MB — this is probably not one.")

    kind = sniff(raw)
    note = None

    if kind == "pdf":
        text, note = _pdf_text(raw)
    elif kind == "docx":
        text = _docx_text(raw)
    elif kind == "rtf":
        text = _rtf_text(raw)
    elif kind == "doc":
        raise ValueError(
            "that is a legacy Word .doc file, which needs a converter "
            "this tool does not carry. Open it in Word or Google Docs "
            "and save as .docx or PDF.")
    elif kind == "odt":
        raise ValueError(
            "that is an OpenDocument .odt file. Export it as PDF or "
            ".docx and upload that.")
    elif kind == "zip":
        raise ValueError(
            "that is a zip archive, not a document. Upload the CV "
            "itself.")
    elif kind == "unknown":
        # A broken zip used to land here and be decoded as text, which
        # accepted 282 characters of "PKgarbagegarbage..." as somebody's
        # CV. A file we cannot identify is not a file we should guess at.
        raise ValueError(
            "that file is damaged, or is not a document this tool can "
            "read. Upload a PDF, .docx, .rtf or .txt.")
    else:
        text = raw.decode("utf-8", errors="replace")
        if not _looks_like_prose(text):
            raise ValueError(
                "that file does not appear to contain readable text. "
                "Upload a PDF, .docx, .rtf or .txt.")

    text = tidy(text)

    if len(text) < MIN_CHARS:
        raise ValueError(
            note or
            f"only {len(text)} characters of text came out of that file. "
            f"A CV should be a few thousand. Try a PDF you can select "
            f"text in, or a .docx.")
    return text, note
