"""Interview prep version history -- same pattern as ProfileVersion.
Regenerating used to silently overwrite the previous prep in place;
now every generation is a new row, exactly one active at a time, older
ones kept for history/restore. Covers the pure version-management
logic (list/restore) without needing the real LLM-calling generation
path -- InterviewPrep rows are constructed directly, same convention
as test_interview_prep_visibility.py and test_interview_prep_platform.py."""

import json

import pytest

from app import models
from app.services import interview_prep_service
from app.services.interview_prep_service import InterviewPrepServiceError
from tests.conftest import make_application, make_company, make_posting


def _make_version(db, application, is_active, general_prep_json="{}"):
    prep = models.InterviewPrep(
        application_id=application.id, general_prep_json=general_prep_json, is_active=is_active,
    )
    db.add(prep)
    db.commit()
    db.refresh(prep)
    return prep


def test_active_interview_prep_property_returns_the_active_one(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    _make_version(db, application, is_active=False, general_prep_json='{"old": true}')
    active = _make_version(db, application, is_active=True, general_prep_json='{"new": true}')

    db.refresh(application)
    assert application.active_interview_prep.id == active.id


def test_active_interview_prep_property_returns_none_with_no_versions(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)

    assert application.active_interview_prep is None


def test_list_interview_prep_versions_orders_most_recent_first(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    v1 = _make_version(db, application, is_active=False)
    v2 = _make_version(db, application, is_active=True)

    versions = interview_prep_service.list_interview_prep_versions(db, application.id)
    assert [v.id for v in versions] == [v2.id, v1.id]


def test_list_interview_prep_versions_empty_for_unknown_application(db):
    assert interview_prep_service.list_interview_prep_versions(db, 999) == []


def test_restore_interview_prep_version_flips_active_flag(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    old_version = _make_version(db, application, is_active=False)
    current_version = _make_version(db, application, is_active=True)

    restored = interview_prep_service.restore_interview_prep_version(db, old_version.id)

    assert restored.id == old_version.id
    assert restored.is_active is True
    db.refresh(current_version)
    assert current_version.is_active is False


def test_restore_already_active_version_is_a_noop(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    active_version = _make_version(db, application, is_active=True)

    result = interview_prep_service.restore_interview_prep_version(db, active_version.id)
    assert result.id == active_version.id
    assert result.is_active is True


def test_restore_nonexistent_version_raises(db):
    with pytest.raises(InterviewPrepServiceError):
        interview_prep_service.restore_interview_prep_version(db, 999)


def _make_rounds_version(db, application, rounds, is_active=True):
    prep = models.InterviewPrep(
        application_id=application.id,
        predicted_rounds_json=json.dumps({"rounds": rounds}),
        is_active=is_active,
    )
    db.add(prep)
    db.commit()
    db.refresh(prep)
    return prep


def test_add_networking_insight_appends_without_overwriting_existing_prep_focus(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    _make_rounds_version(
        db, application, [{"round_name": "PEI", "prep_focus": ["existing point"]}],
    )

    new_version = interview_prep_service.add_networking_insight_to_round(
        db, application.id, "PEI", "It's conversational, not formal"
    )

    rounds = json.loads(new_version.predicted_rounds_json)["rounds"]
    focus = rounds[0]["prep_focus"]
    assert "existing point" in focus  # nothing already there was lost
    assert any("It's conversational, not formal" in f for f in focus)
    assert any("Source: networking conversation" in f for f in focus)


def test_add_networking_insight_creates_a_new_version_not_an_in_place_edit(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    original = _make_rounds_version(db, application, [{"round_name": "PEI", "prep_focus": []}])

    new_version = interview_prep_service.add_networking_insight_to_round(db, application.id, "PEI", "a real insight")

    assert new_version.id != original.id
    db.refresh(original)
    assert original.is_active is False
    assert new_version.is_active is True
    db.refresh(application)
    assert application.active_interview_prep.id == new_version.id


def test_add_networking_insight_rejects_unknown_round(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    _make_rounds_version(db, application, [{"round_name": "PEI", "prep_focus": []}])

    with pytest.raises(InterviewPrepServiceError):
        interview_prep_service.add_networking_insight_to_round(db, application.id, "Nonexistent Round", "x")


def test_add_networking_insight_rejects_empty_text(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    _make_rounds_version(db, application, [{"round_name": "PEI", "prep_focus": []}])

    with pytest.raises(InterviewPrepServiceError):
        interview_prep_service.add_networking_insight_to_round(db, application.id, "PEI", "   ")


def test_add_networking_insight_requires_existing_prep(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)

    with pytest.raises(InterviewPrepServiceError):
        interview_prep_service.add_networking_insight_to_round(db, application.id, "PEI", "a real insight")


def test_restore_only_deactivates_versions_for_the_same_application(db):
    company = make_company(db)
    posting_a = make_posting(db, company)
    posting_b = make_posting(db, company, job_url="https://example.com/job/2")
    application_a = make_application(db, posting_a)
    application_b = make_application(db, posting_b)

    old_a = _make_version(db, application_a, is_active=False)
    _make_version(db, application_a, is_active=True)
    active_b = _make_version(db, application_b, is_active=True)

    interview_prep_service.restore_interview_prep_version(db, old_a.id)

    db.refresh(active_b)
    assert active_b.is_active is True  # untouched -- different application entirely
