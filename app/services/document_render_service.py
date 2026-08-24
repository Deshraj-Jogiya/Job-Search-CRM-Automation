"""
Renders a TailoredDocument's structured
content into an actual PDF file. Application forms (Greenhouse, Lever,
Ashby) take a resume/cover-letter file upload, not pasted JSON text --
this is the missing step between "tailored content exists in the
database" and "there's a file to attach to a real application."

Kept deliberately simple (reportlab's Platypus flow API, no exotic
layout) -- this needs to render reliably across arbitrary profile
content, not win a design award.

Every dynamic string interpolated into a Paragraph() below is escaped
with _esc() first. reportlab's Paragraph parses its input as a
constrained XML-like markup (it recognizes <b>, <i>, <font>, <a href="">,
and notably <img src="".../>, which it will actually fetch at PDF-build
time) -- this content ultimately traces back to an LLM's tailoring pass
grounded partly in a scraped, external, adversarial-by-default job
description. Unescaped, a JD containing prompt-injection markup that the
LLM reproduces near-verbatim could make PDF generation itself issue a
real outbound fetch server-side, or simply crash the render on any
literal '&' or '<' in ordinary content (e.g. "C&A frameworks"). The
handful of tags this module inserts itself (<b>, <i>, &mdash;, &bull;,
&nbsp;, <br/>) are written directly into the f-strings below, never
through _esc(), so they still render as real markup.
"""

import io
import json
from xml.sax.saxutils import escape as _esc

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_styles = getSampleStyleSheet()

_name_style = ParagraphStyle("NameStyle", parent=_styles["Title"], fontSize=18, spaceAfter=2)
_contact_style = ParagraphStyle(
    "ContactStyle", parent=_styles["Normal"], fontSize=9, textColor=colors.grey,
    spaceAfter=10, alignment=TA_CENTER,
)
_section_style = ParagraphStyle(
    "SectionStyle", parent=_styles["Heading2"], fontSize=11, spaceBefore=12, spaceAfter=2,
    textColor=colors.HexColor("#1a1a1a"), borderPadding=0,
)
_role_style = ParagraphStyle("RoleStyle", parent=_styles["Normal"], fontSize=10.5, fontName="Helvetica-Bold", spaceAfter=1)
_role_italic_style = ParagraphStyle("RoleItalicStyle", parent=_styles["Normal"], fontSize=10, fontName="Helvetica-Oblique", spaceAfter=1)
_meta_style = ParagraphStyle("MetaStyle", parent=_styles["Normal"], fontSize=9, textColor=colors.grey, spaceAfter=3)
_meta_right_style = ParagraphStyle("MetaRightStyle", parent=_meta_style, alignment=TA_RIGHT)
_body_style = ParagraphStyle("BodyStyle", parent=_styles["Normal"], fontSize=9.5, leading=13, spaceAfter=8)
_bullet_style = ParagraphStyle("BulletStyle", parent=_styles["Normal"], fontSize=9.5, leading=13, leftIndent=14, spaceAfter=2)

_CONTENT_WIDTH = letter[0] - 1.4 * inch  # page width minus left+right margins (0.7in each)

_TWO_COL_TABLE_STYLE = TableStyle([
    ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ("TOPPADDING", (0, 0), (-1, -1), 0),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
])


def _section_header(title: str) -> list:
    """Uppercase heading + a thin rule underneath, matching the
    résumé format Deshraj specifically asked to match (see the two
    example PDFs reviewed for this pass) -- plain colored Heading2 text
    alone read as noticeably plainer than that reference."""
    return [
        Paragraph(title.upper(), _section_style),
        HRFlowable(width="100%", thickness=0.75, color=colors.HexColor("#1a1a1a"), spaceAfter=4),
    ]


def _two_col_row(left, right, left_style, right_style) -> Table:
    """A left-aligned cell and a right-aligned cell sharing one line
    (company + location, role + dates, degree + GPA) -- the reference
    format's defining structural trait, and something a plain
    Paragraph can't do without tab stops or a table."""
    table = Table(
        [[Paragraph(_esc(left or ""), left_style), Paragraph(_esc(right or ""), right_style)]],
        colWidths=[_CONTENT_WIDTH * 0.65, _CONTENT_WIDTH * 0.35],
    )
    table.setStyle(_TWO_COL_TABLE_STYLE)
    return table


_KNOWN_ACRONYMS = {"ai", "llm", "llms", "bi", "ml", "etl", "elt", "sql", "ui", "ux", "api", "qa"}


def _humanize_skill_category(category: str) -> str:
    words = category.replace("_", " ").split()
    return " ".join(w.upper() if w.lower() in _KNOWN_ACRONYMS else w.capitalize() for w in words)


def _contact_lines(contact: dict) -> list[str]:
    """Info (location/phone/email) and links (LinkedIn/GitHub/portfolio
    URLs) as two separate centered lines rather than one long run --
    matches the reference format, and reads better once a candidate has
    both a phone number and 2-3 URLs."""
    values = [str(v) for v in (contact or {}).values() if v]
    info = [v for v in values if not v.startswith("http")]
    links = [v for v in values if v.startswith("http")]
    sep = " &nbsp;|&nbsp; "
    return [sep.join(_esc(v) for v in group) for group in (info, links) if group]


def render_resume_pdf(resume_content: dict) -> bytes:
    if isinstance(resume_content, str):
        resume_content = json.loads(resume_content)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
    )
    flow = [Paragraph(_esc(resume_content.get("name") or ""), _name_style)]
    flow += [Paragraph(line, _contact_style) for line in _contact_lines(resume_content.get("contact") or {})]

    if resume_content.get("summary"):
        flow += _section_header("Professional Summary")
        flow.append(Paragraph(_esc(resume_content["summary"]), _body_style))

    skills = resume_content.get("skills") or {}
    if skills:
        flow += _section_header("Technical Skills")
        for category, items in skills.items():
            label = _esc(_humanize_skill_category(category))
            flow.append(Paragraph(f"<b>{label}:</b> {_esc(', '.join(items))}", _body_style))

    experience = resume_content.get("experience") or []
    if experience:
        flow += _section_header("Professional Experience")
        for job in experience:
            flow.append(_two_col_row(job.get("company"), job.get("location"), _role_style, _meta_right_style))
            flow.append(_two_col_row(job.get("role"), job.get("date"), _role_italic_style, _meta_right_style))
            flow.append(Spacer(1, 2))
            for bullet in job.get("bullets", []):
                flow.append(Paragraph(f"&bull; {_esc(bullet)}", _bullet_style))
            flow.append(Spacer(1, 4))

    projects = resume_content.get("projects") or []
    if projects:
        flow += _section_header("Key Projects")
        for proj in projects:
            tech = ", ".join(proj.get("technologies") or [])
            flow.append(_two_col_row(proj.get("name"), tech, _role_style, _meta_right_style))
            flow.append(Spacer(1, 2))
            for bullet in proj.get("bullets", []):
                flow.append(Paragraph(f"&bull; {_esc(bullet)}", _bullet_style))
            flow.append(Spacer(1, 4))

    education = resume_content.get("education") or []
    if education:
        flow += _section_header("Education")
        for edu in education:
            flow.append(_two_col_row(edu.get("school"), edu.get("location"), _role_style, _meta_right_style))
            flow.append(_two_col_row(edu.get("degree"), edu.get("date"), _role_italic_style, _meta_right_style))
            flow.append(Spacer(1, 4))

    certifications = resume_content.get("certifications") or []
    if certifications:
        flow += _section_header("Certifications")
        for cert in certifications:
            flow.append(Paragraph(f"&bull; {_esc(cert)}", _bullet_style))

    doc.build(flow)
    return buf.getvalue()


def render_cover_letter_pdf(cover_letter_text: str, candidate_name: str) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
    )
    flow = [Paragraph(_esc(candidate_name or ""), _name_style), Spacer(1, 14)]
    for paragraph in (cover_letter_text or "").split("\n\n"):
        paragraph = paragraph.strip()
        if paragraph:
            # Escape first, then insert our own literal <br/> markup --
            # doing it in this order means the <br/> tags we add can
            # never themselves get escaped away.
            flow.append(Paragraph(_esc(paragraph).replace("\n", "<br/>"), _body_style))
            flow.append(Spacer(1, 8))
    doc.build(flow)
    return buf.getvalue()
