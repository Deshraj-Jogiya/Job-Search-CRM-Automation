"""Real gap found 2026-09-15: dashboard.html was touched three times in
one session (research-agent ask box, activity-log retention setting,
progress cards) with only ad-hoc verification scripts each time, never
a permanent regression test. This consolidates real coverage for all of
it in one place, render-only style matching the rest of this suite."""

from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

env = Environment(loader=FileSystemLoader("app/templates"))


class _UsageTotals:
    calls = 0
    input_tokens = 0
    output_tokens = 0
    estimated_cost_usd = 0.0
    unpriced_calls = 0


class _Settings:
    automation_enabled = False
    min_score_for_auto_launch = 70
    tavily_monthly_call_budget = 1000
    hunter_monthly_call_budget = 25
    interview_prep_answer_target = 8
    automated_backups_enabled = False
    backup_retention_count = 14
    activity_log_retention_days = 90
    quiet_hours_enabled = False
    quiet_hours_start_hour = 22
    quiet_hours_end_hour = 7
    local_timezone = "UTC"
    notification_digest_interval_minutes = 60
    fast_poll_interval_minutes = 5
    full_ingest_interval_minutes = 60
    stale_posting_threshold_days = 30
    location_query = "United States"
    jobright_poll_interval_hours = 6
    confirmation_window_hours = 15.0
    fast_track_score_threshold = 90
    fast_track_freshness_minutes = 30
    fast_track_window_hours = 2.0
    rejected_retention_days = 7


def _base_context(**extra):
    context = {
        "settings": _Settings(),
        "total_applications": 0,
        "profile_variants": [],
        "backup_configured": True,
        "showcase_mode": False,
        "intake_unconfigured": False,
        "ready_to_start": False,
        "profile_completeness_warnings": [],
        "pending_trend_proposal_count": 0,
        "llm_usage": SimpleNamespace(all_time=_UsageTotals(), last_24h=_UsageTotals()),
        "recent_research_queries": [],
        "progress": {
            "total_postings": 0, "applied": 0, "interviewed": 0, "offers": 0, "not_selected": 0,
            "apply_rate": None, "interview_rate_of_applied": None, "offer_rate_of_applied": None,
        },
        "message": None, "error": None,
        "csrf_token": "test-token", "static_version": "0", "is_authenticated": True,
    }
    context.update(extra)
    return env.get_template("dashboard.html").render(**context)


def test_renders_cleanly_with_a_fresh_empty_instance():
    html = _base_context()
    assert "Ask About Your Search" in html
    assert "Real LLM Cost" in html


def test_activity_log_retention_field_shows_the_real_configured_value():
    html = _base_context(settings=_Settings())
    assert 'name="activity_log_retention_days"' in html
    assert 'value="90"' in html


def test_progress_cards_show_real_applied_interview_offer_counts():
    html = _base_context(progress={
        "total_postings": 40, "applied": 12, "interviewed": 3, "offers": 1, "not_selected": 2,
        "apply_rate": 30.0, "interview_rate_of_applied": 25.0, "offer_rate_of_applied": 8.3,
    })
    assert "Applied</span>" in html
    assert "<span class=\"card-value\">12</span>" in html
    assert "25.0%" in html


def test_metrics_link_hidden_when_nothing_applied_yet():
    html = _base_context(progress={
        "total_postings": 5, "applied": 0, "interviewed": 0, "offers": 0, "not_selected": 0,
        "apply_rate": None, "interview_rate_of_applied": None, "offer_rate_of_applied": None,
    })
    assert "Metrics page" not in html


def test_metrics_link_shown_once_something_has_been_applied():
    html = _base_context(progress={
        "total_postings": 5, "applied": 2, "interviewed": 0, "offers": 0, "not_selected": 0,
        "apply_rate": 40.0, "interview_rate_of_applied": None, "offer_rate_of_applied": None,
    })
    assert "Metrics page" in html


def test_research_agent_answered_query_renders():
    html = _base_context(recent_research_queries=[
        SimpleNamespace(question="How many applications are applied?", answer="12 applications.", error=None, steps=[]),
    ])
    assert "How many applications are applied?" in html
    assert "12 applications." in html


def test_research_agent_error_query_renders():
    html = _base_context(recent_research_queries=[
        SimpleNamespace(question="Status of Acme?", answer=None, error="LLM timeout", steps=[]),
    ])
    assert "Something went wrong answering this: LLM timeout" in html


def test_research_agent_pending_query_renders():
    html = _base_context(recent_research_queries=[
        SimpleNamespace(question="Still running?", answer=None, error=None, steps=[]),
    ])
    assert "Still thinking" in html
