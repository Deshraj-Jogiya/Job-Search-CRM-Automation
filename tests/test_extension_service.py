"""Companion browser extension backend (2026-09-16 Long Build, LNG-02).
Deliberately thin: extension_service.py reuses the exact same answer-
matching logic Playwright autofill already has (mechanical_common_answer,
contact fields) rather than a second, JS-side reimplementation -- these
tests exercise that reuse, plus the one genuinely new piece: matching
the page the user is looking at to a real application by hostname."""

import json

from conftest import make_application, make_company, make_posting

from app.services import extension_service


def _profile(**overrides):
    base = {"name": "Deshraj Jogiya", "contact": {"email": "deshraj@example.com", "phone": "555-0100"}}
    base.update(overrides)
    return base


def _set_profile(db, content):
    from app.models import ProfileVariant, ProfileVersion

    variant = ProfileVariant(name="Default", is_default=True)
    db.add(variant)
    db.commit()
    db.refresh(variant)
    version = ProfileVersion(variant_id=variant.id, content_json=json.dumps(content), source="manual", is_active=True)
    db.add(version)
    db.commit()
    return variant


def test_find_fillable_application_matches_by_hostname_ignoring_path_and_query(db, settings):
    company = make_company(db)
    posting = make_posting(db, company, job_url="https://jobs.acme.com/boards/acme/jobs/123")
    application = make_application(db, posting, status="Approved")

    found = extension_service.find_fillable_application(db, "https://jobs.acme.com/boards/acme/jobs/123?utm=x")

    assert found.id == application.id


def test_find_fillable_application_ignores_www_and_case(db, settings):
    company = make_company(db)
    posting = make_posting(db, company, job_url="https://WWW.Acme.com/apply")
    application = make_application(db, posting, status="Approved")

    found = extension_service.find_fillable_application(db, "https://acme.com/apply/step-2")

    assert found.id == application.id


def test_find_fillable_application_ignores_non_approved_applications(db, settings):
    # Real reason this matters: a "Needs Review" or "Rejected" application
    # is not something the user should be auto-filling a real form for --
    # "Approved" is the same real gate the existing "Mark Applied" button
    # already requires.
    company = make_company(db)
    posting = make_posting(db, company, job_url="https://jobs.acme.com/apply")
    make_application(db, posting, status="Needs Review")

    found = extension_service.find_fillable_application(db, "https://jobs.acme.com/apply")

    assert found is None


def test_find_fillable_application_returns_none_for_an_unrelated_site(db, settings):
    company = make_company(db)
    posting = make_posting(db, company, job_url="https://jobs.acme.com/apply")
    make_application(db, posting, status="Approved")

    found = extension_service.find_fillable_application(db, "https://totally-unrelated-site.com")

    assert found is None


def test_resolve_field_answers_fills_contact_fields_from_the_real_profile(db, settings):
    _set_profile(db, _profile())
    company = make_company(db)
    application = make_application(db, make_posting(db, company), status="Approved")

    answers = extension_service.resolve_field_answers(db, application.id, [
        {"field_id": "f1", "label": "First Name"},
        {"field_id": "f2", "label": "Last Name"},
        {"field_id": "f3", "label": "Email Address"},
        {"field_id": "f4", "label": "Phone Number"},
    ])

    assert answers == {"f1": "Deshraj", "f2": "Jogiya", "f3": "deshraj@example.com", "f4": "555-0100"}


def test_resolve_field_answers_uses_mechanical_common_answer_for_eeo_and_preferences(db, settings):
    _set_profile(db, _profile(
        eeo={"gender": "Prefer not to say"},
        application_preferences={"visa_sponsorship": "No"},
    ))
    company = make_company(db)
    application = make_application(db, make_posting(db, company), status="Approved")

    answers = extension_service.resolve_field_answers(db, application.id, [
        {"field_id": "f1", "label": "Gender"},
        {"field_id": "f2", "label": "Will you now or in the future require visa sponsorship?"},
    ])

    assert answers == {"f1": "Prefer not to say", "f2": "No"}


def test_resolve_field_answers_uses_the_real_posting_source_for_referral_question(db, settings):
    _set_profile(db, _profile())
    company = make_company(db)
    posting = make_posting(db, company, source="linkedin")
    application = make_application(db, posting, status="Approved")

    answers = extension_service.resolve_field_answers(db, application.id, [
        {"field_id": "f1", "label": "How did you hear about this position?"},
    ])

    assert answers == {"f1": "LinkedIn"}


def test_resolve_field_answers_fills_the_real_tailored_cover_letter_text(db, settings):
    from app.models import TailoredDocument

    _set_profile(db, _profile())
    company = make_company(db)
    application = make_application(db, make_posting(db, company), status="Approved")
    db.add(TailoredDocument(application_id=application.id, document_type="cover_letter", content="Dear Hiring Manager, ..."))
    db.commit()

    answers = extension_service.resolve_field_answers(db, application.id, [
        {"field_id": "f1", "label": "Cover Letter"},
    ])

    assert answers == {"f1": "Dear Hiring Manager, ..."}


def test_resolve_field_answers_never_guesses_an_unrecognized_question(db, settings):
    # The real principle this whole app runs on: no answer for a field
    # this can't ground in real data, not a plausible-sounding guess.
    _set_profile(db, _profile())
    company = make_company(db)
    application = make_application(db, make_posting(db, company), status="Approved")

    answers = extension_service.resolve_field_answers(db, application.id, [
        {"field_id": "f1", "label": "Why do you want to work here?"},
    ])

    assert answers == {}


def test_resolve_field_answers_returns_empty_for_an_unknown_application(db, settings):
    assert extension_service.resolve_field_answers(db, 999999, [{"field_id": "f1", "label": "First Name"}]) == {}


def test_resolve_field_answers_skips_a_field_with_no_label(db, settings):
    _set_profile(db, _profile())
    company = make_company(db)
    application = make_application(db, make_posting(db, company), status="Approved")

    answers = extension_service.resolve_field_answers(db, application.id, [{"field_id": "f1", "label": ""}])

    assert answers == {}


class TestRealFieldsFoundLiveOnSamsara:
    """Real fields flagged live 2026-09-16 as sitting unfilled that
    shouldn't have -- each backed by a real, already-stored profile
    field (contact.linkedin/github/portfolio, experience[0], a real
    US-format stored phone), never a new fabricated fact."""

    def test_linkedin_github_portfolio_urls(self, db, settings):
        _set_profile(db, _profile(contact={
            "email": "d@example.com", "phone": "(480) 876-2863",
            "linkedin": "https://www.linkedin.com/in/deshrajjogiya",
            "github": "https://github.com/Deshraj-Jogiya",
            "portfolio": "https://deshraj-jogiya.github.io",
        }))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {"field_id": "f1", "label": "LinkedIn Profile"},
            {"field_id": "f2", "label": "GitHub"},
            {"field_id": "f3", "label": "Portfolio / Personal Website"},
        ])

        assert answers == {
            "f1": "https://www.linkedin.com/in/deshrajjogiya",
            "f2": "https://github.com/Deshraj-Jogiya",
            "f3": "https://deshraj-jogiya.github.io",
        }

    def test_phone_country_inferred_from_a_real_us_format_phone_number(self, db, settings):
        _set_profile(db, _profile(contact={"email": "d@example.com", "phone": "(480) 876-2863"}))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [{"field_id": "f1", "label": "Country"}])

        assert answers == {"f1": "United States"}

    def test_phone_country_not_guessed_for_a_non_us_looking_number(self, db, settings):
        # Never a blind default -- only answers when the stored phone
        # itself really looks like a US number.
        _set_profile(db, _profile(contact={"email": "d@example.com", "phone": "+44 20 7946 0958"}))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [{"field_id": "f1", "label": "Country"}])

        assert answers == {}

    def test_most_recent_employer_is_the_real_first_experience_entry(self, db, settings):
        _set_profile(db, _profile(experience=[
            {"company": "Objectways Technologies LLC", "role": "Teleoperation Data Collection Associate", "date": "May 2026 - Present"},
            {"company": "Technoid LLC", "role": "Applied Machine Learning Engineer", "date": "Dec 2025 - May 2026"},
        ]))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {"field_id": "f1", "label": "Most Recent Employer"},
        ])

        assert answers == {"f1": "Objectways Technologies LLC"}

    def test_previously_worked_here_yes_when_a_real_past_employer_matches(self, db, settings):
        _set_profile(db, _profile(experience=[{"company": "Samsara", "role": "Intern", "date": "2023"}]))
        company = make_company(db, name="Samsara")
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {"field_id": "f1", "label": "Have you previously worked at Samsara?"},
        ])

        assert answers == {"f1": "Yes"}

    def test_previously_worked_here_no_when_no_past_employer_matches(self, db, settings):
        _set_profile(db, _profile(experience=[{"company": "Objectways Technologies LLC", "role": "X", "date": "2026"}]))
        company = make_company(db, name="Samsara")
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {"field_id": "f1", "label": "Have you previously worked at Samsara?"},
        ])

        assert answers == {"f1": "No"}

    def test_previously_worked_here_left_blank_with_no_experience_data_at_all(self, db, settings):
        # Missing data, not a genuine "never worked there" -- must not
        # guess "No" when there's nothing real to check against.
        _set_profile(db, _profile())
        company = make_company(db, name="Samsara")
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {"field_id": "f1", "label": "Have you previously worked at Samsara?"},
        ])

        assert answers == {}


class TestSelectFieldSupport:
    """Real bug found live 2026-09-16 on the real Samsara Greenhouse
    form: the content script only ever scanned input/textarea, so every
    <select> question (sponsorship, years of experience, education
    level, previously worked here, relocation, ...) -- the majority of
    real questions on that form -- silently never even reached this
    resolver. A select can't be set to arbitrary text, so this also
    covers the real safety requirement: only ever return one of the
    field's own real options, and leave it blank rather than guess when
    that mapping is ambiguous."""

    def test_selects_the_matching_real_option_for_a_common_answer(self, db, settings):
        _set_profile(db, _profile(application_preferences={"visa_sponsorship": "Yes, in the future"}))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {
                "field_id": "f1",
                "label": 'Will you now or in the future require Samsara to commence ("sponsor") '
                         "an immigration case in order to employ you?",
                "type": "select",
                "options": ["Yes", "No"],
            },
        ])

        assert answers == {"f1": "Yes"}

    def test_never_invents_an_option_that_does_not_exist_on_the_page(self, db, settings):
        # A three-way, semantically nuanced option set where a wrong pick
        # would be a real, serious misstatement on a real application --
        # must come back empty, not a confident-looking wrong guess.
        _set_profile(db, _profile(application_preferences={"work_authorization": "Will require sponsorship now or in the future"}))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {
                "field_id": "f1",
                "label": "Are you legally authorized to work in this country?",
                "type": "select",
                "options": ["I am a U.S. Citizen", "I require sponsorship", "I do not require sponsorship"],
            },
        ])

        assert answers == {}

    def test_a_non_select_field_is_unaffected_by_the_options_logic(self, db, settings):
        _set_profile(db, _profile())
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {"field_id": "f1", "label": "First Name"},
        ])

        assert answers == {"f1": "Deshraj"}


class TestBestOptionMatch:
    def test_exact_match_wins(self):
        assert extension_service._best_option_match("No", ["Yes", "No"]) == "No"

    def test_whole_word_match_on_a_short_option(self):
        assert extension_service._best_option_match("Yes, in the future", ["Yes", "No"]) == "Yes"

    def test_unambiguous_containment_match(self):
        assert extension_service._best_option_match("Job Board", ["LinkedIn", "Job Board", "Other"]) == "Job Board"

    def test_ambiguous_or_no_match_returns_none(self):
        assert extension_service._best_option_match("Prefer not to say", ["Male", "Female", "Non-binary"]) is None

    def test_no_options_returns_none(self):
        assert extension_service._best_option_match("Yes", []) is None

    def test_no_answer_returns_none(self):
        assert extension_service._best_option_match("", ["Yes", "No"]) is None

    def test_short_option_does_not_false_positive_on_a_longer_lookalike_word(self):
        # "No" must not match inside an unrelated word like "None" --
        # this is exactly why the short-option branch uses a real
        # word-boundary regex instead of a bare substring check.
        assert extension_service._best_option_match("None of the above apply", ["Yes", "No"]) is None


class TestApplicationMatchSummary:
    """Added 2026-09-16 after comparing against JobRight's own extension
    popup -- match score, which real documents ground the fill, and a
    profile-completeness signal. Every piece here already existed
    elsewhere in the app (JobApplication.match_score, TailoredDocument,
    profile_service.profile_completeness_warnings); these tests cover
    the bundling, not new intelligence."""

    def test_reports_the_real_match_score(self, db, settings):
        _set_profile(db, _profile())
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved", match_score=82)

        summary = extension_service.application_match_summary(db, application)

        assert summary["match_score"] == 82

    def test_reports_no_tailored_documents_when_none_exist(self, db, settings):
        _set_profile(db, _profile())
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        summary = extension_service.application_match_summary(db, application)

        assert summary["has_tailored_resume"] is False
        assert summary["has_tailored_cover_letter"] is False

    def test_reports_real_tailored_documents_when_they_exist(self, db, settings):
        from app.models import TailoredDocument

        _set_profile(db, _profile())
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")
        db.add(TailoredDocument(application_id=application.id, document_type="resume", content="{}"))
        db.add(TailoredDocument(application_id=application.id, document_type="cover_letter", content="Dear..."))
        db.commit()

        summary = extension_service.application_match_summary(db, application)

        assert summary["has_tailored_resume"] is True
        assert summary["has_tailored_cover_letter"] is True

    def test_profile_warnings_reflect_the_real_base_profile_not_per_job_flags(self, db, settings):
        # Deliberately PROFILE-level, not per-job: an application can
        # only reach "Approved" (the only status this ever matches)
        # after clearing every per-job hard-stop flag, so this must
        # reflect real profile gaps, never something job-specific.
        _set_profile(db, {"name": "Deshraj Jogiya"})  # no experience/education/skills/certifications
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        summary = extension_service.application_match_summary(db, application)

        assert len(summary["profile_warnings"]) > 0

    def test_no_warnings_for_a_genuinely_complete_profile(self, db, settings):
        _set_profile(db, {
            "name": "Deshraj Jogiya",
            "contact": {"email": "d@example.com"},
            "experience": [{"title": "Engineer", "company": "Acme", "bullets": ["Did a real thing."]}],
            "education": [{"school": "State U", "degree": "BS"}],
            "skills": ["Python"],
            "certifications": ["AWS"],
        })
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        summary = extension_service.application_match_summary(db, application)

        assert summary["profile_warnings"] == []

    def test_no_profile_set_up_yet_falls_back_to_no_warnings_not_an_error(self, db, settings):
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        summary = extension_service.application_match_summary(db, application)

        assert summary["profile_warnings"] == []


class TestEducationLevelAnswer:
    """Real field flagged live 2026-09-16 as a required, blocking gap on
    the actual Samsara form -- education[0] is the real highest/most-
    recent degree (same reverse-chronological convention confirmed for
    experience), matched against the field's real select options via
    the same phrase cascade every other select answer already uses."""

    def test_masters_degree_matches_a_real_masters_option(self, db, settings):
        _set_profile(db, _profile(education=[
            {"degree": "Master of Science, Information Technology", "school": "Arizona State University", "date": "Aug 2022 - Jul 2024"},
            {"degree": "Bachelor of Technology, Computer Engineering", "school": "Institute of Advanced Research", "date": "Aug 2017 - Jul 2021"},
        ]))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {
                "field_id": "f1",
                "label": "What is your highest level of education in Computer Science, Statistics, or a related field?",
                "type": "select",
                "options": ["Select...", "High School Diploma", "Associate's Degree", "Bachelor's Degree", "Master's Degree", "Doctorate"],
            },
        ])

        assert answers == {"f1": "Master's Degree"}

    def test_phd_matches_a_real_doctorate_option(self, db, settings):
        _set_profile(db, _profile(education=[{"degree": "Ph.D. in Computer Science", "school": "MIT", "date": "2020 - 2024"}]))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {
                "field_id": "f1", "label": "Highest level of education?", "type": "select",
                "options": ["Select...", "Bachelor's Degree", "Master's Degree", "Doctorate"],
            },
        ])

        assert answers == {"f1": "Doctorate"}

    def test_matches_the_real_samsara_option_text_with_a_curly_apostrophe(self, db, settings):
        # Real bug found live 2026-09-16: the actual Samsara/Greenhouse
        # form's real rendered options use a curly apostrophe
        # ("Master’s", not "Master's" -- confirmed directly in the
        # live DOM), shorter than this app's own canonical "Master's
        # Degree" phrase besides. Both the apostrophe-normalization fix
        # and the existing phrase-containment cascade need to hold here.
        _set_profile(db, _profile(education=[
            {"degree": "Master of Science, Information Technology", "school": "Arizona State University", "date": "Aug 2022 - Jul 2024"},
        ]))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {
                "field_id": "f1",
                "label": "What is your highest level of education in Computer Science, Statistics, or a related field?",
                "type": "select",
                "options": ["High School Diploma/GED", "Associate", "Bachelor’s", "Master’s", "PhD", "JD"],
            },
        ])

        assert answers == {"f1": "Master’s"}

    def test_no_education_data_leaves_it_blank(self, db, settings):
        _set_profile(db, _profile())
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {"field_id": "f1", "label": "Highest level of education?", "type": "select", "options": ["Bachelor's Degree", "Master's Degree"]},
        ])

        assert answers == {}

    def test_unrelated_label_is_not_matched(self, db, settings):
        _set_profile(db, _profile(education=[{"degree": "Master of Science", "school": "X", "date": "2024"}]))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {"field_id": "f1", "label": "What is your favorite color?"},
        ])

        assert answers == {}


class TestRelocationAssistanceAnswer:
    """Real, required, blocking field on the actual Samsara form -- a
    different question from "willing to relocate" (financial/logistical
    help vs. general openness), so deliberately its own narrower
    pattern rather than reusing mechanical_common_answer's existing one.
    Every answer here still only ever lands on a form reviewed before a
    real human submit click, never auto-submitted."""

    def test_real_samsara_wording_answered_no_when_willing_to_relocate(self, db, settings):
        _set_profile(db, _profile(application_preferences={"willing_to_relocate": "Yes"}))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {"field_id": "f1", "label": "Samsara will not provide relocation assistance for this role. Do you require relocation assistance?"},
        ])

        assert answers == {"f1": "No"}

    def test_answered_no_when_not_willing_to_relocate_either(self, db, settings):
        # Not relocating at all -> definitionally no relocation assistance needed.
        _set_profile(db, _profile(application_preferences={"willing_to_relocate": "No"}))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {"field_id": "f1", "label": "Do you require relocation assistance?"},
        ])

        assert answers == {"f1": "No"}

    def test_left_blank_for_a_genuine_maybe(self, db, settings):
        _set_profile(db, _profile(application_preferences={"willing_to_relocate": "Depends on the role"}))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {"field_id": "f1", "label": "Do you require relocation assistance?"},
        ])

        assert answers == {}

    def test_left_blank_with_no_stored_preference_at_all(self, db, settings):
        _set_profile(db, _profile())
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {"field_id": "f1", "label": "Do you require relocation assistance?"},
        ])

        assert answers == {}

    def test_does_not_accidentally_trigger_on_a_plain_willing_to_relocate_question(self, db, settings):
        # These stay two distinct patterns -- this one goes through
        # mechanical_common_answer's own existing willing_to_relocate
        # handling, not this new one.
        _set_profile(db, _profile(application_preferences={"willing_to_relocate": "Yes"}))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {"field_id": "f1", "label": "Are you willing to relocate?"},
        ])

        assert answers == {"f1": "Yes"}


class TestYearsOfExperienceAnswer:
    """Real, required, blocking field flagged live 2026-09-16 -- computed
    from real interval-merged date math over experience entries whose
    own title contains a data/ML/AI/analytics keyword (an objective,
    title-based criterion, not a subjective per-role judgment call).
    Checked against Deshraj's real 6 stored experience entries before
    building this: every one of them genuinely has one of those words
    in its own title already, not a hypothetical test fixture."""

    REAL_EXPERIENCE = [
        {"role": "Teleoperation Data Collection Associate", "company": "Objectways", "date": "May 2026 \u2013 Present"},
        {"role": "Applied Machine Learning Engineer", "company": "Technoid", "date": "Dec 2025 \u2013 May 2026"},
        {"role": "Data Analyst / Data Engineer", "company": "Zifatech", "date": "Jun 2025 \u2013 Dec 2025"},
        {"role": "Data Engineer & Machine Learning Research Assistant", "company": "ASU", "date": "Sep 2024 \u2013 Jun 2025"},
        {"role": "AI/ML Engineering Apprentice", "company": "Jetson", "date": "Jul 2024 \u2013 Aug 2024"},
        {"role": "Data Analyst", "company": "Kronic Keys", "date": "Aug 2021 \u2013 Mar 2022"},
    ]

    def test_matches_a_real_bucketed_select_option(self, db, settings):
        from unittest.mock import patch
        from datetime import datetime

        _set_profile(db, _profile(experience=self.REAL_EXPERIENCE))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        with patch("app.services.extension_service.utcnow") as mock_now:
            mock_now.return_value = datetime(2026, 9, 16)
            answers = extension_service.resolve_field_answers(db, application.id, [
                {
                    "field_id": "f1",
                    "label": "How many years of experience do you have in a data engineering-focused role?",
                    "type": "select",
                    "options": ["Select...", "0-1 years", "2-3 years", "4-6 years", "7+ years"],
                },
            ])

        assert answers == {"f1": "2-3 years"}

    def test_matches_the_real_samsara_options_with_the_gap_between_1_2_and_3plus(self, db, settings):
        # Real bug found live 2026-09-16: the actual Samsara form's real
        # options are "0-1 years" / "1-2 years" / "3+ years" -- note the
        # gap, no "2-3 years" bucket exists at all. Deshraj's real 2.9
        # computed years (post inclusive-month fix) falls in that gap on
        # the raw figure alone; only the rounded-to-3 check makes this
        # correctly land on "3+ years", which is what he expects and what
        # a human reading his resume would call it too.
        from unittest.mock import patch
        from datetime import datetime

        _set_profile(db, _profile(experience=self.REAL_EXPERIENCE))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        with patch("app.services.extension_service.utcnow") as mock_now:
            mock_now.return_value = datetime(2026, 9, 16)
            answers = extension_service.resolve_field_answers(db, application.id, [
                {
                    "field_id": "f1",
                    "label": "How many years of experience do you have in a data engineering-focused role?",
                    "type": "select",
                    "options": ["0-1 years", "1-2 years", "3+ years"],
                },
            ])

        assert answers == {"f1": "3+ years"}

    def test_rounding_never_widens_a_genuinely_low_figure(self, db, settings):
        # The rounding fix above must never push a real 1.4-year figure
        # into a "3+" bucket -- only genuine boundary cases (2.9 -> 3)
        # get the benefit, never an arbitrary low number.
        from unittest.mock import patch
        from datetime import datetime

        _set_profile(db, _profile(experience=[
            {"role": "Data Analyst", "company": "A", "date": "Jan 2025 – May 2026"},  # ~16-17 real months -> ~1.4 years
        ]))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        with patch("app.services.extension_service.utcnow") as mock_now:
            mock_now.return_value = datetime(2026, 9, 16)
            answers = extension_service.resolve_field_answers(db, application.id, [
                {
                    "field_id": "f1", "label": "Years of experience in data engineering?",
                    "type": "select", "options": ["0-1 years", "1-2 years", "3+ years"],
                },
            ])

        assert answers == {"f1": "1-2 years"}

    def test_excludes_a_role_with_no_data_related_keyword_in_its_title(self, db, settings):
        from unittest.mock import patch
        from datetime import datetime

        _set_profile(db, _profile(experience=[
            {"role": "Retail Sales Associate", "company": "Store", "date": "Jan 2020 \u2013 Jan 2024"},  # NOT data-related
            {"role": "Data Analyst", "company": "Kronic Keys", "date": "Aug 2021 \u2013 Mar 2022"},  # 7 months, real
        ]))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        with patch("app.services.extension_service.utcnow") as mock_now:
            mock_now.return_value = datetime(2026, 9, 16)
            answers = extension_service.resolve_field_answers(db, application.id, [
                {
                    "field_id": "f1", "label": "Years of experience in data engineering?",
                    "type": "select", "options": ["0-1 years", "2-3 years"],
                },
            ])

        # Only the 7-month Data Analyst role counts -> 0.6 years -> "0-1 years"
        assert answers == {"f1": "0-1 years"}

    def test_overlapping_roles_are_not_double_counted(self, db, settings):
        _set_profile(db, _profile(experience=[
            {"role": "Data Engineer", "company": "A", "date": "Jan 2023 \u2013 Jan 2024"},
            {"role": "Data Analyst (part-time)", "company": "B", "date": "Jun 2023 \u2013 Sep 2023"},  # fully overlaps with the above
        ]))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {"field_id": "f1", "label": "Years of experience in data engineering?", "type": "select", "options": ["0-1 years", "2-3 years"]},
        ])

        # Real merged span is still just Jan 2023 - Jan 2024 = 1.0 year,
        # not 1.0 + 0.25 = 1.25 if the overlap were wrongly double-counted.
        assert answers == {"f1": "0-1 years"}

    def test_no_experience_data_leaves_it_blank(self, db, settings):
        _set_profile(db, _profile())
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {"field_id": "f1", "label": "Years of experience?", "type": "select", "options": ["0-1 years", "2-3 years"]},
        ])

        assert answers == {}

    def test_unparseable_dates_leave_it_blank_rather_than_guess(self, db, settings):
        _set_profile(db, _profile(experience=[{"role": "Data Engineer", "company": "A", "date": "some time ago"}]))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {"field_id": "f1", "label": "Years of experience?", "type": "select", "options": ["0-1 years", "2-3 years"]},
        ])

        assert answers == {}

    def test_plain_number_for_a_non_select_field(self, db, settings):
        from unittest.mock import patch
        from datetime import datetime

        _set_profile(db, _profile(experience=[{"role": "Data Engineer", "company": "A", "date": "Jan 2023 \u2013 Jan 2024"}]))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        with patch("app.services.extension_service.utcnow") as mock_now:
            mock_now.return_value = datetime(2026, 9, 16)
            answers = extension_service.resolve_field_answers(db, application.id, [
                {"field_id": "f1", "label": "Years of experience?"},
            ])

        # "Jan 2023 - Jan 2024" is 13 real months (Jan through Jan
        # inclusive), not 12 -- both endpoint months are ones actually
        # worked. Real bug found live 2026-09-16: the old end-minus-start
        # math undercounted every interval by exactly one month, which on
        # Deshraj's real profile was the difference between landing in no
        # bucket at all and correctly landing in "3+ years".
        assert answers == {"f1": "1.1"}


class TestZipCodeAnswer:
    """No zip/postal field exists in the profile schema today (checked
    the real stored data directly -- only free-text contact.location).
    This matches contact.zip/zip_code/postal_code so it starts working
    the moment one of those is added via the Profile page."""

    def test_answers_from_a_real_stored_zip_once_the_profile_has_one(self, db, settings):
        _set_profile(db, _profile(contact={"email": "d@example.com", "zip": "85281"}))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {"field_id": "f1", "label": "What is the zip code of your primary residence?"},
        ])

        assert answers == {"f1": "85281"}

    def test_left_blank_when_no_zip_is_stored_rather_than_guessed_from_the_city(self, db, settings):
        _set_profile(db, _profile(contact={"email": "d@example.com", "location": "Tempe, Arizona (open to relocation)"}))
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        answers = extension_service.resolve_field_answers(db, application.id, [
            {"field_id": "f1", "label": "What is the zip code of your primary residence?"},
        ])

        assert answers == {}


class TestAddJobFromPage:
    """The popup's "+ Add This Job in One Click" action -- real gap
    Deshraj pointed at directly, researched against JobRight/Simplify
    Copilot's own popups before building (both let a user add an
    unrecognized posting and generate tailored documents right there).
    Reuses intake_service.get_or_create_company (promoted from a private
    helper, was intake_service._get_or_create_company) rather than a
    second copy of that dedupe logic."""

    def test_creates_a_real_posting_and_application(self, db, settings):
        application = extension_service.add_job_from_page(
            db, "https://example.com/careers/123", "Data Engineer", "Acme Analytics",
            "A" * 60,  # real min-length JD text
        )

        assert application.status == "Ingested"
        assert application.posting.job_title == "Data Engineer"
        assert application.posting.company_name_raw == "Acme Analytics"
        assert application.posting.source == "manual"
        assert application.posting.job_url == "https://example.com/careers/123"

    def test_reuses_an_existing_company_rather_than_creating_a_duplicate(self, db, settings):
        from app.models import Company

        make_company(db, name="Acme Analytics")

        extension_service.add_job_from_page(
            db, "https://example.com/careers/123", "Data Engineer", "Acme Analytics", "A" * 60
        )

        assert db.query(Company).filter(Company.name == "Acme Analytics").count() == 1

    def test_adding_the_same_url_twice_returns_the_same_application_not_a_duplicate(self, db, settings):
        first = extension_service.add_job_from_page(
            db, "https://example.com/careers/123", "Data Engineer", "Acme Analytics", "A" * 60
        )
        second = extension_service.add_job_from_page(
            db, "https://example.com/careers/123", "Data Engineer", "Acme Analytics", "A" * 60
        )

        assert first.id == second.id

    def test_rejects_a_page_with_no_real_job_title(self, db, settings):
        import pytest

        with pytest.raises(extension_service.ExtensionServiceError):
            extension_service.add_job_from_page(db, "https://example.com", "", "Acme", "A" * 60)

    def test_rejects_a_page_with_too_little_real_description_text(self, db, settings):
        import pytest

        with pytest.raises(extension_service.ExtensionServiceError):
            extension_service.add_job_from_page(db, "https://example.com", "Data Engineer", "Acme", "too short")


class TestScoreAndTailorNewApplication:
    """The real-time counterpart to confirmation_service.
    evaluate_and_enqueue's asynchronous-discovery path -- a manually-
    added job skips the timed "Pending Confirmation" window (the human
    is already looking at this exact posting right now), but a genuine
    hard-stop fabrication flag still goes to Needs Review exactly like
    every other source, never silently approved."""

    def test_a_clean_result_is_approved_immediately_skipping_pending_confirmation(self, db, settings):
        from unittest.mock import patch

        application = extension_service.add_job_from_page(
            db, "https://example.com/careers/123", "Data Engineer", "Acme Analytics", "A" * 60
        )

        def fake_tailor(db_arg, application_id):
            app = db_arg.query(type(application)).filter(type(application).id == application_id).first()
            app.status = "Pending Confirmation"  # the real, clean-tailoring outcome for a non-Playwright-supported source
            db_arg.commit()

        with (
            patch("app.services.matching_service.score_application") as mock_score,
            patch("app.services.tailoring_service.tailor_application", side_effect=fake_tailor),
        ):
            extension_service.score_and_tailor_new_application(db, application.id)

        mock_score.assert_called_once()
        db.refresh(application)
        assert application.status == "Approved"
        assert application.confirmed_by_user is True

    def test_a_hard_stop_flag_stays_in_needs_review_never_auto_approved(self, db, settings):
        from unittest.mock import patch

        application = extension_service.add_job_from_page(
            db, "https://example.com/careers/123", "Data Engineer", "Acme Analytics", "A" * 60
        )

        def fake_tailor(db_arg, application_id):
            app = db_arg.query(type(application)).filter(type(application).id == application_id).first()
            app.status = "Needs Review"  # a real hard-stop fabrication finding
            db_arg.commit()

        with (
            patch("app.services.matching_service.score_application"),
            patch("app.services.tailoring_service.tailor_application", side_effect=fake_tailor),
        ):
            extension_service.score_and_tailor_new_application(db, application.id)

        db.refresh(application)
        assert application.status == "Needs Review"  # never auto-approved past a real safety flag
