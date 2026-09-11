import pytest

from app.database import utcnow
from app.models import Company, JobApplication, JobPosting
from app.services import confirmation_service
from app.services.company_utils import normalize_company_name


def _application(db, status="Applied", applied_at=None):
    company = Company(name="Acme", normalized_name=normalize_company_name("Acme"))
    db.add(company)
    db.commit()
    db.refresh(company)
    posting = JobPosting(
        company_id=company.id, company_name_raw="Acme", job_title="Data Engineer",
        job_description="d", source="greenhouse",
    )
    db.add(posting)
    db.commit()
    db.refresh(posting)
    application = JobApplication(posting_id=posting.id, status=status, applied_at=applied_at)
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


class TestMarkReplied:
    def test_sets_replied_at(self, db):
        application = _application(db, applied_at=utcnow())
        confirmation_service.mark_replied(db, application.id)
        db.refresh(application)
        assert application.replied_at is not None

    def test_does_not_change_status(self, db):
        application = _application(db, status="Applied", applied_at=utcnow())
        confirmation_service.mark_replied(db, application.id)
        db.refresh(application)
        assert application.status == "Applied"

    def test_requires_applied_at_to_be_set(self, db):
        application = _application(db, status="Ingested", applied_at=None)
        with pytest.raises(confirmation_service.ConfirmationServiceError):
            confirmation_service.mark_replied(db, application.id)

    def test_can_be_marked_after_interviewing_too(self, db):
        application = _application(db, status="Interviewing", applied_at=utcnow())
        confirmation_service.mark_replied(db, application.id)
        db.refresh(application)
        assert application.replied_at is not None
        assert application.status == "Interviewing"
