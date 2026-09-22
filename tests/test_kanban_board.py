"""Kanban board (2026-09-16 Long Build, done after explicit deferral).

The one real risk here: every status change already goes through a
guarded confirmation_service function with real side effects
(applied_at/interviewing_at/offer_at/not_selected_at timestamps that
analytics_service/metrics_service key off, Company.ghosted_count,
autofill auto-launch on Approve). A naive drag-and-drop that just wrote
application.status directly would silently skip all of that. These tests
verify the board's write path (kanban_move) dispatches through the real
guarded functions -- never a raw status write -- and that an invalid
drag (wrong source status, or a target with no valid manual transition
at all) is rejected with the real error, not silently accepted."""

import json
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from conftest import make_application, make_company, make_posting

from app.routers import jobs as jobs_router
from app.services import confirmation_service


def test_every_interactive_column_has_a_real_dispatch_target():
    interactive_statuses = {status for status, interactive in jobs_router.KANBAN_COLUMNS if interactive}
    assert interactive_statuses == set(confirmation_service.KANBAN_TRANSITIONS) | {"Needs Review", "Pending Confirmation"}
    # Needs Review/Pending Confirmation are real interactive columns (a
    # card can be dragged OUT of them), just never a drag TARGET -- there
    # is no function that moves an application back INTO either by hand.


def test_auto_columns_have_no_dispatch_target_at_all():
    auto_statuses = {status for status, interactive in jobs_router.KANBAN_COLUMNS if not interactive}
    assert auto_statuses == {"Ingested", "Tailored"}
    assert not auto_statuses & set(confirmation_service.KANBAN_TRANSITIONS)


def test_valid_source_statuses_mirrors_the_real_guard_clauses():
    # Spot-check against the literal precondition in each guarded
    # function (confirmation_service.py) -- if these ever drift apart,
    # the board's client-side onMove would allow a drag the server then
    # rejects, a real (if minor) regression this test exists to catch.
    assert confirmation_service.KANBAN_VALID_SOURCE_STATUSES["Applied"] == ("Approved",)
    assert confirmation_service.KANBAN_VALID_SOURCE_STATUSES["Interviewing"] == ("Applied",)
    assert set(confirmation_service.KANBAN_VALID_SOURCE_STATUSES["Offer"]) == {"Applied", "Interviewing"}
    assert set(confirmation_service.KANBAN_VALID_SOURCE_STATUSES["Not Selected"]) == {"Applied", "Interviewing"}
    assert set(confirmation_service.KANBAN_VALID_SOURCE_STATUSES["Approved"]) == {"Needs Review", "Pending Confirmation"}
    assert set(confirmation_service.KANBAN_VALID_SOURCE_STATUSES["Rejected"]) == {"Needs Review", "Pending Confirmation"}


def test_valid_drag_dispatches_through_the_real_guarded_function_with_real_side_effects(db, settings):
    company = make_company(db)
    posting = make_posting(db, company, source="linkedin")  # not autofill-supported, keeps this test focused
    application = make_application(db, posting, status="Applied")

    response = jobs_router.kanban_move(application.id, target_status="Interviewing", db=db)

    db.refresh(application)
    assert application.status == "Interviewing"
    assert application.interviewing_at is not None  # the real side effect a raw status write would have skipped
    assert json.loads(response.body)["ok"] is True


def test_drag_to_approved_no_longer_launches_the_retired_vm_autofill(db, settings):
    """Real behavior change, 2026-09-22, not a regression: the VM's own
    Playwright autofill is retired (autofill_service.is_supported now
    always returns False). Dragging to Approved on the Kanban board
    still approves the application -- that's the human's own explicit
    decision, independent of autofill support -- it just no longer
    tries to launch a browser against a display server that no longer
    exists. Renamed and updated from a test that used to assert the
    opposite, kept rather than deleted since the approve-on-drag
    behavior itself still needs covering."""
    company = make_company(db)
    posting = make_posting(db, company, source="greenhouse")
    application = make_application(db, posting, status="Needs Review")

    with patch("app.services.autofill_service.launch_autofill_in_background") as launch_mock:
        response = jobs_router.kanban_move(application.id, target_status="Approved", db=db)

    db.refresh(application)
    assert application.status == "Approved"
    launch_mock.assert_not_called()
    assert json.loads(response.body)["message"] == "Approved."


def test_drag_from_wrong_source_status_is_rejected_not_silently_applied(db, settings):
    company = make_company(db)
    posting = make_posting(db, company, source="linkedin")
    application = make_application(db, posting, status="Ingested")  # not Applied/Interviewing

    response = jobs_router.kanban_move(application.id, target_status="Offer", db=db)

    db.refresh(application)
    assert application.status == "Ingested"  # unchanged
    assert application.offer_at is None
    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["ok"] is False
    assert "Ingested" in body["error"]


def test_drag_into_a_pipeline_automatic_column_is_rejected(db, settings):
    # No confirmation_service function moves anything INTO Tailored by
    # hand -- tailoring_service.py sets it only after a real LLM call
    # succeeds. The board must never pretend this is a valid drop.
    company = make_company(db)
    posting = make_posting(db, company, source="linkedin")
    application = make_application(db, posting, status="Ingested")

    response = jobs_router.kanban_move(application.id, target_status="Tailored", db=db)

    db.refresh(application)
    assert application.status == "Ingested"
    assert response.status_code == 400
    assert "automatically by the pipeline" in json.loads(response.body)["error"]


def test_kanban_page_groups_every_real_status_into_its_column(db, settings):
    company = make_company(db)
    statuses = ["Ingested", "Tailored", "Needs Review", "Pending Confirmation", "Approved",
                "Applied", "Interviewing", "Offer", "Not Selected", "Rejected"]
    apps = [make_application(db, make_posting(db, company), status=s) for s in statuses]

    columns = jobs_router._group_applications_by_status(apps)

    for app, status in zip(apps, statuses):
        assert columns[status] == [app]


def test_kanban_page_raises_loudly_on_an_unrecognized_status_rather_than_dropping_the_card(db, settings):
    company = make_company(db)
    application = make_application(db, make_posting(db, company), status="Some Future Status")

    with pytest.raises(HTTPException):
        jobs_router._group_applications_by_status([application])
