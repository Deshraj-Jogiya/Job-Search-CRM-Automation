"""Covers only the outreach-hygiene wiring added to draft_outreach_message
(see outreach_hygiene.py) -- not full outreach_service coverage, which
predates this pass and has no existing test file."""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.models import Company, JobApplication, JobPosting, ProfileVariant, ProfileVersion
from app.services import outreach_service
from app.services.company_utils import normalize_company_name


def _application(db):
    variant = ProfileVariant(name="Data Engineering", is_default=True)
    db.add(variant)
    db.commit()
    db.refresh(variant)
    version = ProfileVersion(
        variant_id=variant.id,
        content_json=json.dumps({"name": "Test Candidate", "summary": "A candidate.", "experience": []}),
        source="manual", is_active=True,
    )
    db.add(version)
    db.commit()

    company = Company(name="Acme Corp", normalized_name=normalize_company_name("Acme Corp"))
    db.add(company)
    db.commit()
    db.refresh(company)
    posting = JobPosting(
        company_id=company.id, company_name_raw="Acme Corp", job_title="Data Engineer",
        job_description="d", source="greenhouse",
    )
    db.add(posting)
    db.commit()
    db.refresh(posting)
    application = JobApplication(posting_id=posting.id)
    db.add(application)
    db.commit()
    db.refresh(application)
    return application, company


def _mock_llm():
    llm = MagicMock()
    llm.complete_text.return_value = "Hi there, I'm interested in this role."
    return llm


class TestDraftOutreachMessageCaps:
    def test_second_draft_to_same_person_is_blocked(self, db, settings):
        application, _ = _application(db)
        with patch("app.services.outreach_service.get_llm_provider", return_value=_mock_llm()):
            outreach_service.draft_outreach_message(
                db, application.id, "Jane Recruiter", "jane@acme.com", "email",
            )
            # First message is still just a Draft -- manually mark it Sent
            # to simulate the real state check_caps() looks at.
            from app.models import OutreachMessage
            from app.database import utcnow
            msg = db.query(OutreachMessage).filter(OutreachMessage.application_id == application.id).first()
            msg.status = "Sent"
            msg.sent_at = utcnow()
            db.commit()

            with pytest.raises(outreach_service.OutreachServiceError):
                outreach_service.draft_outreach_message(
                    db, application.id, "Jane Recruiter", "jane@acme.com", "email",
                )

    def test_override_reason_bypasses_the_cap_and_is_recorded(self, db, settings):
        application, _ = _application(db)
        with patch("app.services.outreach_service.get_llm_provider", return_value=_mock_llm()):
            outreach_service.draft_outreach_message(db, application.id, "Jane", "jane@acme.com", "email")
            from app.models import OutreachMessage
            from app.database import utcnow
            msg = db.query(OutreachMessage).filter(OutreachMessage.application_id == application.id).first()
            msg.status = "Sent"
            msg.sent_at = utcnow()
            db.commit()

            second = outreach_service.draft_outreach_message(
                db, application.id, "Jane", "jane@acme.com", "email",
                override_reason="She asked me to follow up directly.",
            )
            assert second.cap_override_reason == "She asked me to follow up directly."

    def test_no_prior_messages_drafts_normally(self, db, settings):
        application, _ = _application(db)
        with patch("app.services.outreach_service.get_llm_provider", return_value=_mock_llm()):
            message = outreach_service.draft_outreach_message(db, application.id, "Jane", "jane@acme.com", "email")
        assert message.status == "Draft"
        assert message.cap_override_reason is None
