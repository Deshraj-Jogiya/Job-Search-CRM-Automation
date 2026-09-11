from datetime import timedelta

import pytest

from app.database import utcnow
from app.models import Company, JobApplication, JobPosting, OutreachMessage
from app.services.company_utils import normalize_company_name
from app.services.outreach_hygiene import OutreachCapViolation, check_caps


def _company(db, **overrides):
    defaults = dict(name="Acme Corp", normalized_name=normalize_company_name("Acme Corp"))
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


def _application(db, company):
    posting = JobPosting(
        company_id=company.id, company_name_raw=company.name, job_title="Data Engineer",
        job_description="d", source="greenhouse",
    )
    db.add(posting)
    db.commit()
    db.refresh(posting)
    application = JobApplication(posting_id=posting.id)
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


def _sent_message(db, application, recipient_address, sent_at=None):
    message = OutreachMessage(
        application_id=application.id, body="hi", recipient_address=recipient_address,
        status="Sent", sent_at=sent_at or utcnow(),
    )
    db.add(message)
    db.commit()
    return message


class TestPersonLifetimeCap:
    def test_first_message_to_a_new_person_is_allowed(self, db, settings):
        company = _company(db)
        check_caps(db, "recruiter@acme.com", company.id, settings)  # no raise

    def test_second_message_to_same_person_violates_default_cap(self, db, settings):
        company = _company(db)
        application = _application(db, company)
        _sent_message(db, application, "recruiter@acme.com")

        with pytest.raises(OutreachCapViolation):
            check_caps(db, "recruiter@acme.com", company.id, settings)

    def test_only_sent_messages_count_not_drafts(self, db, settings):
        company = _company(db)
        application = _application(db, company)
        db.add(OutreachMessage(application_id=application.id, body="hi", recipient_address="recruiter@acme.com", status="Draft"))
        db.commit()

        check_caps(db, "recruiter@acme.com", company.id, settings)  # draft doesn't count -- no raise

    def test_raising_the_cap_setting_allows_a_second_message(self, db, settings):
        company = _company(db)
        application = _application(db, company)
        _sent_message(db, application, "recruiter@acme.com")
        settings.outreach_per_person_lifetime = 2

        check_caps(db, "recruiter@acme.com", company.id, settings)  # no raise


class TestCompanyRollingWindowCap:
    def test_under_the_cap_is_allowed(self, db, settings):
        company = _company(db)
        application = _application(db, company)
        _sent_message(db, application, "a@acme.com")
        _sent_message(db, application, "b@acme.com")

        check_caps(db, "new-person@acme.com", company.id, settings)  # 2 < default cap of 10, no raise

    def test_at_the_cap_violates(self, db, settings):
        # Default cap is 10, not the original (relaxed) 3 -- the per-
        # company hygiene cap exists to prevent genuine duplicates, not
        # to throttle normal outreach volume.
        company = _company(db)
        application = _application(db, company)
        for i in range(10):
            _sent_message(db, application, f"person{i}@acme.com")

        with pytest.raises(OutreachCapViolation):
            check_caps(db, "new-person@acme.com", company.id, settings)

    def test_messages_outside_the_rolling_window_dont_count(self, db, settings):
        company = _company(db)
        application = _application(db, company)
        old = utcnow() - timedelta(days=settings.outreach_per_company_window_days + 5)
        for i in range(3):
            _sent_message(db, application, f"person{i}@acme.com", sent_at=old)

        check_caps(db, "new-person@acme.com", company.id, settings)  # all 3 outside the window, no raise

    def test_different_company_is_unaffected(self, db, settings):
        company_a = _company(db, name="A Co", normalized_name="a co")
        company_b = _company(db, name="B Co", normalized_name="b co")
        application_a = _application(db, company_a)
        for i in range(3):
            _sent_message(db, application_a, f"person{i}@acme.com")

        check_caps(db, "new-person@bco.com", company_b.id, settings)  # company B untouched, no raise
