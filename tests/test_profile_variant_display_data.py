"""Real inefficiency found 2026-09-15 via a full-codebase UX/performance
audit: the Profile page used to fetch every ProfileVersion's full
content_json (a complete resume's worth of JSON per row -- 64 rows on
the real live profile) just to pick out the active version and any
pending ones in Python. Moved to profile_service.get_variant_display_data
both to fix that (only the lightweight history list scans every row now)
and to make it testable at all -- the router had zero coverage."""

import json

from app.models import ProfileVariant, ProfileVersion
from app.services import profile_service


def _variant_with_versions(db) -> ProfileVariant:
    variant = ProfileVariant(name="Data Engineering", is_default=True)
    db.add(variant)
    db.commit()
    db.refresh(variant)

    old = ProfileVersion(
        variant_id=variant.id, content_json=json.dumps({"name": "Old", "experience": []}),
        source="manual", is_active=False, change_summary="First save",
    )
    active = ProfileVersion(
        variant_id=variant.id, content_json=json.dumps({"name": "Active", "experience": [{"role": "DE"}]}),
        source="manual", is_active=True, change_summary="Latest save",
    )
    pending = ProfileVersion(
        variant_id=variant.id, content_json=json.dumps({"name": "Active", "experience": []}),
        source="linkedin_diff", is_active=False, change_summary="Pending LinkedIn sync",
    )
    other_pending_source = ProfileVersion(
        variant_id=variant.id, content_json=json.dumps({"name": "Active", "experience": []}),
        source="manual", is_active=False, change_summary="An old manual save, not pending",
    )
    db.add_all([old, active, pending, other_pending_source])
    db.commit()
    return variant


def test_returns_all_four_versions_in_the_lightweight_history_list(db):
    variant = _variant_with_versions(db)

    result = profile_service.get_variant_display_data(db, variant.id)

    assert len(result["versions"]) == 4
    assert {v.change_summary for v in result["versions"]} == {
        "First save", "Latest save", "Pending LinkedIn sync", "An old manual save, not pending",
    }


def test_active_version_is_the_one_real_active_row_with_full_content(db):
    variant = _variant_with_versions(db)

    result = profile_service.get_variant_display_data(db, variant.id)

    assert result["active_version"].change_summary == "Latest save"
    assert result["active_content"] == {"name": "Active", "experience": [{"role": "DE"}]}


def test_pending_versions_only_includes_inactive_linkedin_diff_rows(db):
    variant = _variant_with_versions(db)

    result = profile_service.get_variant_display_data(db, variant.id)

    assert len(result["pending_versions"]) == 1
    assert result["pending_versions"][0].change_summary == "Pending LinkedIn sync"


def test_pending_version_shrink_is_flagged_as_a_real_regression_warning(db):
    # Active profile has a real certification; the pending LinkedIn diff
    # drops it entirely -- detect_profile_regressions should flag that
    # shrink, same real check the Profile page needs to actually show it.
    variant = ProfileVariant(name="Test", is_default=True)
    db.add(variant)
    db.commit()
    db.refresh(variant)
    active = ProfileVersion(
        variant_id=variant.id,
        content_json=json.dumps({"certifications": ["AWS Certified"], "education": [{"degree": "BS"}]}),
        source="manual", is_active=True,
    )
    pending = ProfileVersion(
        variant_id=variant.id,
        content_json=json.dumps({"certifications": [], "education": [{"degree": "BS"}]}),
        source="linkedin_diff", is_active=False,
    )
    db.add_all([active, pending])
    db.commit()

    result = profile_service.get_variant_display_data(db, variant.id)

    assert len(result["pending_versions"]) == 1
    assert result["pending_versions"][0].regression_warnings != []


def test_no_versions_returns_empty_structure_not_an_error(db):
    variant = ProfileVariant(name="Empty", is_default=True)
    db.add(variant)
    db.commit()
    db.refresh(variant)

    result = profile_service.get_variant_display_data(db, variant.id)

    assert result["versions"] == []
    assert result["active_version"] is None
    assert result["pending_versions"] == []
    assert result["active_content"] == {}


def test_profile_template_renders_the_lightweight_versions_real_end_to_end(db):
    # Real integration check: renders profile.html against the actual
    # output of get_variant_display_data (the load_only'd version list
    # included) -- catches a deferred-column access bug the pure
    # service-level tests above can't, since Jinja2 attribute access
    # behaves differently than a plain Python assert.
    from jinja2 import Environment, FileSystemLoader

    variant = _variant_with_versions(db)
    display = profile_service.get_variant_display_data(db, variant.id)

    env = Environment(loader=FileSystemLoader("app/templates"))
    html = env.get_template("profile.html").render(
        variant_data=[{
            "variant": variant,
            "active_version": display["active_version"],
            "pending_versions": display["pending_versions"],
            "versions": display["versions"],
            "contact": {},
            "eeo": {},
            "application_preferences": {},
            "education": [],
            "certifications": [],
            "completeness_warnings": [],
            "behavioral_stories": [],
        }],
        message=None, error=None, csrf_token="test-token", static_version="0", is_authenticated=False,
    )

    assert "Version history (4)" in html
    assert "Pending LinkedIn sync" in html
    assert "First save" in html
