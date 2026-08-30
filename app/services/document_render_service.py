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
handful of tags this module inserts itself (<b>, <i>, &mdash;, &nbsp;,
<br/>) are written directly into the f-strings below, never through
_esc(), so they still render as real markup.

Bullets use a plain ASCII "-" as the bullet character (via reportlab's
real bulletText mechanism, not typed inline), not a Unicode bullet --
confirmed via pdfplumber that reportlab's base-14 Helvetica font has no
usable ToUnicode mapping for &bull;, so every bullet point extracted as
a garbled (cid:N) glyph reference instead of real text. Renders
identically either way; a hyphen extracts cleanly on every ATS parser
instead of gambling on font-encoding support none of them need to have.
"""

import io
import json
from xml.sax.saxutils import escape as _esc

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_styles = getSampleStyleSheet()

_name_style = ParagraphStyle("NameStyle", parent=_styles["Title"], fontSize=16, spaceAfter=0)
# Every content style below has spaceBefore=spaceAfter=0, no exceptions.
# Real bug (2026-08-29, second pass): the first uniform-spacing fix added
# one consistent Spacer before every section title, but each style STILL
# carried its own leftover spaceAfter (_body_style 3.5, _bullet_style
# 0.3) -- and reportlab does NOT collapse an explicit Spacer with a
# neighboring paragraph's spaceAfter the way two paragraphs' spaceBefore/
# spaceAfter collapse with each other; it just adds on top. Measured with
# pdfplumber (not assumed): section titles preceded by a body_style line
# landed ~20pt below it, titles preceded by a bullet_style line landed
# ~17pt below it, purely from that leftover 3.2pt difference in the
# invisible style, not from anything visible in the content. Every
# paragraph in this document now contributes exactly zero automatic
# spacing -- render_resume_pdf's SECTION_GAP/ENTRY_GAP/LINE_GAP spacers
# are the only source of vertical rhythm anywhere in the page, so there
# is nothing left that can vary by which style happens to trail.
_contact_style = ParagraphStyle(
    "ContactStyle", parent=_styles["Normal"], fontSize=8.3, leading=10,
    textColor=colors.grey, spaceAfter=0, alignment=TA_CENTER,
)
_section_style = ParagraphStyle(
    "SectionStyle", parent=_styles["Heading2"], fontSize=10, leading=12, spaceBefore=0, spaceAfter=0,
    textColor=colors.HexColor("#1a1a1a"), borderPadding=0,
)
_role_style = ParagraphStyle(
    "RoleStyle", parent=_styles["Normal"], fontSize=9.7, leading=11.5,
    fontName="Helvetica-Bold", spaceBefore=0, spaceAfter=0,
)
_role_italic_style = ParagraphStyle(
    "RoleItalicStyle", parent=_styles["Normal"], fontSize=9.2, leading=11,
    fontName="Helvetica-Oblique", spaceBefore=0, spaceAfter=0,
)
_meta_style = ParagraphStyle(
    "MetaStyle", parent=_styles["Normal"], fontSize=8.3, leading=11, textColor=colors.grey, spaceAfter=0,
)
_meta_right_style = ParagraphStyle("MetaRightStyle", parent=_meta_style, alignment=TA_RIGHT)
_body_style = ParagraphStyle("BodyStyle", parent=_styles["Normal"], fontSize=8.7, leading=10.3, spaceAfter=0)
# bulletIndent < leftIndent is what actually produces a hanging indent --
# the bullet character sits at bulletIndent, the paragraph's own text
# (first line AND every wrapped continuation line) starts at leftIndent.
# This style used to be paired with a literal "- " typed into the bullet
# text instead of reportlab's real bulletText mechanism -- that put
# "- " and the text on the exact same left edge for every line, so a
# bullet that wrapped to a second line had its continuation flush with
# the dash instead of hanging under the actual text start, reading as
# visibly uneven/misaligned wherever a bullet ran past one line (which
# is most of them at this content density). See every Paragraph(...,
# bulletText="-") call below -- this style alone doesn't fix anything
# unless paired with that.
_bullet_style = ParagraphStyle(
    "BulletStyle", parent=_styles["Normal"], fontSize=8.7, leading=10.3,
    leftIndent=13, bulletIndent=0, spaceAfter=0,
)

_CONTENT_WIDTH = letter[0] - 1.1 * inch  # page width minus left+right margins (0.55in each)

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
        HRFlowable(width="100%", thickness=0.75, color=colors.HexColor("#1a1a1a"), spaceAfter=2),
    ]


def _two_col_row(left, right, left_style, right_style, left_link: str = None) -> Table:
    """A left-aligned cell and a right-aligned cell sharing one line
    (company + location, role + dates, degree + GPA) -- the reference
    format's defining structural trait, and something a plain
    Paragraph can't do without tab stops or a table.

    left_link (only ever passed for a project's name row) appends a
    clickable "| GitHub" -- reportlab's Paragraph markup supports
    <a href=""> natively (see this module's docstring for why <img> is
    the actually-dangerous tag, not this one), so this is a real
    clickable link in the rendered PDF, not just text. The URL itself
    still goes through _esc() before being interpolated into the href
    attribute, same discipline as every other dynamic string here."""
    left_text = _esc(left or "")
    if left_link:
        left_text += f' &nbsp;|&nbsp; <a href="{_esc(left_link)}"><font color="#2563eb"><u>GitHub</u></font></a>'
    table = Table(
        [[Paragraph(left_text, left_style), Paragraph(_esc(right or ""), right_style)]],
        colWidths=[_CONTENT_WIDTH * 0.65, _CONTENT_WIDTH * 0.35],
    )
    table.setStyle(_TWO_COL_TABLE_STYLE)
    return table


_KNOWN_ACRONYMS = {"ai", "llm", "llms", "bi", "ml", "etl", "elt", "sql", "ui", "ux", "api", "qa"}

# A generic word-by-word capitalize/uppercase pass reads as visibly
# broken for real category keys -- "llms" fully uppercased ("LLMS")
# isn't how anyone writes it (LLMs), and a key with no underscore
# standing in for "and" renders as a run-on noun pile ("Devops Tooling
# Analytics", "Data Engineering Cloud Platforms") instead of a real
# section title. Real user feedback (2026-08-29): this read as an
# unpolished, obviously-generated label on an otherwise clean resume.
# Explicit overrides for the category keys this app's own seed profile
# actually uses; falls through to the generic heuristic below for any
# other fork's differently-named categories rather than assuming this
# exact key set.
_CATEGORY_TITLE_OVERRIDES = {
    "generative_ai_llms_agentic_frameworks": "Generative AI, LLMs & Agentic Frameworks",
    "machine_learning_deep_learning": "Machine Learning & Deep Learning",
    "data_engineering_cloud_platforms": "Data Engineering & Cloud Platforms",
    "full_stack_software_engineering": "Full-Stack Software Engineering",
    "devops_tooling_analytics": "DevOps, Tooling & Analytics",
}


def _humanize_skill_category(category: str) -> str:
    override = _CATEGORY_TITLE_OVERRIDES.get(category.lower())
    if override:
        return override
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

    # Every paragraph style used in this function has spaceBefore=
    # spaceAfter=0 (see the style definitions above) -- ALL vertical
    # rhythm on the page comes from exactly these three named gaps,
    # nothing else. Second real bug on this same issue (2026-08-29):
    # the first uniform-spacing pass added one consistent Spacer before
    # every section title, but each style still carried its own leftover
    # spaceAfter, and reportlab does NOT collapse an explicit Spacer with
    # a neighboring paragraph's spaceAfter the way two paragraphs'
    # spaceBefore/spaceAfter collapse with each other -- it just adds on
    # top. Measured with pdfplumber (not assumed): section titles
    # preceded by a body_style line landed ~20pt below it, titles
    # preceded by a bullet_style line landed ~17pt below it, purely from
    # the 3.2pt difference in each style's own invisible spaceAfter, with
    # nothing different visible in the actual content. Zeroing every
    # style's own spacing and inserting these three gaps explicitly,
    # every time, is what actually makes the rhythm identical everywhere
    # -- there's nothing left that can vary by which style happens to
    # trail a section.
    SECTION_GAP = Spacer(1, 8)  # before every section title, always
    ENTRY_GAP = Spacer(1, 5)    # between entries within a section (jobs, projects, degrees)
    LINE_GAP = Spacer(1, 2)     # between individual lines within one block (bullets, skill categories, cert lines, contact lines)

    def _entries(items):
        """Yields (is_first, item) -- callers prepend ENTRY_GAP for
        every item except the first, so spacing between entries within
        a section is identical everywhere this is used."""
        for i, item in enumerate(items):
            yield i == 0, item

    def _lines(items):
        """Same as _entries but for LINE_GAP -- a separate helper (not
        reused) so a future change to entry vs. line spacing can't
        accidentally couple the two."""
        for i, item in enumerate(items):
            yield i == 0, item

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=0.4 * inch, bottomMargin=0.35 * inch,
        leftMargin=0.55 * inch, rightMargin=0.55 * inch,
    )
    flow = [Paragraph(_esc(resume_content.get("name") or ""), _name_style)]
    for is_first, line in _lines(_contact_lines(resume_content.get("contact") or {})):
        if not is_first:
            flow.append(LINE_GAP)
        flow.append(Paragraph(line, _contact_style))

    if resume_content.get("summary"):
        flow.append(SECTION_GAP)
        flow += _section_header("Professional Summary")
        flow.append(Paragraph(_esc(resume_content["summary"]), _body_style))

    skills = resume_content.get("skills") or {}
    if skills:
        flow.append(SECTION_GAP)
        flow += _section_header("Technical Skills")
        for is_first, (category, items) in _lines(list(skills.items())):
            if not is_first:
                flow.append(LINE_GAP)
            label = _esc(_humanize_skill_category(category))
            flow.append(Paragraph(f"<b>{label}:</b> {_esc(', '.join(items))}", _body_style))

    experience = resume_content.get("experience") or []
    if experience:
        flow.append(SECTION_GAP)
        flow += _section_header("Professional Experience")
        for is_first, job in _entries(experience):
            if not is_first:
                flow.append(ENTRY_GAP)
            # KeepTogether -- real bug found rendering this exact resume:
            # each row/bullet of an entry is its own flowable, so with
            # nothing binding them together reportlab was free to break
            # the page in the middle of one (observed: "Arizona State
            # University" alone at the bottom of page 1, its role/dates/
            # bullets pushed to page 2). A resume entry split across a
            # page boundary reads as broken formatting, not as a content
            # decision -- keep the whole entry atomic; if it doesn't fit
            # in what's left of the page, the WHOLE entry moves down,
            # never part of it.
            entry_flow = [
                _two_col_row(job.get("company"), job.get("location"), _role_style, _meta_right_style),
                _two_col_row(job.get("role"), job.get("date"), _role_italic_style, _meta_right_style),
            ]
            for is_first_bullet, bullet in _lines(job.get("bullets", [])):
                if not is_first_bullet:
                    entry_flow.append(LINE_GAP)
                entry_flow.append(Paragraph(_esc(bullet), _bullet_style, bulletText="-"))
            flow.append(KeepTogether(entry_flow))

    projects = resume_content.get("projects") or []
    if projects:
        flow.append(SECTION_GAP)
        flow += _section_header("Key Projects")
        for is_first, proj in _entries(projects):
            if not is_first:
                flow.append(ENTRY_GAP)
            tech = ", ".join(proj.get("technologies") or [])
            entry_flow = [
                _two_col_row(
                    proj.get("name"), tech, _role_style, _meta_right_style, left_link=proj.get("github_url"),
                ),
            ]
            for is_first_bullet, bullet in _lines(proj.get("bullets", [])):
                if not is_first_bullet:
                    entry_flow.append(LINE_GAP)
                entry_flow.append(Paragraph(_esc(bullet), _bullet_style, bulletText="-"))
            flow.append(KeepTogether(entry_flow))

    education = resume_content.get("education") or []
    if education:
        flow.append(SECTION_GAP)
        flow += _section_header("Education")
        for is_first, edu in _entries(education):
            if not is_first:
                flow.append(ENTRY_GAP)
            flow.append(KeepTogether([
                _two_col_row(edu.get("school"), edu.get("location"), _role_style, _meta_right_style),
                _two_col_row(edu.get("degree"), edu.get("date"), _role_italic_style, _meta_right_style),
            ]))

    certifications = resume_content.get("certifications") or []
    if certifications:
        flow.append(SECTION_GAP)
        flow += _section_header("Certifications")
        for is_first, cert in _lines(certifications):
            if not is_first:
                flow.append(LINE_GAP)
            flow.append(Paragraph(_esc(cert), _bullet_style, bulletText="-"))

    doc.build(flow)
    return buf.getvalue()


_cheat_title_style = ParagraphStyle("CheatTitleStyle", parent=_styles["Title"], fontSize=14, spaceAfter=2)
_cheat_subtitle_style = ParagraphStyle(
    "CheatSubtitleStyle", parent=_styles["Normal"], fontSize=9, textColor=colors.grey, spaceAfter=8,
)
_cheat_round_style = ParagraphStyle(
    "CheatRoundStyle", parent=_styles["Heading3"], fontSize=10.5, spaceBefore=8, spaceAfter=1,
    textColor=colors.HexColor("#1a1a1a"),
)
_cheat_meta_style = ParagraphStyle("CheatMetaStyle", parent=_styles["Normal"], fontSize=7.5, textColor=colors.grey, spaceAfter=3)
# Same hanging-indent fix as _bullet_style above -- bulletIndent < leftIndent
# so a wrapped cue line aligns under the text, not under the dash.
_cheat_cue_style = ParagraphStyle(
    "CheatCueStyle", parent=_styles["Normal"], fontSize=8.5, leading=11,
    leftIndent=10, bulletIndent=0, spaceAfter=2,
)


def render_interview_prep_cheat_sheet_pdf(
    job_title: str, company_name: str, general_prep: dict, company_prep: dict, predicted_rounds: dict,
) -> bytes:
    """Compact quick-reference version of interview prep -- glance-during-
    the-call cues (question + quick_reference), not the full drafted
    answers (those are the in-app study view, deliberately not what
    this prints -- reading a paragraph off a page mid-call reads as
    reading a script, a one-line cue doesn't). Genuinely uncapped
    content (see interview_prep_service.py's docstring) means this may
    run past one physical page for a role with many rounds -- real prep
    isn't trimmed to fit a page count."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
    )
    flow = [
        Paragraph(_esc(f"{job_title} @ {company_name}"), _cheat_title_style),
        Paragraph("Quick-reference cues -- see the app for full drafted answers.", _cheat_subtitle_style),
    ]

    if general_prep and general_prep.get("strengths_to_emphasize"):
        flow.append(Paragraph("STRENGTHS TO EMPHASIZE", _cheat_round_style))
        for s in general_prep["strengths_to_emphasize"]:
            flow.append(Paragraph(_esc(s), _cheat_cue_style, bulletText="-"))

    if general_prep and general_prep.get("potential_gaps_to_address"):
        flow.append(Paragraph("GAPS TO ADDRESS HONESTLY IF RAISED", _cheat_round_style))
        for g in general_prep["potential_gaps_to_address"]:
            flow.append(Paragraph(_esc(g), _cheat_cue_style, bulletText="-"))

    if company_prep and company_prep.get("why_this_company_talking_points"):
        flow.append(Paragraph("WHY THIS COMPANY", _cheat_round_style))
        for t in company_prep["why_this_company_talking_points"]:
            flow.append(Paragraph(_esc(t), _cheat_cue_style, bulletText="-"))

    for round_ in (predicted_rounds or {}).get("rounds", []):
        header = round_.get("round_name", "")
        interviewer = round_.get("likely_interviewer")
        flow.append(Paragraph(_esc(header.upper()), _cheat_round_style))
        if interviewer:
            flow.append(Paragraph(_esc(f"likely run by: {interviewer}"), _cheat_meta_style))
        for qa in round_.get("qa_pairs", []):
            question = qa.get("question", "")
            cue = qa.get("quick_reference") or qa.get("draft_answer", "")[:120]
            flow.append(Paragraph(f"<b>Q:</b> {_esc(question)}", _cheat_cue_style))
            flow.append(Paragraph(f"<b>Cue:</b> {_esc(cue)}", _cheat_cue_style))
        if round_.get("questions_to_ask_them"):
            flow.append(Paragraph("<b>Ask them:</b> " + _esc(" / ".join(round_["questions_to_ask_them"])), _cheat_cue_style))

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
