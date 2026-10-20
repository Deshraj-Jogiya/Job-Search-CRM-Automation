from app.services.document_render_service import standard_document_filename


def test_resume_uses_full_name_and_company():
    assert standard_document_filename("resume", "Deshraj Jogiya", "Fireflies.ai") == "Deshraj_Jogiya_Fireflies_resume.pdf"


def test_cover_letter_uses_first_name_and_company():
    assert standard_document_filename("cover_letter", "Deshraj Jogiya", "Fireflies.ai") == "Deshraj_Fireflies_cover_letter.pdf"


def test_corporate_suffix_is_dropped():
    assert standard_document_filename("resume", "Deshraj Jogiya", "AikiTech, Inc.") == "Deshraj_Jogiya_AikiTech_resume.pdf"


def test_missing_values_fall_back_without_empty_segments():
    assert standard_document_filename("resume", "", "") == "Candidate_Company_resume.pdf"


def test_company_shows_employment_type_in_parentheses():
    from app.services import resume_rules

    assert resume_rules.display_company({"company": "Technoid LLC", "employment_type": "Contract"}) == "Technoid LLC (Contract)"
    assert resume_rules.display_company({"company": "Technoid LLC"}) == "Technoid LLC"


def test_earlier_line_has_no_nested_parentheses():
    from app.services import resume_rules

    line = resume_rules.earlier_line(
        {"role": "Data Analyst", "company": "Kronic Keys", "employment_type": "Full-Time",
         "location": "Junagadh, India", "date": "Aug 2021 - Mar 2022"}
    )

    assert line == "Earlier: Data Analyst, Kronic Keys (Full-Time) | Junagadh, India | Aug 2021 - Mar 2022"
