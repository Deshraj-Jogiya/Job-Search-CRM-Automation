"""
Phase 12 (auto-apply support): renders a TailoredDocument's structured
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
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

_styles = getSampleStyleSheet()

_name_style = ParagraphStyle("NameStyle", parent=_styles["Title"], fontSize=18, spaceAfter=2)
_contact_style = ParagraphStyle("ContactStyle", parent=_styles["Normal"], fontSize=9, textColor=colors.grey, spaceAfter=10)
_section_style = ParagraphStyle(
    "SectionStyle", parent=_styles["Heading2"], fontSize=12, spaceBefore=12, spaceAfter=4,
    textColor=colors.HexColor("#1a1a1a"), borderPadding=0,
)
_role_style = ParagraphStyle("RoleStyle", parent=_styles["Normal"], fontSize=10.5, fontName="Helvetica-Bold", spaceAfter=1)
_meta_style = ParagraphStyle("MetaStyle", parent=_styles["Normal"], fontSize=9, textColor=colors.grey, spaceAfter=3)
_body_style = ParagraphStyle("BodyStyle", parent=_styles["Normal"], fontSize=9.5, leading=13, spaceAfter=8)
_bullet_style = ParagraphStyle("BulletStyle", parent=_styles["Normal"], fontSize=9.5, leading=13, leftIndent=14, spaceAfter=2)


_KNOWN_ACRONYMS = {"ai", "llm", "llms", "bi", "ml", "etl", "elt", "sql", "ui", "ux", "api", "qa"}


def _humanize_skill_category(category: str) -> str:
    words = category.replace("_", " ").split()
    return " ".join(w.upper() if w.lower() in _KNOWN_ACRONYMS else w.capitalize() for w in words)


def _contact_line(contact: dict) -> str:
    parts = [v for v in (contact or {}).values() if v]
    return " &nbsp;|&nbsp; ".join(_esc(str(p)) for p in parts)


def render_resume_pdf(resume_content: dict) -> bytes:
    if isinstance(resume_content, str):
        resume_content = json.loads(resume_content)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
    )
    flow = [
        Paragraph(_esc(resume_content.get("name") or ""), _name_style),
        Paragraph(_esc(resume_content.get("title") or ""), _contact_style),
        Paragraph(_contact_line(resume_content.get("contact") or {}), _contact_style),
    ]

    if resume_content.get("summary"):
        flow.append(Paragraph("Summary", _section_style))
        flow.append(Paragraph(_esc(resume_content["summary"]), _body_style))

    skills = resume_content.get("skills") or {}
    if skills:
        flow.append(Paragraph("Skills", _section_style))
        for category, items in skills.items():
            label = _esc(_humanize_skill_category(category))
            flow.append(Paragraph(f"<b>{label}:</b> {_esc(', '.join(items))}", _body_style))

    experience = resume_content.get("experience") or []
    if experience:
        flow.append(Paragraph("Experience", _section_style))
        for job in experience:
            flow.append(Paragraph(f"{_esc(job.get('role', ''))} &mdash; {_esc(job.get('company', ''))}", _role_style))
            meta = " | ".join(_esc(x) for x in (job.get("location"), job.get("date")) if x)
            if meta:
                flow.append(Paragraph(meta, _meta_style))
            for bullet in job.get("bullets", []):
                flow.append(Paragraph(f"&bull; {_esc(bullet)}", _bullet_style))
            flow.append(Spacer(1, 4))

    projects = resume_content.get("projects") or []
    if projects:
        flow.append(Paragraph("Projects", _section_style))
        for proj in projects:
            flow.append(Paragraph(_esc(proj.get("name", "")), _role_style))
            for bullet in proj.get("bullets", []):
                flow.append(Paragraph(f"&bull; {_esc(bullet)}", _bullet_style))
            if proj.get("technologies"):
                flow.append(Paragraph(f"<i>{_esc(', '.join(proj['technologies']))}</i>", _meta_style))
            flow.append(Spacer(1, 4))

    education = resume_content.get("education") or []
    if education:
        flow.append(Paragraph("Education", _section_style))
        for edu in education:
            line = " &mdash; ".join(_esc(x) for x in (edu.get("degree"), edu.get("school")) if x)
            flow.append(Paragraph(line, _role_style))
            if edu.get("date"):
                flow.append(Paragraph(_esc(edu["date"]), _meta_style))

    certifications = resume_content.get("certifications") or []
    if certifications:
        flow.append(Paragraph("Certifications", _section_style))
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
