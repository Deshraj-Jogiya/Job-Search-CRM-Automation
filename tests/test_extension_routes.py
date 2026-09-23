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


class TestDocumentForAttachment:
    """Real PDF bytes for the content script to attach via the
    DataTransfer API -- see extension/content.js's attachDocument.
    Uses the exact same rendering path (page_fit_service/
    document_render_service) the existing download route and
    Playwright's own autofill already use, not a second copy."""

    def _resume_content(self):
        return {
            "name": "Test Candidate", "title": "Data Engineer",
            "contact": {"email": "t@example.com", "phone": "555-1234", "location": "Austin, TX"},
            "summary": "A concise summary.",
            "skills": {"Languages": ["Python", "SQL"]},
            "experience": [{"role": "Engineer", "company": "Acme", "location": "Remote", "date": "2023 - Present", "bullets": ["Did a real thing."]}],
            "projects": [],
            "education": [{"degree": "B.S. Computer Science", "school": "State University", "date": "2020"}],
            "certifications": [],
        }

    def test_returns_a_real_pdf_for_the_real_tailored_resume(self, db, settings):
        import json
        from app.models import TailoredDocument

        company = make_company(db)
        application = make_application(db, make_posting(db, company, company_name_raw="Acme", job_title="Data Engineer"), status="Approved")
        db.add(TailoredDocument(application_id=application.id, document_type="resume", content=json.dumps(self._resume_content())))
        db.commit()

        response = extension_router.document_for_attachment(application.id, "resume", db=db, _account_id=1)

        assert response.status_code == 200
        assert response.media_type == "application/pdf"
        assert response.body[:4] == b"%PDF"  # a real PDF, not an error page or empty response
        assert "X-Filename" in response.headers

    def test_returns_a_real_pdf_for_the_real_tailored_cover_letter(self, db, settings):
        import json
        from app.models import TailoredDocument

        company = make_company(db)
        application = make_application(db, make_posting(db, company, company_name_raw="Acme", job_title="Data Engineer"), status="Approved")
        db.add(TailoredDocument(application_id=application.id, document_type="resume", content=json.dumps(self._resume_content())))
        db.add(TailoredDocument(application_id=application.id, document_type="cover_letter", content="Dear Hiring Manager, I am excited to apply."))
        db.commit()

        response = extension_router.document_for_attachment(application.id, "cover_letter", db=db, _account_id=1)

        assert response.status_code == 200
        assert response.body[:4] == b"%PDF"

    def test_404_shaped_error_when_nothing_tailored_yet(self, db, settings):
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        with pytest.raises(HTTPException) as exc_info:
            extension_router.document_for_attachment(application.id, "resume", db=db, _account_id=1)

        assert exc_info.value.status_code == 400

    def test_unknown_document_type_rejected(self, db, settings):
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        with pytest.raises(HTTPException) as exc_info:
            extension_router.document_for_attachment(application.id, "something-else", db=db, _account_id=1)

        assert exc_info.value.status_code == 400


class _SyncThread:
    """Runs the "background" target synchronously and immediately,
    instead of on a real thread -- makes these tests deterministic
    rather than racing a real spawned thread."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class TestAddJobRoute:
    """The popup's "+ Add This Job in One Click" action."""

    def test_creates_the_application_and_returns_its_id(self, db, settings):
        from unittest.mock import patch

        with patch("app.routers.extension.threading.Thread", _SyncThread), \
             patch("app.routers.extension._score_and_tailor_in_background") as mock_bg:
            result = extension_router.add_job(
                extension_router.AddJobRequest(
                    url="https://example.com/careers/123", job_title="Data Engineer",
                    company_name="Acme Analytics", job_description="A" * 60,
                ),
                db=db, _account_id=1,
            )

        assert "application_id" in result
        mock_bg.assert_called_once_with(result["application_id"])

    def test_400_shaped_error_when_the_page_has_no_real_job_title(self, db, settings):
        with pytest.raises(HTTPException) as exc_info:
            extension_router.add_job(
                extension_router.AddJobRequest(
                    url="https://example.com", job_title="", company_name="Acme", job_description="A" * 60
                ),
                db=db, _account_id=1,
            )

        assert exc_info.value.status_code == 400


class TestTailorExistingApplicationRoute:
    """The popup's "Generate Tailored Resume + Cover Letter" action for
    an application the extension already matched."""

    def test_kicks_off_background_tailoring_for_a_real_application(self, db, settings):
        from unittest.mock import patch

        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        with patch("app.routers.extension.threading.Thread", _SyncThread), \
             patch("app.routers.extension._tailor_existing_in_background") as mock_bg:
            result = extension_router.tailor_existing_application(application.id, db=db, _account_id=1)

        assert result == {"ok": True}
        mock_bg.assert_called_once_with(application.id)

    def test_404_for_an_application_that_does_not_exist(self, db, settings):
        with pytest.raises(HTTPException) as exc_info:
            extension_router.tailor_existing_application(999999, db=db, _account_id=1)

        assert exc_info.value.status_code == 404


class TestMarkAppliedFromExtensionRoute:
    """Real gap closed 2026-09-23: the VM's old Playwright autofill had a
    real submission-confirmation watcher with direct server-side access
    to the browser tab it launched; the extension had no equivalent --
    confirmed live, a real Checkr submission through it stayed
    'Approved' forever, applied_at never set. content.js's own client-
    side watcher calls this route the moment it recognizes a real
    post-submit confirmation page."""

    def test_marks_a_real_approved_application_applied(self, db, settings):
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Approved")

        result = extension_router.mark_applied_from_extension(application.id, db=db, _account_id=1)

        assert result == {"ok": True}
        db.refresh(application)
        assert application.status == "Applied"
        assert application.applied_at is not None

    def test_404_for_an_application_that_does_not_exist(self, db, settings):
        with pytest.raises(HTTPException) as exc_info:
            extension_router.mark_applied_from_extension(999999, db=db, _account_id=1)

        assert exc_info.value.status_code == 404

    def test_already_applied_is_a_real_success_not_an_error(self, db, settings):
        # Real race this guards against: the human clicks "Mark as
        # Applied" manually while the client-side watcher is still
        # polling the same page -- both paths call the same
        # confirmation_service.mark_applied, which correctly refuses a
        # second transition. That must not surface as a scary error to
        # the extension over something that already succeeded.
        company = make_company(db)
        application = make_application(db, make_posting(db, company), status="Applied")

        result = extension_router.mark_applied_from_extension(application.id, db=db, _account_id=1)

        assert result["ok"] is True
        assert "already_handled" in result
