"""
Part C7: real .docx resume output, ATS-safe by construction. This is
a RENDERER, not a second generator -- it consumes the exact same
resume_doc dict tailor_application() already builds and saves (see
tailoring_service.py), with C3 (skills)/C4 (hedged metrics)/C6
(project selection) already applied there. This module applies C2
(role classification into Experience/Earlier/Credential -- a
structural decision the existing PDF renderer's table-based layout
doesn't have a slot for, so it lives here only, see ARCHITECTURE.md's
note on this) and C8 (work-authorization line, config-only).

ATS-safety is enforced, not just intended:
  - Single column, no tables, no text boxes, no images.
  - No headers/footers -- contact info lives in the document body.
  - Standard section headings (Summary / Technical Skills /
    Professional Experience / Selected Projects / Education /
    Certifications).
  - Bullets use python-docx's real "List Bullet" style (real numbering
    XML under the hood), never a literal "*" character typed into a
    run -- see tests/test_docx_generator.py's structural assertions.
"""

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from . import resume_rules
from .document_render_service import _humanize_skill_category

# Real w:hyperlink XML -- python-docx has no built-in hyperlink support, and a
# plain run of URL text is not clickable in Word. This is the standard
# low-level pattern for it. Real hyperlinks are normal, expected resume
# content and don't affect ATS parsing (unlike tables/text boxes/images,
# which this module's docstring above does deliberately avoid).
_LINK_COLOR_HEX = "2563EB"


def _add_hyperlink(paragraph, url: str, text: str) -> None:
    part = paragraph.part
    r_id = part.relate_to(
        url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )

    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)

    run = OxmlElement("w:r")
    run_props = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), _LINK_COLOR_HEX)
    run_props.append(color)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    run_props.append(underline)
    run.append(run_props)

    text_el = OxmlElement("w:t")
    text_el.text = text
    run.append(text_el)

    hyperlink.append(run)
    paragraph._p.append(hyperlink)

# python-docx's default template carries real, non-zero spacing on
# EVERY paragraph (w:docDefaults -- 10pt space-after, 1.15x line
# spacing) plus a 24pt space-before on Heading 1 and 1in/1.25in page
# margins, none of which build_resume_docx ever overrode -- so every
# one of a real resume's ~35-50 paragraphs silently added its own
# extra vertical gap, compounding into a document that ran to 2 pages
# on content that reads as under a page's worth of text. Found by
# actually opening a generated file, not just checking extracted text
# (which never surfaces layout). Same lesson document_render_service.py's
# PDF renderer already learned the hard way (see its own "zero every
# paragraph style's own spacing" history) -- applied here explicitly,
# every paragraph style below sets space_before/space_after/line_spacing
# itself rather than leaving anything to inherit.
_MARGIN_TOP_IN = 0.35
_MARGIN_BOTTOM_IN = 0.3
_MARGIN_SIDE_IN = 0.55

_BODY_FONT_PT = 10
_NAME_FONT_PT = 16
_HEADING_FONT_PT = 12

_HEADING_SPACE_BEFORE_PT = 6
_HEADING_SPACE_AFTER_PT = 2
_ENTRY_SPACE_AFTER_PT = 3  # after a role/project's meta line and after its last bullet
_BULLET_SPACE_AFTER_PT = 0


def _zero_spacing(paragraph_format, space_after_pt: float = 0) -> None:
    paragraph_format.space_before = Pt(0)
    paragraph_format.space_after = Pt(space_after_pt)
    paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE


def _configure_base_styles(doc: Document) -> None:
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(_BODY_FONT_PT)
    _zero_spacing(normal.paragraph_format, _ENTRY_SPACE_AFTER_PT)

    heading = doc.styles["Heading 1"]
    heading.font.size = Pt(_HEADING_FONT_PT)
    heading.font.color.rgb = RGBColor(0, 0, 0)  # plain black -- Word's default Heading 1 is a themed blue
    _zero_spacing(heading.paragraph_format, _HEADING_SPACE_AFTER_PT)
    heading.paragraph_format.space_before = Pt(_HEADING_SPACE_BEFORE_PT)

    bullet = doc.styles["List Bullet"]
    _zero_spacing(bullet.paragraph_format, _BULLET_SPACE_AFTER_PT)

    section = doc.sections[0]
    section.top_margin = Inches(_MARGIN_TOP_IN)
    section.bottom_margin = Inches(_MARGIN_BOTTOM_IN)
    section.left_margin = Inches(_MARGIN_SIDE_IN)
    section.right_margin = Inches(_MARGIN_SIDE_IN)


def _add_name_and_contact(doc: Document, resume_doc: dict) -> None:
    name_p = doc.add_paragraph()
    name_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    name_run = name_p.add_run(resume_doc.get("name") or "")
    name_run.bold = True
    name_run.font.size = Pt(_NAME_FONT_PT)

    title = resume_doc.get("title")
    if title:
        title_p = doc.add_paragraph(title)
        title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    contact = resume_doc.get("contact") or {}
    contact_parts = [
        contact.get(k) for k in ("email", "phone", "location", "linkedin", "github", "website")
        if contact.get(k)
    ]
    if contact_parts:
        contact_p = doc.add_paragraph()
        contact_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        contact_p.paragraph_format.space_after = Pt(_HEADING_SPACE_AFTER_PT)
        for i, part in enumerate(contact_parts):
            if i > 0:
                contact_p.add_run(" | ")
            if part.startswith("http"):
                _add_hyperlink(contact_p, part, part)
            else:
                contact_p.add_run(part)


def _add_bullets(doc: Document, bullets: list[str]) -> None:
    for bullet in bullets:
        doc.add_paragraph(bullet, style="List Bullet")


def _add_skills_section(doc: Document, resume_doc: dict, config: dict) -> None:
    skills = resume_doc.get("skills") or {}
    if not skills:
        return
    doc.add_heading("Technical Skills", level=1)
    ordered_categories = list(config["skills"]["groups"])
    ordered_categories += [c for c in skills if c not in ordered_categories]

    lines_used = 0
    for category in ordered_categories:
        items = skills.get(category)
        if not items:
            continue
        if lines_used >= config["skills"]["max_skill_lines"]:
            break
        label = _humanize_skill_category(category)
        doc.add_paragraph(f"{label}: {', '.join(items)}")
        lines_used += 1


def _add_experience_section(doc: Document, resume_doc: dict, config: dict) -> None:
    experience = resume_doc.get("experience") or []
    if not experience:
        return

    concurrent_idx = resume_rules.detect_concurrent_overlaps(experience, config)
    earlier_lines = []
    credential_entries = []
    wrote_heading = False

    for i, entry in enumerate(experience):
        classification = resume_rules.classify_role(entry, config)
        if classification == "CREDENTIAL":
            credential_entries.append(entry)
            continue
        if classification == "EARLIER":
            suffix = " (concurrent)" if i in concurrent_idx else ""
            earlier_lines.append(
                f"Earlier: {entry.get('role', '')}, {entry.get('company', '')} "
                f"({entry.get('location', '')}, {resume_rules.display_date_range(entry)}){suffix}"
            )
            continue

        if not wrote_heading:
            doc.add_heading("Professional Experience", level=1)
            wrote_heading = True

        role_p = doc.add_paragraph()
        role_run = role_p.add_run(f"{entry.get('role', '')} — {entry.get('company', '')}")
        role_run.bold = True
        suffix = " (concurrent)" if i in concurrent_idx else ""
        meta_bits = [b for b in (entry.get("location"), resume_rules.display_date_range(entry)) if b]
        doc.add_paragraph(f"{' | '.join(meta_bits)}{suffix}")
        _add_bullets(doc, entry.get("bullets", []))

    if earlier_lines:
        if not wrote_heading:
            doc.add_heading("Professional Experience", level=1)
        for line in earlier_lines:
            doc.add_paragraph(line)

    resume_doc["_credential_entries"] = credential_entries  # handed off to certifications section


def _add_projects_section(doc: Document, resume_doc: dict, config: dict) -> None:
    projects = resume_doc.get("projects") or []
    if not projects:
        return
    doc.add_heading("Selected Projects", level=1)
    pv = config["projects_by_variant"]
    for project in projects:
        name_p = doc.add_paragraph()
        name_run = name_p.add_run(project.get("name", ""))
        name_run.bold = True
        github_url = project.get("github_url")
        if github_url:
            name_p.add_run("  |  ")
            _add_hyperlink(name_p, github_url, "GitHub")
        bullets = (project.get("bullets") or [])[: pv["bullets_per_project_max"]]
        _add_bullets(doc, bullets)


def _add_education_section(doc: Document, resume_doc: dict) -> None:
    education = resume_doc.get("education") or []
    if not education:
        return
    doc.add_heading("Education", level=1)
    for edu in education:
        bits = [b for b in (edu.get("degree"), edu.get("school"), edu.get("date")) if b]
        doc.add_paragraph(" | ".join(bits))


def _add_certifications_section(doc: Document, resume_doc: dict) -> None:
    certifications = list(resume_doc.get("certifications") or [])
    credential_entries = resume_doc.pop("_credential_entries", [])
    if not certifications and not credential_entries:
        return
    doc.add_heading("Certifications", level=1)
    for cert in certifications:
        text = cert if isinstance(cert, str) else cert.get("name", "")
        doc.add_paragraph(text)
    for entry in credential_entries:
        bits = [b for b in (entry.get("role"), entry.get("company"), entry.get("date")) if b]
        doc.add_paragraph(" | ".join(bits))


def _add_languages_section(doc: Document, resume_doc: dict) -> None:
    languages = resume_doc.get("languages") or []
    if not languages:
        return
    doc.add_heading("Languages", level=1)
    text = ", ".join(
        f"{lang.get('language', '')} ({lang.get('proficiency', '')})" if lang.get("proficiency") else lang.get("language", "")
        for lang in languages
    )
    doc.add_paragraph(text)


def build_resume_docx(resume_doc: dict, config: dict | None = None) -> Document:
    """resume_doc is the same dict tailor_application() saves to
    TailoredDocument -- C3/C4/C6 are already applied to it by the time
    it gets here. Returns a python-docx Document; callers save it
    (doc.save(path) or doc.save(BytesIO()))."""
    config = config or resume_rules.get_config()
    resume_doc = dict(resume_doc)  # shallow copy -- _add_experience_section stashes a scratch key on it

    doc = Document()
    _configure_base_styles(doc)

    _add_name_and_contact(doc, resume_doc)

    summary = resume_doc.get("summary")
    if summary:
        doc.add_heading("Summary", level=1)
        doc.add_paragraph(summary)

    _add_skills_section(doc, resume_doc, config)
    _add_experience_section(doc, resume_doc, config)
    _add_projects_section(doc, resume_doc, config)
    _add_education_section(doc, resume_doc)
    _add_certifications_section(doc, resume_doc)
    _add_languages_section(doc, resume_doc)

    work_auth = resume_rules.work_authorization_line(config)
    if work_auth:
        doc.add_paragraph(work_auth)

    return doc
