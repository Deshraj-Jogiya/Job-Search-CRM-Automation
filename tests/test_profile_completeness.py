"""Profile data-integrity safeguards. Built after a real incident: a
real profile went 5 days and 8 separate manual saves with a completely
missing second degree and zero certifications, never flagged, because
a raw JSON textarea gives no signal that a section shrank or was never
there. Covers the two mechanical (non-LLM) checks that exist to catch
this -- detect_profile_regressions (shrink vs. the current active
version) and profile_completeness_warnings (empty sections, regardless
of history) -- plus the structured education/certification add/remove
functions meant to replace raw-JSON editing for exactly these fields."""

import json

from app.models import ProfileVariant, ProfileVersion
from app.services import profile_service
from app.services.profile_service import (
    ProfileServiceError,
    add_certification,
    add_education_entry,
    detect_profile_regressions,
    profile_completeness_warnings,
    remove_certification,
    remove_education_entry,
)


def make_variant_with_content(db, content: dict) -> ProfileVariant:
    variant = ProfileVariant(name="Default", is_default=True)
    db.add(variant)
    db.commit()
    db.refresh(variant)
    version = ProfileVersion(
        variant_id=variant.id,
        content_json=json.dumps(content),
        source="manual",
        is_active=True,
    )
    db.add(version)
    db.commit()
    return variant


# -- detect_profile_regressions ----------------------------------------


def test_no_regression_when_nothing_shrank():
    old = {"education": [{"degree": "MS"}], "certifications": ["A"]}
    new = {"education": [{"degree": "MS"}, {"degree": "BS"}], "certifications": ["A", "B"]}
    assert detect_profile_regressions(old, new) == []


def test_detects_certifications_shrinking_to_zero():
    old = {"certifications": ["Tableau", "IBM Data Analyst"]}
    new = {"certifications": []}
    warnings = detect_profile_regressions(old, new)
    assert any("certifications: 2 -> 0" in w for w in warnings)


def test_detects_education_shrinking():
    old = {"education": [{"degree": "MS"}, {"degree": "BS"}]}
    new = {"education": [{"degree": "MS"}]}
    warnings = detect_profile_regressions(old, new)
    assert any("education entries: 2 -> 1" in w for w in warnings)


def test_detects_skills_dict_shrinking():
    old = {"skills": {"languages": ["Python", "SQL"], "cloud": ["AWS"]}}
    new = {"skills": {"languages": ["Python"], "cloud": ["AWS"]}}
    warnings = detect_profile_regressions(old, new)
    assert any("skills: 3 -> 2" in w for w in warnings)


def test_never_had_it_to_begin_with_is_not_a_regression():
    """The real incident: certifications was empty from the very first
    version, so there was never a 'shrink' -- detect_profile_regressions
    alone can't catch this, which is exactly why
    profile_completeness_warnings exists separately."""
    old = {"certifications": []}
    new = {"certifications": []}
    assert detect_profile_regressions(old, new) == []


# -- profile_completeness_warnings --------------------------------------


def test_flags_empty_certifications():
    warnings = profile_completeness_warnings({"certifications": [], "education": [{"degree": "MS"}], "experience": [{}]})
    assert any("certifications" in w.lower() for w in warnings)


def test_flags_empty_education():
    warnings = profile_completeness_warnings({"certifications": ["A"], "education": [], "experience": [{}]})
    assert any("education" in w.lower() for w in warnings)


def test_no_warnings_when_everything_present():
    content = {
        "certifications": ["A"],
        "education": [{"degree": "MS"}],
        "experience": [{"role": "Engineer", "bullets": ["Did a thing."]}],
        "skills": {"languages": ["Python"]},
    }
    assert profile_completeness_warnings(content) == []


def test_flags_empty_skills():
    content = {
        "certifications": ["A"],
        "education": [{"degree": "MS"}],
        "experience": [{"role": "Engineer", "bullets": ["Did a thing."]}],
        "skills": {},
    }
    warnings = profile_completeness_warnings(content)
    assert any("skills" in w.lower() for w in warnings)


def test_flags_experience_entry_with_no_bullets_even_though_role_is_present():
    """The real failure mode this catches: an entry survives a paste
    with its role/company intact but its bullets wiped -- invisible to
    scoring (which only ever reads bullets), and invisible to a plain
    experience-count check (the entry is still there)."""
    content = {
        "certifications": ["A"],
        "education": [{"degree": "MS"}],
        "skills": {"languages": ["Python"]},
        "experience": [{"role": "Data Engineer", "company": "Acme", "bullets": []}],
    }
    warnings = profile_completeness_warnings(content)
    assert any("Data Engineer" in w and "no bullets" in w for w in warnings)


def test_flags_project_entry_with_no_bullets():
    content = {
        "certifications": ["A"],
        "education": [{"degree": "MS"}],
        "skills": {"languages": ["Python"]},
        "experience": [{"role": "Engineer", "bullets": ["Did a thing."]}],
        "projects": [{"name": "Side Project", "bullets": []}],
    }
    warnings = profile_completeness_warnings(content)
    assert any("Side Project" in w and "no bullets" in w for w in warnings)


def test_does_not_flag_experience_entry_that_has_bullets():
    content = {
        "certifications": ["A"],
        "education": [{"degree": "MS"}],
        "skills": {"languages": ["Python"]},
        "experience": [{"role": "Engineer", "bullets": ["Shipped a thing.", "Fixed a thing."]}],
    }
    assert profile_completeness_warnings(content) == []


def test_detects_experience_bullets_thinning_even_when_entry_count_is_unchanged():
    """Same real failure mode as above, but for detect_profile_regressions:
    a paste that keeps every role/company row but drops bullets from one
    of them looks identical to the old top-level count check (same
    number of experience entries), so it needs its own comparison."""
    old = {"experience": [{"role": "Engineer", "bullets": ["A", "B", "C"]}]}
    new = {"experience": [{"role": "Engineer", "bullets": ["A"]}]}
    warnings = detect_profile_regressions(old, new)
    assert any("experience bullets: 3 -> 1" in w for w in warnings)


def test_detects_project_bullets_thinning():
    old = {"projects": [{"name": "X", "bullets": ["A", "B"]}]}
    new = {"projects": [{"name": "X", "bullets": []}]}
    warnings = detect_profile_regressions(old, new)
    assert any("project bullets: 2 -> 0" in w for w in warnings)


# -- structured education/certification management ----------------------


def test_add_education_entry_appends_without_touching_other_fields(db):
    variant = make_variant_with_content(db, {"education": [{"degree": "MS", "school": "ASU", "date": "GPA 4.0"}], "experience": [{"role": "x"}]})

    add_education_entry(db, variant.id, "B.Tech", "Institute of Advanced Research", "GPA 7.8/10")

    active = db.query(ProfileVersion).filter(ProfileVersion.variant_id == variant.id, ProfileVersion.is_active == True).first()  # noqa: E712
    content = json.loads(active.content_json)
    assert len(content["education"]) == 2
    assert content["education"][1]["degree"] == "B.Tech"
    assert content["experience"] == [{"role": "x"}]


def test_add_education_requires_degree_and_school(db):
    variant = make_variant_with_content(db, {"education": []})
    try:
        add_education_entry(db, variant.id, "", "ASU", "")
        assert False, "should have raised"
    except ProfileServiceError:
        pass


def test_remove_education_entry_by_index(db):
    variant = make_variant_with_content(db, {"education": [{"degree": "MS"}, {"degree": "BS"}]})

    remove_education_entry(db, variant.id, 0)

    active = db.query(ProfileVersion).filter(ProfileVersion.variant_id == variant.id, ProfileVersion.is_active == True).first()  # noqa: E712
    content = json.loads(active.content_json)
    assert content["education"] == [{"degree": "BS"}]


def test_remove_education_entry_stale_index_raises_not_crashes(db):
    variant = make_variant_with_content(db, {"education": [{"degree": "MS"}]})
    try:
        remove_education_entry(db, variant.id, 5)
        assert False, "should have raised"
    except ProfileServiceError:
        pass


def test_add_and_remove_certification_round_trip(db):
    variant = make_variant_with_content(db, {"certifications": []})

    add_certification(db, variant.id, "AWS Certified Solutions Architect")
    active = db.query(ProfileVersion).filter(ProfileVersion.variant_id == variant.id, ProfileVersion.is_active == True).first()  # noqa: E712
    assert json.loads(active.content_json)["certifications"] == ["AWS Certified Solutions Architect"]

    remove_certification(db, variant.id, 0)
    active = db.query(ProfileVersion).filter(ProfileVersion.variant_id == variant.id, ProfileVersion.is_active == True).first()  # noqa: E712
    assert json.loads(active.content_json)["certifications"] == []


def test_structured_edits_do_not_trigger_regression_warning_noise(db):
    """Removing one certification via the dedicated button is a
    deliberate, narrow action -- it must not itself get flagged as a
    'regression' the way a wholesale raw-JSON paste shrinking would."""
    variant = make_variant_with_content(db, {"certifications": ["A", "B"]})
    version = remove_certification(db, variant.id, 0)
    assert version.source == "manual"  # saved fine, no warning plumbing attached to this path


# -- create_manual_version regression surfacing --------------------------


def test_manual_save_still_saves_but_returns_warnings_on_shrink(db):
    variant = make_variant_with_content(db, {"certifications": ["A", "B", "C"]})

    version, warnings = profile_service.create_manual_version(
        db, variant.id, json.dumps({"certifications": []})
    )

    assert warnings and "certifications: 3 -> 0" in warnings[0]
    active = db.query(ProfileVersion).filter(ProfileVersion.variant_id == variant.id, ProfileVersion.is_active == True).first()  # noqa: E712
    assert active.id == version.id
    assert json.loads(active.content_json)["certifications"] == []
