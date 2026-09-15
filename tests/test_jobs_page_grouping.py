"""Real gap found 2026-09-15 via a full-codebase UX audit: the Jobs
page rendered every application in one flat, ungrouped, reverse-
chronological list -- no way to see "what needs a decision right now"
without scanning past everything else. _group_applications_by_stage is
the pure logic behind the fix; pure Python, no DB needed to test it."""

from types import SimpleNamespace

from app.routers.jobs import _group_applications_by_stage


def _app(status: str):
    return SimpleNamespace(status=status)


def test_needs_attention_group_covers_pending_confirmation_and_needs_review():
    apps = [_app("Pending Confirmation"), _app("Needs Review")]
    result = _group_applications_by_stage(apps)
    assert result["needs_attention"] == apps
    assert result["in_progress"] == []
    assert result["applied"] == []
    assert result["closed"] == []


def test_in_progress_group_covers_draft_ingested_tailored_approved():
    apps = [_app("Draft"), _app("Ingested"), _app("Tailored"), _app("Approved")]
    result = _group_applications_by_stage(apps)
    assert result["in_progress"] == apps


def test_applied_group_covers_applied_interviewing_offer():
    apps = [_app("Applied"), _app("Interviewing"), _app("Offer")]
    result = _group_applications_by_stage(apps)
    assert result["applied"] == apps


def test_closed_group_covers_rejected_and_not_selected():
    apps = [_app("Rejected"), _app("Not Selected")]
    result = _group_applications_by_stage(apps)
    assert result["closed"] == apps


def test_unrecognized_status_falls_into_in_progress_rather_than_vanishing():
    # A future status added later and not yet sorted into a group must
    # never silently disappear from the page.
    app = _app("Some Future Status")
    result = _group_applications_by_stage([app])
    assert result["in_progress"] == [app]


def test_every_group_key_always_present_even_when_empty():
    result = _group_applications_by_stage([])
    assert set(result.keys()) == {"needs_attention", "in_progress", "applied", "closed"}
    assert all(v == [] for v in result.values())


def test_preserves_order_within_each_group():
    apps = [_app("Applied"), _app("Rejected"), _app("Interviewing")]
    result = _group_applications_by_stage(apps)
    assert [a.status for a in result["applied"]] == ["Applied", "Interviewing"]
