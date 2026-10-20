"""Profile-level interview prep (app/services/profile_prep_service.py): general prep for any call, built from the
profile only. The LLM draft call itself is replaced with a stub, same convention as the rest of this suite; what is
tested is everything around it: the mechanical reference sections, confidential-project filtering, version
handling, and the figure check and missing-question check applied to whatever the model returns."""

import json
from unittest.mock import patch

import pytest
from sqlalchemy import event
from jinja2 import Environment, FileSystemLoader

from app import models
from app.services import profile_prep_service
from app.services.profile_prep_service import COMMON_QUESTIONS, ProfilePrepServiceError
from tests.conftest import make_variant

env = Environment(loader=FileSystemLoader("app/templates"))

PROFILE = {
    "name": "Test Candidate",
    "summary": "Data engineer.",
    "experience": [
        {"company": "Acme LLC", "employment_type": "Contract", "role": "Data Engineer", "date": "2025",
         "location": "Remote", "bullets": ["Cut ingestion time by 60%."]},
    ],
    "projects": [
        {"name": "Public Project", "date": "2026", "technologies": ["Python"], "bullets": ["Built a thing, 15 tests."]},
        {"name": "Secret Team Project", "confidential": True, "technologies": ["Go"], "bullets": ["Under NDA."]},
    ],
    "skills": {"data_engineering": ["SQL", "dbt"]},
    "education": [{"degree": "MS IT", "school": "ASU", "date": "2024"}],
    "certifications": ["Cert A"],
    "application_preferences": {
        "work_authorization": "Will require sponsorship now or in the future", "willing_to_relocate": "Yes",
        "salary_minimum": "$80,000+", "salary_negotiable": True, "notice_period": "2 weeks",
    },
}


def _story(db, variant, status):
    s = models.BehavioralStory(
        variant_id=variant.id, title=f"{status} story", situation="s", task="t", action="a", result="r",
        traits_json="[]", status=status,
    )
    db.add(s)
    db.commit()
    return s


def _stub_answers(**override):
    base = {
        "pitch_30_seconds": "I build data pipelines.", "pitch_90_seconds": "Longer pitch.",
        "answers": [{"question": q, "draft_answer": "An answer.", "quick_reference": "cue"} for q in COMMON_QUESTIONS],
        "strengths_to_steer_toward": ["Pipelines"],
        "honest_gaps": [{"gap": "Limited years", "honest_answer": "Three years, across several roles."}],
    }
    base.update(override)
    return base


def test_reference_sections_come_straight_from_the_profile_and_hide_confidential_projects(db):
    variant = make_variant(db, content=PROFILE)
    _story(db, variant, "draft")
    _story(db, variant, "confirmed")

    profile, v = profile_prep_service.load_profile(db)
    ref = profile_prep_service.build_reference(db, profile, v.id)

    assert [p["name"] for p in ref["projects"]] == ["Public Project"]
    assert ref["timeline"][0]["employment_type"] == "Contract"
    assert {"label": "Salary negotiable", "value": "Yes"} in ref["preferences"]
    assert {"label": "Notice period", "value": "2 weeks"} in ref["preferences"]
    assert [s.title for s in ref["stories"]] == ["confirmed story"]
    assert ref["skills"] == [{"name": "Data engineering", "tools": ["SQL", "dbt"]}]


def test_no_profile_raises_a_user_facing_error(db):
    with pytest.raises(ProfilePrepServiceError):
        profile_prep_service.load_profile(db)


def test_confidential_project_never_reaches_the_llm_prompt(db):
    make_variant(db, content=PROFILE)
    seen = {}

    def fake_draft(profile, stories):
        seen["profile"] = json.dumps(profile)
        return _stub_answers()

    with patch.object(profile_prep_service, "_draft_answers", fake_draft):
        profile_prep_service.generate_profile_prep(db)

    assert "Secret Team Project" not in seen["profile"]
    assert "Public Project" in seen["profile"]


def test_generate_stores_one_active_version_and_deactivates_the_old_one(db):
    variant = make_variant(db, content=PROFILE)
    with patch.object(profile_prep_service, "_draft_answers", lambda p, s: _stub_answers()):
        first = profile_prep_service.generate_profile_prep(db)
        second = profile_prep_service.generate_profile_prep(db)

    db.refresh(first)
    assert first.is_active is False and second.is_active is True
    prep, drafted = profile_prep_service.get_active_prep(db, variant.id)
    assert prep.id == second.id
    assert drafted["missing_questions"] == [] and drafted["grounding_warnings"] == []


def test_figure_not_in_the_profile_is_flagged_not_silently_kept(db):
    make_variant(db, content=PROFILE)
    answers = [{"question": q, "draft_answer": "An answer.", "quick_reference": ""} for q in COMMON_QUESTIONS]
    answers[0]["draft_answer"] = "I improved accuracy by 99% across the platform."
    stub = _stub_answers(
        answers=answers,
        honest_gaps=[{"gap": "g", "honest_answer": "I saved 70% of the time."}],
    )
    with patch.object(profile_prep_service, "_draft_answers", lambda p, s: stub):
        profile_prep_service.generate_profile_prep(db)

    _, drafted = profile_prep_service.get_active_prep(db, db.query(models.ProfileVariant).first().id)
    text = " ".join(drafted["grounding_warnings"])
    assert "99%" in text and "70%" in text
    assert not any("60%" in w for w in drafted["grounding_warnings"])  # 60% is in the profile


def test_questions_the_model_skipped_are_listed(db):
    make_variant(db, content=PROFILE)
    stub = _stub_answers(answers=[
        {"question": q, "draft_answer": "x", "quick_reference": ""} for q in COMMON_QUESTIONS[:-2]
    ])
    with patch.object(profile_prep_service, "_draft_answers", lambda p, s: stub):
        profile_prep_service.generate_profile_prep(db)

    _, drafted = profile_prep_service.get_active_prep(db, db.query(models.ProfileVariant).first().id)
    assert drafted["missing_questions"] == list(COMMON_QUESTIONS[-2:])


def test_llm_failure_is_wrapped_and_stores_nothing(db):
    make_variant(db, content=PROFILE)

    def boom(p, s):
        raise RuntimeError("provider down")

    with patch.object(profile_prep_service, "_draft_answers", boom):
        with pytest.raises(ProfilePrepServiceError):
            profile_prep_service.generate_profile_prep(db)
    assert db.query(models.ProfileInterviewPrep).count() == 0


_STATUS = {"state": "current", "generating": False, "last_error": None, "current_version_id": 1,
           "built_from_version_id": 1, "trigger": "manual", "auto_refresh": True, "current_version_at": None}


def _ctx(**extra):
    c = {"csrf_token": "t", "static_version": "0", "is_authenticated": True, "message": None, "error": None,
         "prep_status": _STATUS}
    c.update(extra)
    return c


def test_page_renders_reference_and_drafted_sections(db):
    variant = make_variant(db, content=PROFILE)
    with patch.object(profile_prep_service, "_draft_answers", lambda p, s: _stub_answers()):
        prep = profile_prep_service.generate_profile_prep(db)
    profile, v = profile_prep_service.load_profile(db)
    ref = profile_prep_service.build_reference(db, profile, v.id)
    _, drafted = profile_prep_service.get_active_prep(db, variant.id)

    html = env.get_template("profile_prep.html").render(**_ctx(setup_error=None, reference=ref, prep=prep, drafted=drafted))

    assert "Acme LLC · Contract" in html
    assert "Public Project" in html and "Secret Team Project" not in html
    assert "Tell me about yourself." in html
    assert "Refresh answers" in html and "Up to date" in html
    assert 'href="/interview-prep"' in html  # shared nav link present


def test_page_before_generation_offers_the_generate_button(db):
    variant = make_variant(db, content=PROFILE)
    profile, v = profile_prep_service.load_profile(db)
    ref = profile_prep_service.build_reference(db, profile, v.id)

    html = env.get_template("profile_prep.html").render(**_ctx(setup_error=None, reference=ref, prep=None, drafted=None))

    assert "Refresh answers" in html and "Nothing drafted yet." in html


def test_page_without_a_profile_points_to_the_profile_page():
    html = env.get_template("profile_prep.html").render(**_ctx(setup_error="No profile is set up yet."))
    assert "No profile is set up yet." in html and 'href="/profile"' in html


def test_generate_route_without_a_profile_redirects_with_the_error(db):
    from app.routers import profile_prep as router

    response = router.generate_profile_prep_now(db=db)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/interview-prep?error=")


def test_generate_route_starts_background_work_and_redirects_with_a_message(db):
    from app.routers import profile_prep as router

    make_variant(db, content=PROFILE)
    with patch.object(profile_prep_service, "start_background_generation", return_value=True) as start:
        response = router.generate_profile_prep_now(db=db)

    start.assert_called_once_with("manual")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/interview-prep?message=")


# ---- keeping the prep in step with the profile -------------------------------------------------------------


@pytest.fixture()
def auto_on(monkeypatch):
    monkeypatch.setenv("PROFILE_PREP_AUTO_REFRESH", "1")
    profile_prep_service._state.update({"generating": False, "last_error": None, "last_failure_at": 0.0, "timer": None})
    yield
    profile_prep_service._state.update({"generating": False, "last_error": None, "last_failure_at": 0.0, "timer": None})


def _new_active_version(db, variant, content):
    for v in db.query(models.ProfileVersion).filter_by(variant_id=variant.id, is_active=True):
        v.is_active = False
    db.add(models.ProfileVersion(variant_id=variant.id, content_json=json.dumps(content), source="manual", is_active=True))
    db.commit()


def _remove_listeners():
    event.remove(models.ProfileVersion, "after_insert", profile_prep_service._on_version_inserted)
    event.remove(models.ProfileVersion, "after_update", profile_prep_service._on_version_updated)
    profile_prep_service._listeners_registered = False


def test_status_goes_none_current_stale_as_the_profile_changes(db, settings):
    variant = make_variant(db, content=PROFILE)
    assert profile_prep_service.status(db)["state"] == "none"

    with patch.object(profile_prep_service, "_draft_answers", lambda p, s: _stub_answers()):
        prep = profile_prep_service.generate_profile_prep(db, trigger="initial")
    st = profile_prep_service.status(db)
    assert st["state"] == "current" and st["trigger"] == "initial"
    assert prep.profile_version_id == st["current_version_id"]

    _new_active_version(db, variant, {**PROFILE, "summary": "Changed."})
    st = profile_prep_service.status(db)
    assert st["state"] == "stale" and st["built_from_version_id"] != st["current_version_id"]


def test_catch_up_makes_the_initial_prep_then_leaves_a_current_one_alone(db, settings, auto_on):
    make_variant(db, content=PROFILE)
    calls = []

    def draft(p, s):
        calls.append(1)
        return _stub_answers()

    with patch.object(profile_prep_service, "_draft_answers", draft):
        assert profile_prep_service.catch_up(db) == "generated"
        assert profile_prep_service.catch_up(db) == "up to date"
    assert len(calls) == 1
    assert profile_prep_service.status(db)["trigger"] == "initial"


def test_catch_up_regenerates_after_a_profile_change_and_marks_the_trigger(db, settings, auto_on):
    variant = make_variant(db, content=PROFILE)
    with patch.object(profile_prep_service, "_draft_answers", lambda p, s: _stub_answers()):
        profile_prep_service.catch_up(db)
        _new_active_version(db, variant, {**PROFILE, "summary": "Changed."})
        assert profile_prep_service.catch_up(db) == "generated"
    st = profile_prep_service.status(db)
    assert st["state"] == "current" and st["trigger"] == "profile_changed"
    assert db.query(models.ProfileInterviewPrep).filter_by(is_active=True).count() == 1


def test_catch_up_respects_the_switch_and_the_failure_cooldown(db, settings, auto_on):
    make_variant(db, content=PROFILE)
    settings.profile_prep_auto_refresh = False
    db.commit()
    assert profile_prep_service.catch_up(db) == "auto refresh off"

    settings.profile_prep_auto_refresh = True
    db.commit()

    def boom(p, s):
        raise RuntimeError("no credits")

    with patch.object(profile_prep_service, "_draft_answers", boom):
        assert profile_prep_service.catch_up(db) == "generated"  # attempted, failed, recorded
        assert "no credits" in profile_prep_service.status(db)["last_error"]
        assert profile_prep_service.catch_up(db) == "cooling down after a failed attempt"
    assert db.query(models.ProfileInterviewPrep).count() == 0


def test_catch_up_without_a_profile_does_nothing(db, settings, auto_on):
    assert profile_prep_service.catch_up(db) == "no profile yet"


def test_only_one_generation_runs_at_a_time(db, settings, auto_on):
    make_variant(db, content=PROFILE)
    profile_prep_service._state["generating"] = True
    assert profile_prep_service.run_generation("manual") is False
    assert profile_prep_service.start_background_generation("manual") is False


def test_a_new_active_profile_version_schedules_a_refresh_and_an_inactive_one_does_not(db, settings):
    variant = make_variant(db, content=PROFILE)
    profile_prep_service.register_auto_refresh()
    try:
        with patch.object(profile_prep_service, "schedule_refresh_after_profile_change") as sched:
            _new_active_version(db, variant, {**PROFILE, "summary": "Changed."})
            assert sched.called
            sched.reset_mock()
            db.add(models.ProfileVersion(variant_id=variant.id, content_json="{}", source="linkedin_diff", is_active=False))
            db.commit()
            assert not sched.called
    finally:
        _remove_listeners()


def test_restoring_an_older_version_schedules_a_refresh(db, settings):
    variant = make_variant(db, content=PROFILE)
    _new_active_version(db, variant, {**PROFILE, "summary": "Changed."})
    older = db.query(models.ProfileVersion).filter_by(variant_id=variant.id, is_active=False).first()
    profile_prep_service.register_auto_refresh()
    try:
        with patch.object(profile_prep_service, "schedule_refresh_after_profile_change") as sched:
            older.is_active = True
            db.commit()
            assert sched.called
    finally:
        _remove_listeners()


def test_debounce_cancels_the_previous_timer_so_a_burst_makes_one_refresh(auto_on):
    with patch.object(profile_prep_service.threading, "Timer") as timer:
        profile_prep_service.schedule_refresh_after_profile_change()
        profile_prep_service.schedule_refresh_after_profile_change()
    assert timer.call_count == 2
    timer.return_value.cancel.assert_called_once()


def test_auto_refresh_is_off_in_the_showcase_instance(db, settings, auto_on, monkeypatch):
    monkeypatch.setattr(profile_prep_service, "is_showcase_mode", lambda: True)
    assert profile_prep_service.auto_refresh_enabled(db) is False


def test_auto_refresh_switch_route_saves_the_setting(db, settings):
    from app.routers import profile_prep as router

    router.set_auto_refresh(auto_refresh=False, db=db)
    db.refresh(settings)
    assert settings.profile_prep_auto_refresh is False
    router.set_auto_refresh(auto_refresh=True, db=db)
    db.refresh(settings)
    assert settings.profile_prep_auto_refresh is True


def test_status_banner_shows_stale_generating_and_never_drafted_states(db):
    reference = {"preferences": [], "timeline": [], "projects": [], "skills": {}, "education": [],
                 "certifications": [], "stories": [], "questions_to_ask": []}
    base = _ctx(setup_error=None, reference=reference, prep=None, drafted=None)
    stale = {**_STATUS, "state": "stale", "built_from_version_id": 1, "current_version_id": 2}
    template = env.get_template("profile_prep.html")

    html = template.render(**{**base, "prep_status": stale})
    assert "older than your profile" in html and "profile is now version 2" in html
    html = template.render(**{**base, "prep_status": {**stale, "generating": True}})
    assert "Updating now" in html and "disabled" in html
    html = template.render(**{**base, "prep_status": {**_STATUS, "state": "none", "built_from_version_id": None}})
    assert "Answers not drafted yet." in html
    from app.services.profile_prep_service import friendly_error

    raw = "credit balance is too low"
    html = template.render(**{**base, "prep_status": {**stale, "last_error": raw, "last_error_text": friendly_error(raw)}})
    assert "no credits left" in html and "credit balance is too low" not in html


def test_page_helpers_make_the_reference_readable():
    from app.services.profile_prep_service import _end_key, friendly_error, skill_group_name, split_bullet

    assert skill_group_name("generative_ai_llms_agentic_frameworks") == "Generative AI, LLMs and agents"
    assert split_bullet("Data Quality Testing: Wrote 15 dbt tests.") == {"label": "Data Quality Testing", "text": "Wrote 15 dbt tests."}
    assert split_bullet("Wrote 15 dbt tests.") == {"label": "", "text": "Wrote 15 dbt tests."}
    assert _end_key("Feb 2026 – Present") > _end_key("Sep 2026") > _end_key("Apr 2025 – Nov 2025")
    assert "no credits left" in friendly_error("Error code: 400 ... Your credit balance is too low")
    assert friendly_error(None) is None


def test_filler_words_are_dropped_but_real_time_stays():
    from app.services.profile_prep_service import split_bullet

    out = split_bullet("Pipeline: Built a real PySpark job, real-time inference on real-world data, actually ran it")
    assert out == {"label": "Pipeline", "text": "Built a PySpark job, real-time inference on real-world data, ran it"}
