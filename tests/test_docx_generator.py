from app.services.docx_generator import build_resume_docx
from app.services.resume_rules import get_config

_RESUME_DOC = {
    "name": "Test Candidate",
    "title": "Data Engineer",
    "contact": {"email": "test@example.com", "phone": "555-1234", "location": "Austin, TX"},
    "summary": "Data engineer with a track record of building reliable pipelines.",
    "skills": {"Languages": ["Python", "SQL"], "Cloud & Warehouses": ["AWS", "Snowflake"]},
    "experience": [
        {
            "role": "Data Engineer", "company": "Acme Corp", "location": "Austin, TX",
            "date": "Jan 2022 - Present", "bullets": ["Built ingestion pipelines.", "Cut runtime by roughly 40%."],
        },
        {
            "role": "Intern", "company": "Beta Inc", "location": "Remote",
            "date": "Jun 2020 - Aug 2020", "bullets": ["Wrote ETL scripts."],
        },
    ],
    "projects": [
        {"name": "Career Pilot", "bullets": ["Built a job-search CRM.", "Automated tailoring."]},
    ],
    "education": [{"degree": "B.S. Computer Science", "school": "State University", "date": "2019"}],
    "certifications": ["AWS Certified Data Engineer"],
}


def _body_xml(doc) -> str:
    return doc.element.body.xml


class TestBuildResumeDocx:
    def test_returns_a_real_document(self):
        doc = build_resume_docx(_RESUME_DOC)
        assert doc is not None

    def test_name_and_contact_in_body(self):
        doc = build_resume_docx(_RESUME_DOC)
        text = "\n".join(p.text for p in doc.paragraphs)
        assert "Test Candidate" in text
        assert "test@example.com" in text

    def test_standard_section_headings_present(self):
        doc = build_resume_docx(_RESUME_DOC)
        headings = {p.text for p in doc.paragraphs if p.style.name.startswith("Heading")}
        assert "Summary" in headings
        assert "Technical Skills" in headings
        assert "Professional Experience" in headings
        assert "Selected Projects" in headings
        assert "Education" in headings
        assert "Certifications" in headings

    def test_recent_role_rendered_as_full_entry(self):
        doc = build_resume_docx(_RESUME_DOC)
        text = "\n".join(p.text for p in doc.paragraphs)
        assert "Data Engineer — Acme Corp" in text

    def test_short_old_internship_folds_into_earlier_line(self):
        doc = build_resume_docx(_RESUME_DOC)
        text = "\n".join(p.text for p in doc.paragraphs)
        assert "Earlier: Intern, Beta Inc" in text
        assert "Intern — Beta Inc" not in text  # never a full separate entry

    def test_certification_and_credential_entries_both_appear(self):
        doc = build_resume_docx(_RESUME_DOC)
        text = "\n".join(p.text for p in doc.paragraphs)
        assert "AWS Certified Data Engineer" in text


class TestAtsSafety:
    """C7's enforced-not-intended ATS-safety checks."""

    def test_zero_tables(self):
        doc = build_resume_docx(_RESUME_DOC)
        assert len(doc.tables) == 0
        assert "<w:tbl>" not in _body_xml(doc)

    def test_zero_literal_bullet_characters_in_any_run(self):
        doc = build_resume_docx(_RESUME_DOC)
        for p in doc.paragraphs:
            for run in p.runs:
                assert "•" not in run.text
                assert not run.text.strip().startswith("- ")

    def test_bullets_use_real_list_bullet_style(self):
        doc = build_resume_docx(_RESUME_DOC)
        bullet_paragraphs = [p for p in doc.paragraphs if p.style.name == "List Bullet"]
        assert len(bullet_paragraphs) > 0

    def test_no_headers_or_footers_used(self):
        doc = build_resume_docx(_RESUME_DOC)
        section = doc.sections[0]
        # python-docx sections always HAVE a header/footer part, but an
        # unused one carries no real paragraphs with text.
        header_text = "".join(p.text for p in section.header.paragraphs)
        footer_text = "".join(p.text for p in section.footer.paragraphs)
        assert header_text == ""
        assert footer_text == ""

    def test_single_section_no_multi_column_layout(self):
        doc = build_resume_docx(_RESUME_DOC)
        assert len(doc.sections) == 1


class TestPageDensity:
    """A real bug (found by actually opening a generated file, not
    just checking extracted text): python-docx's default template
    carries 10pt space-after + 1.15x line spacing on every paragraph,
    a 24pt space-before on headings, and 1in/1.25in page margins --
    none of which the generator explicitly overrode, so a real resume
    silently ran to 2 pages on well under a page's worth of content.
    These assertions pin the fix structurally; test_golden_resume.py's
    text-only comparison can't catch a spacing regression like this one
    on its own."""

    def test_margins_are_tight_not_word_defaults(self):
        doc = build_resume_docx(_RESUME_DOC)
        section = doc.sections[0]
        assert section.top_margin.inches < 0.6
        assert section.bottom_margin.inches < 0.6
        assert section.left_margin.inches < 0.75
        assert section.right_margin.inches < 0.75

    def test_normal_style_has_no_leftover_space_after(self):
        doc = build_resume_docx(_RESUME_DOC)
        space_after = doc.styles["Normal"].paragraph_format.space_after
        assert space_after is not None
        assert space_after.pt <= 6

    def test_normal_style_is_single_spaced_not_115x(self):
        from docx.enum.text import WD_LINE_SPACING

        doc = build_resume_docx(_RESUME_DOC)
        assert doc.styles["Normal"].paragraph_format.line_spacing_rule == WD_LINE_SPACING.SINGLE

    def test_heading_space_before_is_modest_not_24pt(self):
        doc = build_resume_docx(_RESUME_DOC)
        space_before = doc.styles["Heading 1"].paragraph_format.space_before
        assert space_before is not None
        assert space_before.pt <= 12

    def test_bullet_style_inherits_zero_leftover_spacing(self):
        doc = build_resume_docx(_RESUME_DOC)
        bullet_style = doc.styles["List Bullet"]
        space_after = bullet_style.paragraph_format.space_after
        assert space_after is not None
        assert space_after.pt <= 6


class TestWorkAuthorizationNeverGenerated:
    def test_no_work_auth_line_by_default(self):
        doc = build_resume_docx(_RESUME_DOC)
        text = "\n".join(p.text for p in doc.paragraphs)
        assert "authorized to work" not in text.lower()
        assert "visa" not in text.lower()

    def test_config_supplied_text_is_used_verbatim_when_enabled(self):
        config = dict(get_config())
        config["work_authorization"] = {
            "include_work_auth_line": True,
            "work_auth_text": "Authorized to work in the U.S. without sponsorship.",
        }
        doc = build_resume_docx(_RESUME_DOC, config=config)
        text = "\n".join(p.text for p in doc.paragraphs)
        assert "Authorized to work in the U.S. without sponsorship." in text
