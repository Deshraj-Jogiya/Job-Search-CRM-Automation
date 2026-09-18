"""progress_ingested_applications: the real fix for a serious gap found
live 2026-09-18 -- automated intake created real "Ingested"
JobApplication rows, but nothing ever automatically scored or tailored
them (605 had silently piled up with automation running for hours).
Mocks matching_service.score_application/tailoring_service.tailor_application
themselves rather than the LLM underneath -- this suite has no existing
LLM-mocking infrastructure for those (score_application makes a real
evaluate_match LLM call), and the real thing under test here is this
function's own contract with them: batch size, ordering, the tailor
threshold, and per-application error isolation."""

from conftest import make_company, make_posting

from app.models import GlobalSettings
from app.services import confirmation_service, matching_service, tailoring_service


def _settings(db, **overrides):
    settings = db.query(GlobalSettings).first()
    for key, value in overrides.items():
        setattr(settings, key, value)
    db.commit()
    return settings


def test_scores_and_tailors_applications_above_the_threshold(db, settings, monkeypatch):
    _settings(db, auto_score_batch_size=10, min_score_for_auto_tailor=50)
    company = make_company(db)
    posting = make_posting(db, company)
    from conftest import make_application

    application = make_application(db, posting, status="Ingested")

    def fake_score(db, application_id):
        app = db.get(type(application), application_id)
        app.match_score = 70
        db.commit()
        return app

    tailored_ids = []

    def fake_tailor(db, application_id):
        tailored_ids.append(application_id)

    monkeypatch.setattr(matching_service, "score_application", fake_score)
    monkeypatch.setattr(tailoring_service, "tailor_application", fake_tailor)

    confirmation_service.progress_ingested_applications(db)

    assert application.match_score == 70
    assert tailored_ids == [application.id]


def test_does_not_tailor_a_low_scoring_application(db, settings, monkeypatch):
    _settings(db, auto_score_batch_size=10, min_score_for_auto_tailor=50)
    company = make_company(db)
    posting = make_posting(db, company)
    from conftest import make_application

    application = make_application(db, posting, status="Ingested")

    def fake_score(db, application_id):
        app = db.get(type(application), application_id)
        app.match_score = 30  # below the 50 threshold
        db.commit()
        return app

    tailored_ids = []

    monkeypatch.setattr(matching_service, "score_application", fake_score)
    monkeypatch.setattr(tailoring_service, "tailor_application", lambda db, aid: tailored_ids.append(aid))

    confirmation_service.progress_ingested_applications(db)

    assert application.match_score == 30
    assert tailored_ids == []  # scored, but not worth spending tailoring cost on


def test_respects_the_batch_size_and_processes_oldest_first(db, settings, monkeypatch):
    _settings(db, auto_score_batch_size=2, min_score_for_auto_tailor=100)  # 100 = never tailor, just checking batching
    company = make_company(db)
    from conftest import make_application

    apps = []
    for i in range(4):
        posting = make_posting(db, company, job_url=f"https://example.com/job/{i}")
        apps.append(make_application(db, posting, status="Ingested"))

    scored_ids = []

    def fake_score(db, application_id):
        scored_ids.append(application_id)
        app = db.get(type(apps[0]), application_id)
        app.match_score = 0
        db.commit()
        return app

    monkeypatch.setattr(matching_service, "score_application", fake_score)
    monkeypatch.setattr(tailoring_service, "tailor_application", lambda db, aid: None)

    confirmation_service.progress_ingested_applications(db)

    # Only the batch size (2), oldest (lowest id) first -- not all 4 at once.
    assert scored_ids == [apps[0].id, apps[1].id]


def test_one_failing_application_does_not_stop_the_rest_of_the_batch(db, settings, monkeypatch):
    _settings(db, auto_score_batch_size=10, min_score_for_auto_tailor=100)
    company = make_company(db)
    from conftest import make_application

    posting1 = make_posting(db, company, job_url="https://example.com/job/a")
    posting2 = make_posting(db, company, job_url="https://example.com/job/b")
    app1 = make_application(db, posting1, status="Ingested")
    app2 = make_application(db, posting2, status="Ingested")

    def fake_score(db, application_id):
        if application_id == app1.id:
            raise RuntimeError("simulated LLM failure")
        app = db.get(type(app2), application_id)
        app.match_score = 0
        db.commit()
        return app

    monkeypatch.setattr(matching_service, "score_application", fake_score)
    monkeypatch.setattr(tailoring_service, "tailor_application", lambda db, aid: None)

    confirmation_service.progress_ingested_applications(db)  # must not raise

    assert app2.match_score == 0  # the second application still got processed
