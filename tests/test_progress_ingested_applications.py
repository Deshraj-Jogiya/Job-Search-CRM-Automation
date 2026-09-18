"""progress_ingested_applications: the real fix for a serious gap found
live 2026-09-18 -- automated intake created real "Ingested"
JobApplication rows, but nothing ever automatically scored or tailored
them (605 had silently piled up with automation running for hours).
Mocks matching_service.score_application/tailoring_service.tailor_application
themselves rather than the LLM underneath -- this suite has no existing
LLM-mocking infrastructure for those (score_application makes a real
evaluate_match LLM call), and the real thing under test here is this
function's own contract with them: batch size, ordering, the tailor
threshold, and per-application error isolation.

Production processes each application on its own thread with its own
DB session (real, necessary concurrency -- see the function's own
docstring for why sequential processing doesn't reliably finish within
one scheduler tick). These tests therefore always re-query by id after
calling it, rather than trusting a Python object loaded before the
call -- the test's own session has no reason to see a different
session's committed writes without an explicit refresh, same as any
other real concurrent-writer scenario."""

from conftest import make_application, make_company, make_posting

from app.models import GlobalSettings, JobApplication
from app.services import confirmation_service, matching_service, tailoring_service


def _settings(db, **overrides):
    settings = db.query(GlobalSettings).first()
    for key, value in overrides.items():
        setattr(settings, key, value)
    db.commit()
    return settings


def _reload(db, application_id):
    db.expire_all()
    return db.query(JobApplication).filter(JobApplication.id == application_id).first()


def test_scores_and_tailors_applications_above_the_threshold(db, settings, monkeypatch):
    _settings(db, auto_score_batch_size=10, min_score_for_auto_tailor=50)
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting, status="Ingested")
    application_id = application.id

    def fake_score(db, application_id):
        app = db.query(JobApplication).filter(JobApplication.id == application_id).first()
        app.match_score = 70
        db.commit()
        return app

    tailored_ids = []

    def fake_tailor(db, application_id):
        tailored_ids.append(application_id)

    monkeypatch.setattr(matching_service, "score_application", fake_score)
    monkeypatch.setattr(tailoring_service, "tailor_application", fake_tailor)

    confirmation_service.progress_ingested_applications(db)

    assert _reload(db, application_id).match_score == 70
    assert tailored_ids == [application_id]


def test_does_not_tailor_a_low_scoring_application(db, settings, monkeypatch):
    _settings(db, auto_score_batch_size=10, min_score_for_auto_tailor=50)
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting, status="Ingested")
    application_id = application.id

    def fake_score(db, application_id):
        app = db.query(JobApplication).filter(JobApplication.id == application_id).first()
        app.match_score = 30  # below the 50 threshold
        db.commit()
        return app

    tailored_ids = []

    monkeypatch.setattr(matching_service, "score_application", fake_score)
    monkeypatch.setattr(tailoring_service, "tailor_application", lambda db, aid: tailored_ids.append(aid))

    confirmation_service.progress_ingested_applications(db)

    assert _reload(db, application_id).match_score == 30
    assert tailored_ids == []  # scored, but not worth spending tailoring cost on


def test_respects_the_batch_size_and_processes_oldest_first(db, settings, monkeypatch):
    _settings(db, auto_score_batch_size=2, min_score_for_auto_tailor=100)  # 100 = never tailor, just checking batching
    company = make_company(db)

    app_ids = []
    for i in range(4):
        posting = make_posting(db, company, job_url=f"https://example.com/job/{i}")
        app_ids.append(make_application(db, posting, status="Ingested").id)

    scored_ids = []

    def fake_score(db, application_id):
        scored_ids.append(application_id)
        app = db.query(JobApplication).filter(JobApplication.id == application_id).first()
        app.match_score = 0
        db.commit()
        return app

    monkeypatch.setattr(matching_service, "score_application", fake_score)
    monkeypatch.setattr(tailoring_service, "tailor_application", lambda db, aid: None)

    confirmation_service.progress_ingested_applications(db)

    # Only the batch size (2), oldest (lowest id) first -- not all 4 at once.
    assert sorted(scored_ids) == sorted(app_ids[:2])


def test_one_failing_application_does_not_stop_the_rest_of_the_batch(db, settings, monkeypatch):
    _settings(db, auto_score_batch_size=10, min_score_for_auto_tailor=100)
    company = make_company(db)

    posting1 = make_posting(db, company, job_url="https://example.com/job/a")
    posting2 = make_posting(db, company, job_url="https://example.com/job/b")
    app1_id = make_application(db, posting1, status="Ingested").id
    app2_id = make_application(db, posting2, status="Ingested").id

    def fake_score(db, application_id):
        if application_id == app1_id:
            raise RuntimeError("simulated LLM failure")
        app = db.query(JobApplication).filter(JobApplication.id == application_id).first()
        app.match_score = 0
        db.commit()
        return app

    monkeypatch.setattr(matching_service, "score_application", fake_score)
    monkeypatch.setattr(tailoring_service, "tailor_application", lambda db, aid: None)

    confirmation_service.progress_ingested_applications(db)  # must not raise

    assert _reload(db, app2_id).match_score == 0  # the second application still got processed


def test_null_settings_do_not_crash_the_batch(db, settings, monkeypatch):
    """Real bug found live 2026-09-18, minutes after this function first
    deployed: a migration adding a new nullable column to GlobalSettings
    (a pre-existing singleton row) never backfills that existing row --
    the Column's Python-side default only applies to a brand-new row.
    The live settings row genuinely had min_score_for_auto_tailor=NULL,
    and an unguarded int-vs-None comparison crashed the ENTIRE batch via
    pool.map()'s list(), not just one application. Both settings used
    here get an explicit `or` fallback specifically to survive this."""
    _settings(db, auto_score_batch_size=None, min_score_for_auto_tailor=None)
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting, status="Ingested")
    application_id = application.id

    def fake_score(db, application_id):
        app = db.query(JobApplication).filter(JobApplication.id == application_id).first()
        app.match_score = 70
        db.commit()
        return app

    tailored_ids = []

    monkeypatch.setattr(matching_service, "score_application", fake_score)
    monkeypatch.setattr(tailoring_service, "tailor_application", lambda db, aid: tailored_ids.append(aid))

    confirmation_service.progress_ingested_applications(db)  # must not raise

    # Falls back to the documented defaults (50 for the tailor threshold)
    # -- 70 >= 50, so this should still have been tailored, not silently
    # dropped just because the setting itself was NULL.
    assert tailored_ids == [application_id]


def test_a_low_scoring_application_is_never_reselected_on_a_later_call(db, settings, monkeypatch):
    """Real, serious bug found live 2026-09-18, minutes after this first
    deployed: filtering the batch on status == "Ingested" ALONE wasn't
    enough -- a low-scoring application (correctly, deliberately left
    at "Ingested" rather than tailored) never leaves that status, so it
    kept getting re-selected and re-scored by EVERY subsequent call,
    forever. Confirmed live: the same ~12 real postings got scored
    again, and again, every 2 minutes, while genuinely untouched
    applications further back in the queue never got reached at all.
    match_analysis_json (never NULL after a real score, regardless of
    the score's value) is what actually excludes an already-scored
    application from being picked up again -- match_score itself can't
    be used for this (its own Column default=0 means a freshly-ingested,
    NEVER-scored row already reads 0, not NULL, confirmed directly
    against real data before trusting this)."""
    _settings(db, auto_score_batch_size=10, min_score_for_auto_tailor=50)
    company = make_company(db)
    posting = make_posting(db, company)
    application_id = make_application(db, posting, status="Ingested").id

    score_calls = []

    def fake_score(db, application_id):
        score_calls.append(application_id)
        app = db.query(JobApplication).filter(JobApplication.id == application_id).first()
        app.match_score = 20  # below the 50 threshold -- stays "Ingested"
        app.match_analysis_json = "{}"  # what a real score_application call always sets
        db.commit()
        return app

    monkeypatch.setattr(matching_service, "score_application", fake_score)
    monkeypatch.setattr(tailoring_service, "tailor_application", lambda db, aid: None)

    confirmation_service.progress_ingested_applications(db)
    confirmation_service.progress_ingested_applications(db)  # a later tick

    assert score_calls == [application_id]  # only ever scored once, not every call
    assert _reload(db, application_id).status == "Ingested"  # correctly still not tailored


def test_a_high_scoring_application_stuck_after_a_tailoring_failure_gets_retried(db, settings, monkeypatch):
    """Second real gap found the same day, right after the fix above:
    excluding every already-scored row also silently orphaned an
    application that scored WELL but whose tailor_application call
    itself failed once (confirmed live: 4 real applications stuck
    exactly this way from a transient LLM/JSON error earlier the same
    session) -- without this, a genuinely good match never got a second
    chance. A low scorer stays correctly excluded forever (this
    function's own design never tailors it); only a high scorer whose
    tailoring hasn't actually succeeded yet should be retried."""
    _settings(db, auto_score_batch_size=10, min_score_for_auto_tailor=50)
    company = make_company(db)
    posting = make_posting(db, company)
    application_id = make_application(db, posting, status="Ingested").id

    # Simulates the real stuck state directly: already scored well, but
    # tailoring never actually succeeded (status never left "Ingested").
    stuck = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    stuck.match_score = 80
    stuck.match_analysis_json = "{}"
    db.commit()

    tailored_ids = []

    monkeypatch.setattr(matching_service, "score_application", lambda db, aid: db.query(JobApplication).filter(JobApplication.id == aid).first())
    monkeypatch.setattr(tailoring_service, "tailor_application", lambda db, aid: tailored_ids.append(aid))

    confirmation_service.progress_ingested_applications(db)

    assert tailored_ids == [application_id]  # got a real second chance, not abandoned
