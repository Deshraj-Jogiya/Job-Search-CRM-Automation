"""Real gap found live 2026-09-16: linkedin/github/portfolio/zip were
already real fields the browser extension and lever_autofill.py both
read (contact.linkedin/github/portfolio) -- but the only way to ever
SET them was pasting raw profile JSON. A candidate using only the
structured Profile page had no way to add these at all. Fixed with a
real form, same convention as the existing EEO/preferences one:
submits every contact field together (update_structured_fields does a
shallow top-level merge, not a deep merge, so a partial submission
would silently wipe out whatever wasn't re-typed)."""

import json

from conftest import make_variant

from app.routers import profile as profile_router
from app.services import profile_service


def _active_content(db, variant_id):
    version = profile_service.get_active_version(db, variant_id)
    return json.loads(version.content_json)


def test_saves_every_real_contact_field(db, settings):
    variant = make_variant(db, content={"name": "Test"})

    profile_router.update_contact(
        variant.id,
        email="d@example.com", phone="555-0100", location="Tempe, Arizona",
        city="Tempe", state="Arizona", zip_code="85281",
        linkedin="https://linkedin.com/in/d", github="https://github.com/d", portfolio="https://d.dev",
        db=db,
    )

    contact = _active_content(db, variant.id).get("contact")
    assert contact == {
        "email": "d@example.com", "phone": "555-0100", "location": "Tempe, Arizona",
        "city": "Tempe", "state": "Arizona", "zip": "85281",
        "linkedin": "https://linkedin.com/in/d", "github": "https://github.com/d", "portfolio": "https://d.dev",
    }


def test_resubmitting_all_fields_preserves_ones_that_did_not_change(db, settings):
    # The real risk this whole form design guards against: a save here
    # must never silently drop an existing value just because a later
    # edit only meant to change one field.
    variant = make_variant(db, content={"name": "Test"})
    profile_router.update_contact(
        variant.id, email="d@example.com", phone="", location="", city="", state="", zip_code="",
        linkedin="https://linkedin.com/in/d", github="", portfolio="", db=db,
    )

    # A later save that re-submits the SAME email/linkedin plus a new zip.
    profile_router.update_contact(
        variant.id, email="d@example.com", phone="", location="", city="", state="", zip_code="85281",
        linkedin="https://linkedin.com/in/d", github="", portfolio="", db=db,
    )

    contact = _active_content(db, variant.id).get("contact")
    assert contact == {"email": "d@example.com", "zip": "85281", "linkedin": "https://linkedin.com/in/d"}


def test_does_not_touch_other_profile_sections(db, settings):
    # Every Form(...) param needs an explicit value when calling the
    # route function directly like this -- its own default is a FastAPI
    # marker object, not a real "", only ever resolved by the actual
    # request-handling dependency injection this bypasses.
    variant = make_variant(db, content={"name": "Test", "skills": ["Python"], "eeo": {"gender": "Male"}})

    profile_router.update_contact(
        variant.id, email="d@example.com", phone="", location="", city="", state="", zip_code="",
        linkedin="", github="", portfolio="", db=db,
    )

    content = _active_content(db, variant.id)
    assert content["skills"] == ["Python"]
    assert content["eeo"] == {"gender": "Male"}
    assert content["contact"] == {"email": "d@example.com"}


def test_blank_fields_are_omitted_not_stored_as_empty_strings(db, settings):
    variant = make_variant(db, content={"name": "Test"})

    profile_router.update_contact(
        variant.id, email="d@example.com", phone="   ", location="", city="", state="", zip_code="",
        linkedin="", github="", portfolio="", db=db,
    )

    contact = _active_content(db, variant.id).get("contact")
    assert contact == {"email": "d@example.com"}
    assert "phone" not in contact
