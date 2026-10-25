"""Everything around a cover letter's body: the contact header, date, greeting and sign-off, and the Word and plain-text
copies of both tailored documents.

The body is stored on its own (tailoring_service.clean_cover_letter strips any greeting or sign-off the model writes), so
every format that leaves this app -- the PDF, the Word file, the plain text, and the extension's paste into an application
form -- gets the same frame from here. A letter is addressed to the application's recruiter_name when one is saved, and
to "Hiring Manager" otherwise; it never guesses a name.

Plain text exists because many application forms take the letter or resume as pasted text, not a file."""

import io
import re
from datetime import date

from docx import Document
from docx.shared import Pt

from . import docx_generator, resume_rules

DEFAULT_ADDRESSEE = "Hiring Manager"
CLOSING = "Sincerely,"


def greeting(recipient_name: str | None) -> str:
    name = (recipient_name or "").strip()
    return f"Dear {name or DEFAULT_ADDRESSEE},"


def contact_line(contact: dict | None) -> str:
    contact = contact or {}
    return " | ".join(resume_rules.contact_info_parts(contact) + resume_rules.contact_link_values(contact))


def letter_date(today: date | None = None) -> str:
    d = today or date.today()
    return f"{d.strftime('%B')} {d.day}, {d.year}"


_OWN_GREETING_RE = re.compile(r"^(dear|hello|hi|to whom)\b.{0,80}$", re.IGNORECASE)
_OWN_CLOSING_RE = re.compile(r"^(sincerely|best regards|kind regards|warm regards|regards|respectfully|best|thank you)[,.!]?$",
                             re.IGNORECASE)


def letter_parts(body: str, recipient_name: str | None = None) -> tuple[str, list[str]]:
    """(greeting line, body paragraphs). Letters written before the frame existed, or by hand, carry their own
    greeting and sign-off: the sign-off is dropped so it is not printed twice, and their own greeting ("Dear Loenbro
    Hiring Team,") is kept unless an addressee has been saved for the application."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body or "") if p.strip()]
    own_greeting = None
    if paragraphs:
        first, _, rest = paragraphs[0].partition("\n")
        if _OWN_GREETING_RE.match(first.strip()):
            own_greeting = first.strip()
            paragraphs = ([rest.strip()] if rest.strip() else []) + paragraphs[1:]
    for i, paragraph in enumerate(paragraphs):
        if _OWN_CLOSING_RE.match(paragraph.splitlines()[0].strip()):
            paragraphs = paragraphs[:i]
            break
    if (recipient_name or "").strip() or not own_greeting:
        return greeting(recipient_name), paragraphs
    return own_greeting, paragraphs


def body_paragraphs(body: str) -> list[str]:
    return letter_parts(body)[1]


def full_text(body: str, candidate_name: str, recipient_name: str | None = None) -> str:
    """Greeting, body and sign-off: what gets pasted into an application form's cover-letter box."""
    salutation, paragraphs = letter_parts(body, recipient_name)
    return "\n\n".join([salutation] + paragraphs + [f"{CLOSING}\n{candidate_name or ''}".rstrip()])


def plain_text(body: str, candidate_name: str, contact: dict | None = None, recipient_name: str | None = None,
               today: date | None = None) -> str:
    header = [line for line in (candidate_name, contact_line(contact)) if line]
    return "\n".join(header + ["", letter_date(today), "", full_text(body, candidate_name, recipient_name)]) + "\n"


def build_docx(body: str, candidate_name: str, contact: dict | None = None, recipient_name: str | None = None,
               today: date | None = None) -> Document:
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    name = doc.add_paragraph()
    run = name.add_run(candidate_name or "")
    run.bold = True
    run.font.size = Pt(16)
    line = contact_line(contact)
    if line:
        doc.add_paragraph(line)
    doc.add_paragraph(letter_date(today))
    salutation, paragraphs = letter_parts(body, recipient_name)
    doc.add_paragraph(salutation)
    for paragraph in paragraphs:
        doc.add_paragraph(paragraph)
    doc.add_paragraph(CLOSING)
    doc.add_paragraph(candidate_name or "")
    return doc


def docx_bytes(document: Document) -> bytes:
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def resume_plain_text(resume_doc: dict) -> str:
    """The tailored resume as plain text, built from the same Word document the DOCX download uses, so the sections,
    order and rules (certificates, no work-authorization line) are identical."""
    document = docx_generator.build_resume_docx(resume_doc)
    lines: list[str] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        style = (paragraph.style.name or "").lower()
        if style.startswith("heading") or style == "title":
            lines += ["", text.upper()]
        elif "list" in style:
            lines.append(f"- {text}")
        else:
            lines.append(text)
    return "\n".join(lines).strip() + "\n"
