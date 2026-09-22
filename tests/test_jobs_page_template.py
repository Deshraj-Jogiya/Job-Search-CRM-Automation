"""Real end-to-end check for the Jobs page restructuring (2026-09-15):
grouped by pipeline stage instead of one flat list, settings + target
companies collapsed behind <details>, target companies capped with a
real total shown. Renders the actual template against real DB-backed
fixtures and the router's own grouping function, catching anything a
pure-logic test of _group_applications_by_stage alone couldn't."""

import json

from jinja2 import Environment, FileSystemLoader

from app.routers.jobs import KANBAN_COLUMNS, _group_applications_by_stage, _group_applications_by_status
from tests.conftest import make_application, make_company, make_posting

env = Environment(loader=FileSystemLoader("app/templates"))


def _base_context(**extra):
    context = {
        "sources": [], "keywords": [], "seniority_exclusions": [], "location_exclusions": [],
        "target_companies": [], "target_companies_total": 0,
        "automation_enabled": True,
        # Board tab's context -- see test_kanban_page_template.py for
        # the real checks on this tab; present here only so the list-
        # view tests in this file render the template at all.
        "columns": _group_applications_by_status([]), "column_order": KANBAN_COLUMNS,
        "valid_source_statuses_json": json.dumps({}),
        "message": None, "error": None,
        "csrf_token": "test-token", "static_version": "0", "is_authenticated": False,
    }
    context.update(extra)
    return context


def test_applications_render_in_their_real_stage_group(db):
    company = make_company(db)
    # One posting per application -- job_applications.posting_id is unique.
    needs_attention_app = make_application(db, make_posting(db, company), status="Pending Confirmation")
    in_progress_app = make_application(db, make_posting(db, company), status="Tailored")
    applied_app = make_application(db, make_posting(db, company), status="Applied")
    closed_app = make_application(db, make_posting(db, company), status="Rejected")
    applications = [needs_attention_app, in_progress_app, applied_app, closed_app]

    html = env.get_template("jobs.html").render(**_base_context(
        application_groups=_group_applications_by_stage(applications),
        applications_total=len(applications),
    ))

    assert "Needs Attention -- 1" in html
    assert "In Progress -- 1" in html
    assert "Applied &amp; Waiting -- 1" in html
    assert "Closed -- 1" in html
    assert "Job Postings (4)" in html


def test_closed_section_is_collapsed_by_default_others_are_open(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting, status="Rejected")

    html = env.get_template("jobs.html").render(**_base_context(
        application_groups=_group_applications_by_stage([application]),
        applications_total=1,
    ))

    # Crude but real check: the <details> immediately preceding "Closed"
    # must NOT carry the open attribute; the ones before it must.
    closed_block_start = html.index("Closed -- 1")
    details_tag_start = html.rindex("<details", 0, closed_block_start)
    details_tag = html[details_tag_start:closed_block_start]
    assert "open" not in details_tag

    needs_attention_start = html.index("Needs Attention")
    na_details_start = html.rindex("<details", 0, needs_attention_start)
    na_details_tag = html[na_details_start:needs_attention_start]
    assert "open" in na_details_tag


def test_empty_groups_show_their_own_empty_state_not_a_blank_section(db):
    html = env.get_template("jobs.html").render(**_base_context(
        application_groups=_group_applications_by_stage([]),
        applications_total=0,
    ))

    assert "Nothing waiting on you right now." in html
    assert "Nothing currently being scored or tailored." in html
    assert "Nothing submitted yet." in html
    assert "Nothing closed out yet." in html
    assert "No postings found yet" in html


def test_target_companies_preview_shows_real_total_and_remainder_note(db):
    html = env.get_template("jobs.html").render(**_base_context(
        application_groups=_group_applications_by_stage([]),
        applications_total=0,
        target_companies=[{"name": "Acme", "greenhouse_slug": "acme", "lever_slug": None, "ashby_slug": None,
                            "recruitee_slug": None, "personio_slug": None, "workable_slug": None, "smartrecruiters_slug": None}],
        target_companies_total=354,
    ))

    assert "Target Companies (Direct ATS Boards) -- 354" in html
    assert "Showing 1 of 354" in html


def test_target_companies_no_remainder_note_when_all_shown(db):
    html = env.get_template("jobs.html").render(**_base_context(
        application_groups=_group_applications_by_stage([]),
        applications_total=0,
        target_companies=[{"name": "Acme", "greenhouse_slug": "acme", "lever_slug": None, "ashby_slug": None,
                            "recruitee_slug": None, "personio_slug": None, "workable_slug": None, "smartrecruiters_slug": None}],
        target_companies_total=1,
    ))

    assert "Showing" not in html


def test_search_settings_and_target_companies_are_collapsed_by_default(db):
    html = env.get_template("jobs.html").render(**_base_context(
        application_groups=_group_applications_by_stage([]),
        applications_total=0,
    ))

    settings_start = html.index("Search Settings")
    settings_details_start = html.rindex("<details", 0, settings_start)
    assert "open" not in html[settings_details_start:settings_start]

    companies_start = html.index("Target Companies")
    companies_details_start = html.rindex("<details", 0, companies_start)
    assert "open" not in html[companies_details_start:companies_start]
