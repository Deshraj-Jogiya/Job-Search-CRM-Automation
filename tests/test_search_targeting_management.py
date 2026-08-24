"""Search keywords, seniority exclusions, and location exclusions share
one removal model: no hard-delete reachable from the app, only pause
(toggle) -- a paused term stays recoverable via the "paused" dropdown
route (reactivate_*) instead of needing to be retyped from memory.
Covers that the reactivate routes actually restore is_active=True, and
that the old hard-delete routes are genuinely gone, not just unused."""

import pytest

from app.models import LocationExclusion, SearchKeyword, SeniorityExclusion
from app.routers import jobs as jobs_router


def test_reactivate_keyword_restores_a_paused_one(db):
    kw = SearchKeyword(keyword="Data Engineer", is_active=False)
    db.add(kw)
    db.commit()
    db.refresh(kw)

    jobs_router.reactivate_keyword(keyword_id=kw.id, db=db)

    db.refresh(kw)
    assert kw.is_active is True


def test_reactivate_seniority_exclusion_restores_a_paused_one(db):
    ex = SeniorityExclusion(term="Staff", is_active=False)
    db.add(ex)
    db.commit()
    db.refresh(ex)

    jobs_router.reactivate_seniority_exclusion(exclusion_id=ex.id, db=db)

    db.refresh(ex)
    assert ex.is_active is True


def test_reactivate_location_exclusion_restores_a_paused_one(db):
    ex = LocationExclusion(term="India", is_active=False)
    db.add(ex)
    db.commit()
    db.refresh(ex)

    jobs_router.reactivate_location_exclusion(exclusion_id=ex.id, db=db)

    db.refresh(ex)
    assert ex.is_active is True


def test_reactivate_unknown_id_returns_an_error_redirect_not_a_crash(db):
    response = jobs_router.reactivate_keyword(keyword_id=999999, db=db)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


@pytest.mark.parametrize(
    "attr",
    ["delete_keyword", "delete_seniority_exclusion", "delete_location_exclusion"],
)
def test_hard_delete_routes_no_longer_exist(attr):
    """A real, permanent hard-delete-by-misclick was the whole problem --
    confirms the old routes were actually removed, not just orphaned."""
    assert not hasattr(jobs_router, attr)
