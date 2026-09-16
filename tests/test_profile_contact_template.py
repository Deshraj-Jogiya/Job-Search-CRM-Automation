"""Real end-to-end check that the new Contact Details section (see
routers/profile.py's update_contact) actually renders with real stored
values pre-filled, same pattern as test_jobs_page_template.py."""

from jinja2 import Environment, FileSystemLoader

env = Environment(loader=FileSystemLoader("app/templates"))


def _base_context(**extra):
    context = {
        "variant_data": [],
        "message": None, "error": None,
        "csrf_token": "test-token", "static_version": "0", "is_authenticated": False,
    }
    context.update(extra)
    return context


def _entry(variant, contact=None):
    return {
        "variant": variant,
        "active_version": None,
        "pending_versions": [],
        "versions": [],
        "contact": contact or {},
        "eeo": {},
        "application_preferences": {},
        "education": [],
        "certifications": [],
        "completeness_warnings": [],
        "behavioral_stories": [],
    }


def test_contact_section_renders_with_no_data_yet(db):
    from conftest import make_variant

    variant = make_variant(db)
    html = env.get_template("profile.html").render(**_base_context(variant_data=[_entry(variant)]))

    assert "Contact details" in html
    assert f'/profile/variants/{variant.id}/contact' in html


def test_real_stored_values_are_pre_filled(db):
    from conftest import make_variant

    variant = make_variant(db)
    contact = {
        "email": "deshraj@example.com", "phone": "(480) 876-2863",
        "linkedin": "https://www.linkedin.com/in/deshrajjogiya",
        "github": "https://github.com/Deshraj-Jogiya",
        "portfolio": "https://deshraj-jogiya.github.io",
        "zip": "85281",
    }
    html = env.get_template("profile.html").render(**_base_context(variant_data=[_entry(variant, contact)]))

    assert 'value="deshraj@example.com"' in html
    assert 'value="(480) 876-2863"' in html
    assert 'value="https://www.linkedin.com/in/deshrajjogiya"' in html
    assert 'value="https://github.com/Deshraj-Jogiya"' in html
    assert 'value="https://deshraj-jogiya.github.io"' in html
    assert 'value="85281"' in html


def test_missing_fields_render_as_empty_not_a_template_crash(db):
    from conftest import make_variant

    variant = make_variant(db)
    # Only some fields set -- the rest must render as empty inputs, not
    # raise (Jinja's undefined attribute access on a plain dict already
    # returns None via .get-style behavior here since `contact` is a
    # real dict, but this guards against a regression either way).
    html = env.get_template("profile.html").render(
        **_base_context(variant_data=[_entry(variant, {"email": "d@example.com"})])
    )

    assert 'value="d@example.com"' in html
    assert 'name="zip_code" value=""' in html
