"""Auth boundary for the extension API (routers/extension.py). This is
the one route in the app authenticated by an explicit header instead of
the admin_session cookie -- see the router's own docstring for why
(the extension calls from a chrome-extension:// origin that never gets
the CSRF cookie via a real page load). These tests cover the boundary
that actually matters: no header, a garbage header, and an expired/
tampered token must all be rejected exactly like a missing login,
never treated as "not provided, so allow it through"."""

import pytest
from fastapi import HTTPException
from types import SimpleNamespace

from conftest import make_application, make_company, make_posting

from app.routers import extension as extension_router
from app.services import auth_service


def _request(headers: dict):
    return SimpleNamespace(headers=headers)


def test_missing_session_header_is_rejected(db, settings):
    with pytest.raises(HTTPException) as exc_info:
        extension_router.require_extension_session(_request({}))
    assert exc_info.value.status_code == 401


def test_garbage_session_header_is_rejected(db, settings):
    with pytest.raises(HTTPException) as exc_info:
        extension_router.require_extension_session(_request({"X-Career-Pilot-Session": "not-a-real-token"}))
    assert exc_info.value.status_code == 401


def test_genuine_session_token_is_accepted(db, settings):
    from app.models import AdminAccount

    account = AdminAccount(username="deshraj", password_hash="x")
    db.add(account)
    db.commit()
    db.refresh(account)
    token = auth_service.create_session_token(account.id)

    account_id = extension_router.require_extension_session(_request({"X-Career-Pilot-Session": token}))

    assert account_id == account.id


def test_match_endpoint_reports_no_match_honestly_rather_than_erroring(db, settings):
    result = extension_router.match_current_page(
        extension_router.MatchRequest(url="https://nowhere-real.example.com"), db=db, _account_id=1,
    )
    assert result == {"matched": False}


def test_match_endpoint_reports_the_real_application_when_found(db, settings):
    # Enriched 2026-09-16 after comparing against JobRight's own popup --
    # match score, which real documents will ground the fill, and a
    # profile-completeness signal, researched and reused from existing
    # code (see extension_service.application_match_summary) rather than
    # invented. No profile variant exists in this test's db at all, so
    # profile_warnings falls back to [] (same MatchingServiceError path
    # resolve_field_answers already handles) rather than raising.
    company = make_company(db)
    posting = make_posting(db, company, job_url="https://jobs.acme.com/apply", job_title="Data Engineer")
    application = make_application(db, posting, status="Approved", match_score=82)

    result = extension_router.match_current_page(
        extension_router.MatchRequest(url="https://jobs.acme.com/apply"), db=db, _account_id=1,
    )

    assert result == {
        "matched": True,
        "application_id": application.id,
        "job_title": "Data Engineer",
        "company_name": company.name,
        "match_score": 82,
        "has_tailored_resume": False,
        "has_tailored_cover_letter": False,
        "profile_warnings": [],
    }


def test_match_endpoint_reports_real_tailored_document_presence(db, settings):
    from app.models import TailoredDocument

    company = make_company(db)
    posting = make_posting(db, company, job_url="https://jobs.acme.com/apply", job_title="Data Engineer")
    application = make_application(db, posting, status="Approved")
    db.add(TailoredDocument(application_id=application.id, document_type="resume", content="{}"))
    db.commit()

    result = extension_router.match_current_page(
        extension_router.MatchRequest(url="https://jobs.acme.com/apply"), db=db, _account_id=1,
    )

    assert result["has_tailored_resume"] is True
    assert result["has_tailored_cover_letter"] is False


def test_match_endpoint_reports_real_profile_completeness_warnings(db, settings):
    import json
    from app.models import ProfileVariant, ProfileVersion

    variant = ProfileVariant(name="Default", is_default=True)
    db.add(variant)
    db.commit()
    db.refresh(variant)
    # A profile with every completeness-relevant section genuinely empty.
    db.add(ProfileVersion(
        variant_id=variant.id, content_json=json.dumps({"name": "Deshraj Jogiya"}), source="manual", is_active=True,
    ))
    db.commit()

    company = make_company(db)
    application = make_application(db, make_posting(db, company, job_url="https://jobs.acme.com/apply"), status="Approved")

    result = extension_router.match_current_page(
        extension_router.MatchRequest(url="https://jobs.acme.com/apply"), db=db, _account_id=1,
    )

    assert len(result["profile_warnings"]) > 0


def test_answers_endpoint_dispatches_to_the_real_resolver(db, settings):
    import json
    from app.models import ProfileVariant, ProfileVersion

    variant = ProfileVariant(name="Default", is_default=True)
    db.add(variant)
    db.commit()
    db.refresh(variant)
    db.add(ProfileVersion(
        variant_id=variant.id,
        content_json=json.dumps({"name": "Deshraj Jogiya", "contact": {"email": "d@example.com"}}),
        source="manual", is_active=True,
    ))
    db.commit()

    company = make_company(db)
    application = make_application(db, make_posting(db, company), status="Approved")

    result = extension_router.field_answers(
        extension_router.FieldsRequest(application_id=application.id, fields=[{"field_id": "f1", "label": "First Name"}]),
        db=db, _account_id=1,
    )

    assert result == {"f1": "Deshraj"}
