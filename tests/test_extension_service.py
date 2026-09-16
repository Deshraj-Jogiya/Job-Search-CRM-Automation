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
