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


# A letter body that passes these reads like a letter, not a list of "you ask X, I did X" matches. Career-office
# guidance (one page, 3-5 paragraphs, about 250-400 words, a specific reason for this company, the story behind one
# achievement rather than a restated resume line, a close that asks for the interview) turned into rules a machine
# can check. They judge shape, not truth: unsupported_numbers in tailoring_service still guards the facts.
MIN_WORDS, MAX_WORDS = 250, 450
MIN_PARAGRAPHS, MAX_PARAGRAPHS = 4, 5
STORY_MIN_WORDS = 70
_STOCK_PHRASES = (
    "i am applying for", "i'm applying for", "i am writing to apply", "i am excited to apply",
    "the posting asks", "the posting describes", "the posting names", "the job description asks", "the role asks for",
    "please find my resume", "please find attached", "i work the same way", "that is the kind of work i do",
    "i believe i would be a great fit", "i am confident that i", "to whom it may concern",
)
_ASK_RE = re.compile(r"\b(interview|conversation|talk|discuss|speak|chat|meet)\b", re.IGNORECASE)
_NUMBER_IN_TEXT_RE = re.compile(r"\d")
_LEGAL_SUFFIX_RE = re.compile(r",?\s+(inc\.?|llc|l\.l\.c\.|ltd\.?|corp\.?|corporation|co\.|company|holdings|lp|llp)$",
                              re.IGNORECASE)


_GENERIC_TAIL_RE = re.compile(r"(\s+(services|technologies|technology|solutions|systems|group|labs|industries|university|"
                              r"software|international))+$", re.IGNORECASE)


def _company_names(company_name: str) -> list[str]:
    """Ways a letter can name the company: the full name, without its legal suffix or a parenthetical
    ("Amazon Web Services (AWS)" -> also "Amazon Web Services", "AWS"), and without a leading "The"."""
    name = (company_name or "").strip()
    names = {name}
    paren = re.search(r"\(([^)]+)\)", name)
    if paren:
        names.add(paren.group(1).strip())
        name = re.sub(r"\s*\([^)]*\)", "", name).strip()
        names.add(name)
    bare = _LEGAL_SUFFIX_RE.sub("", name).strip()
    bare = re.sub(r"\.(com|io|ai|co)(?=\s|$)", "", bare, flags=re.IGNORECASE).strip()  # "Amazon.com Services" -> "Amazon Services"
    if bare.lower().startswith("the "):
        bare = bare[4:]
    names.add(bare)
    # "Amazon Services", "The Johns Hopkins University", "CHAOS Industries": letters say "Amazon", "Johns Hopkins", "CHAOS".
    short = _GENERIC_TAIL_RE.sub("", bare).strip()
    if short and short != bare:
        names.add(short)
    return [n for n in names if len(n) >= 2]


def quality_issues(body: str, company_name: str) -> list[str]:
    """Plain-language reasons a letter body is not ready, empty when it is."""
    paragraphs = body_paragraphs(body)
    text = "\n\n".join(paragraphs)
    lower = text.lower()
    issues = []
    words = len(text.split())
    if words < MIN_WORDS:
        issues.append(f"too short ({words} words, needs at least {MIN_WORDS})")
    elif words > MAX_WORDS:
        issues.append(f"too long ({words} words, keep it under {MAX_WORDS})")
    if not MIN_PARAGRAPHS <= len(paragraphs) <= MAX_PARAGRAPHS:
        issues.append(f"{len(paragraphs)} paragraphs (needs {MIN_PARAGRAPHS}-{MAX_PARAGRAPHS})")
    variants = sorted(_company_names(company_name), key=len, reverse=True)  # longest first: one mention counts once
    mentions = len(re.findall(r"(?<!\w)(?:" + "|".join(re.escape(n.lower()) for n in variants) + r")(?!\w)", lower)) \
        if variants else 0
    if mentions < 2:
        issues.append(f"names {company_name or 'the company'} {mentions} time(s); say why this company, not any company")
    stock = [p for p in _STOCK_PHRASES if p in lower]
    if stock:
        issues.append("stock phrasing: " + ", ".join(f'"{p}"' for p in stock))
    if not any(len(p.split()) >= STORY_MIN_WORDS and _NUMBER_IN_TEXT_RE.search(p) for p in paragraphs[1:-1] or paragraphs):
        issues.append(f"no story paragraph (one body paragraph of {STORY_MIN_WORDS}+ words telling one piece of work "
                      "and its measured result)")
    if paragraphs and not _ASK_RE.search(paragraphs[-1]):
        issues.append("last paragraph does not ask for an interview or conversation")
    return issues


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
