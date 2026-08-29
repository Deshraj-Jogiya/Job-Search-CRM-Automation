"""Resume PDF layout, reworked to match a format Deshraj specifically
asked to match (two real example PDFs reviewed for this pass): centered
name/contact block split into an info line and a links line, uppercase
section headers with a rule underneath, and two-column left/right rows
for company+location / role+dates / project+tech / school+date.
Deliberately does NOT replicate the aggressive bullet rewording seen in
the reference's JD-tailored example (claiming tools/skills not used) --
content fabrication is a separate, already-guarded concern
(tailoring_service's fabrication safeguard), not a rendering one."""

from unittest.mock import patch

from app.services import document_render_service
from app.services.document_render_service import (
    _contact_lines,
    _humanize_skill_category,
    _section_header,
    render_cover_letter_pdf,
    render_interview_prep_cheat_sheet_pdf,
    render_resume_pdf,
)

_FULL_CONTENT = {
    "name": "Jane Doe",
    "title": "Data Engineer",
    "summary": "Builds pipelines.",
    "contact": {
        "email": "jane@example.com",
        "phone": "555-1234",
        "location": "Austin, TX",
        "linkedin": "https://linkedin.com/in/jane",
        "github": "https://github.com/jane",
    },
    "skills": {"languages": ["Python", "SQL"]},
    "experience": [
        {
            "role": "Data Engineer",
            "company": "Acme Corp",
            "location": "Remote",
            "date": "2022 - Present",
            "bullets": ["Did a thing.", "Did another thing."],
        }
    ],
    "projects": [
        {"name": "Side Project", "bullets": ["Built it."], "technologies": ["Python", "Docker"]}
    ],
    "education": [{"degree": "B.S. Computer Science", "school": "State University", "date": "2016 - 2020"}],
    "certifications": ["AWS Certified Data Analytics"],
}




def test_contact_lines_splits_info_from_links():
    lines = _contact_lines({"email": "a@b.com", "phone": "555-1234", "linkedin": "https://linkedin.com/in/x"})
    assert len(lines) == 2
    assert "a@b.com" in lines[0] and "555-1234" in lines[0]
    assert "linkedin.com" in lines[1]
    assert "linkedin.com" not in lines[0]


def test_contact_lines_omits_empty_groups():
    assert _contact_lines({"email": "a@b.com"}) == _contact_lines({"email": "a@b.com"})
    lines = _contact_lines({"email": "a@b.com"})
    assert len(lines) == 1


def test_contact_lines_escapes_html():
    lines = _contact_lines({"email": "a<b>@example.com"})
    assert "<b>" not in lines[0]
    assert "&lt;b&gt;" in lines[0]


def test_section_header_is_uppercased():
    flow = _section_header("Professional Summary")
    assert len(flow) == 2
    assert "PROFESSIONAL SUMMARY" in flow[0].text


def test_humanize_skill_category_capitalizes_and_uppercases_acronyms():
    assert _humanize_skill_category("data_tools") == "Data Tools"
    assert _humanize_skill_category("ai_ml") == "AI ML"


def test_humanize_skill_category_uses_explicit_overrides_for_known_categories():
    # Real bug: the generic word-by-word pass produced "Generative AI LLMS
    # Agentic Frameworks" (LLMs fully uppercased) and "Devops Tooling
    # Analytics" (no "&", reads as a run-on noun pile) -- visibly
    # unpolished on an otherwise clean resume.
    assert _humanize_skill_category("generative_ai_llms_agentic_frameworks") == "Generative AI, LLMs & Agentic Frameworks"
    assert _humanize_skill_category("devops_tooling_analytics") == "DevOps, Tooling & Analytics"
    assert _humanize_skill_category("machine_learning_deep_learning") == "Machine Learning & Deep Learning"
    assert _humanize_skill_category("data_engineering_cloud_platforms") == "Data Engineering & Cloud Platforms"
    assert _humanize_skill_category("full_stack_software_engineering") == "Full-Stack Software Engineering"


def test_render_resume_pdf_produces_a_real_pdf():
    pdf_bytes = render_resume_pdf(_FULL_CONTENT)
    assert pdf_bytes.startswith(b"%PDF")


def test_render_resume_pdf_uses_reference_section_titles():
    """render_resume_pdf itself dictates the exact section wording
    (matching Deshraj's real resume: "PROFESSIONAL SUMMARY", "TECHNICAL
    SKILLS", etc., not the old generic "Summary"/"Skills") -- verified
    by spying on _section_header's calls rather than parsing reportlab's
    compressed PDF stream, which uses an encoding not worth
    reverse-engineering just for a test (real visual output was already
    checked by hand for this pass)."""
    with patch.object(document_render_service, "_section_header", wraps=_section_header) as spy:
        render_resume_pdf(_FULL_CONTENT)
    titles_used = [call.args[0] for call in spy.call_args_list]
    assert titles_used == [
        "Professional Summary",
        "Technical Skills",
        "Professional Experience",
        "Key Projects",
        "Education",
        "Certifications",
    ]


def test_render_resume_pdf_omits_empty_sections():
    minimal = {"name": "Jane Doe", "contact": {"email": "jane@example.com"}}
    with patch.object(document_render_service, "_section_header", wraps=_section_header) as spy:
        render_resume_pdf(minimal)
    assert spy.call_args_list == []


def test_render_resume_pdf_accepts_json_string():
    import json

    pdf_bytes = render_resume_pdf(json.dumps(_FULL_CONTENT))
    assert pdf_bytes.startswith(b"%PDF")


def test_render_resume_pdf_with_project_github_link_still_produces_a_real_pdf():
    content = {**_FULL_CONTENT, "projects": [
        {"name": "Side Project", "bullets": ["Built it."], "technologies": ["Python"],
         "github_url": "https://github.com/jane/side-project"},
    ]}
    pdf_bytes = render_resume_pdf(content)
    assert pdf_bytes.startswith(b"%PDF")


def test_render_resume_pdf_project_without_github_url_still_renders():
    content = {**_FULL_CONTENT, "projects": [
        {"name": "Side Project", "bullets": ["Built it."], "technologies": ["Python"]},
    ]}
    pdf_bytes = render_resume_pdf(content)
    assert pdf_bytes.startswith(b"%PDF")


def test_render_resume_pdf_handles_missing_optional_fields_without_crashing():
    sparse = {
        "name": "Jane Doe",
        "contact": {"email": "jane@example.com"},
        "experience": [{"role": "Engineer", "bullets": []}],  # no company/location/date
        "projects": [{"name": "X"}],  # no bullets/technologies
        "education": [{"degree": "B.S."}],  # no school/date
    }
    pdf_bytes = render_resume_pdf(sparse)
    assert pdf_bytes.startswith(b"%PDF")


def test_render_cover_letter_pdf_still_works():
    pdf_bytes = render_cover_letter_pdf("Dear hiring manager,\n\nI'd love to join.", "Jane Doe")
    assert pdf_bytes.startswith(b"%PDF")


_CHEAT_SHEET_ROUNDS = {
    "rounds": [
        {
            "round_name": "Recruiter Screen",
            "likely_interviewer": "HR generalist",
            "what_it_tests": "fit and motivation",
            "qa_pairs": [
                {
                    "question": "Tell me about yourself.",
                    "draft_answer": "A long drafted answer that would read as a paragraph in the app.",
                    "quick_reference": "MS ASU -> data eng roles -> CurioSync",
                }
            ],
            "questions_to_ask_them": ["What does day-to-day look like?"],
        }
    ],
    "grounded_in_real_research": True,
}


def test_render_interview_prep_cheat_sheet_produces_a_real_pdf():
    pdf_bytes = render_interview_prep_cheat_sheet_pdf(
        "Data Engineer II", "Acme & Co", {"strengths_to_emphasize": ["Real pipelines"]},
        {"why_this_company_talking_points": ["Their AI mission"]}, _CHEAT_SHEET_ROUNDS,
    )
    assert pdf_bytes.startswith(b"%PDF")


def test_render_interview_prep_cheat_sheet_handles_missing_sections():
    pdf_bytes = render_interview_prep_cheat_sheet_pdf("Data Engineer II", "Acme & Co", {}, {}, {"rounds": []})
    assert pdf_bytes.startswith(b"%PDF")


def test_render_interview_prep_cheat_sheet_handles_special_characters():
    rounds = {
        "rounds": [
            {
                "round_name": "Tech & Systems <Design>",
                "qa_pairs": [{"question": "AT&T style Q?", "quick_reference": "cue with & and <tags>"}],
            }
        ]
    }
    pdf_bytes = render_interview_prep_cheat_sheet_pdf("Role & Title", "Acme <Co>", {}, {}, rounds)
    assert pdf_bytes.startswith(b"%PDF")
