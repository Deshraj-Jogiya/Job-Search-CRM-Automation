import json

import pytest

from app import models
from app.routers import auth as auth_router
from app.services import demo_seed_service
from app.services.demo_seed_service import DemoSeedRefused, reset_demo_data, seed_demo_data


class TestGuards:
    def test_refuses_outside_showcase_mode(self, db, monkeypatch):
        monkeypatch.setenv("APP_MODE", "personal")
        with pytest.raises(DemoSeedRefused):
            seed_demo_data(db)
        with pytest.raises(DemoSeedRefused):
            reset_demo_data(db)
        assert db.query(models.JobApplication).count() == 0

    def test_reset_refuses_a_non_sqlite_database(self, db, monkeypatch):
        monkeypatch.setenv("APP_MODE", "showcase")
        monkeypatch.setattr(db.get_bind().dialect, "name", "postgresql")
        with pytest.raises(DemoSeedRefused):
            reset_demo_data(db)


class TestSeed:
    def test_fills_every_pipeline_stage(self, db, monkeypatch):
        monkeypatch.setenv("APP_MODE", "showcase")
        assert seed_demo_data(db) is True
        statuses = {a.status for a in db.query(models.JobApplication)}
        assert {"Ingested", "Needs Review", "Pending Confirmation", "Approved", "Applied",
                "Interviewing", "Offer", "Not Selected", "Rejected"} <= statuses
        assert db.query(models.InterviewPrep).count() == 2
        assert db.query(models.MockInterviewSession).filter_by(status="completed").count() == 1
        assert db.query(models.Company).filter_by(is_cap_exempt=True).count() == 1
        prep = db.query(models.ProfileInterviewPrep).one()
        version = db.query(models.ProfileVersion).filter_by(is_active=True).one()
        assert prep.profile_version_id == version.id  # counts as current, so nothing tries to regenerate it

    def test_every_tailored_document_parses_and_is_scored(self, db, monkeypatch):
        monkeypatch.setenv("APP_MODE", "showcase")
        seed_demo_data(db)
        for doc in db.query(models.TailoredDocument):
            assert doc.ats_score is not None
            if doc.document_type == "resume":
                assert json.loads(doc.content)["experience"]

    def test_only_fictional_contact_details(self, db, monkeypatch):
        monkeypatch.setenv("APP_MODE", "showcase")
        seed_demo_data(db)
        for msg in db.query(models.OutreachMessage):
            assert "example" in (msg.recipient_address or "")
        for app in db.query(models.JobApplication).filter(models.JobApplication.recruiter_email.isnot(None)):
            assert app.recruiter_email.endswith("@example.com")

    def test_second_seed_is_a_no_op(self, db, monkeypatch):
        monkeypatch.setenv("APP_MODE", "showcase")
        seed_demo_data(db)
        count = db.query(models.JobApplication).count()
        assert seed_demo_data(db) is False
        assert db.query(models.JobApplication).count() == count

    def test_reset_undoes_visitor_changes(self, db, monkeypatch):
        monkeypatch.setenv("APP_MODE", "showcase")
        seed_demo_data(db)
        db.query(models.JobApplication).update({models.JobApplication.status: "Applied"})
        db.add(models.AdminAccount(username="visitor", password_hash="x"))
        db.commit()
        reset_demo_data(db)
        assert db.query(models.AdminAccount).count() == 0
        assert db.query(models.JobApplication).filter_by(status="Interviewing").count() == 2
        assert db.query(models.ProfileVariant).count() == 1


def test_signup_is_closed_in_showcase_mode(db, monkeypatch):
    monkeypatch.setenv("APP_MODE", "showcase")
    response = auth_router.signup_submit(request=None, username="visitor", password="longenough1",
                                         confirm_password="longenough1", recovery_email="", db=db)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert db.query(models.AdminAccount).count() == 0


def test_daily_reset_is_scheduled_only_in_showcase_mode():
    # The reset job itself is guarded too; this keeps the scheduler from even registering it elsewhere.
    import inspect
    from app.services import scheduler
    assert "is_showcase_mode()" in inspect.getsource(scheduler.start_scheduler)
    assert demo_seed_service.reset_demo_data.__name__ in inspect.getsource(scheduler._demo_reset_tick)
