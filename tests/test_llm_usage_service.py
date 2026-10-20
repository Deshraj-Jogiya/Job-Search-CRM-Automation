"""Real incident, 2026-09-18: progress_ingested_applications had no
dollar ceiling of its own and burned $164.91 in 24h against an $18-30
estimate. daily_llm_spend_cap_usd / spend_cap_exceeded close that real
gap -- these tests exercise the check itself, independent of any call
site that uses it (see test_progress_ingested_applications.py for those)."""

from app.models import GlobalSettings, LlmUsageLog
from app.services import llm_usage_service


def _settings(db, **overrides):
    settings = db.query(GlobalSettings).first()
    for key, value in overrides.items():
        setattr(settings, key, value)
    db.commit()
    return settings


def _log_spend(db, cost_usd):
    db.add(LlmUsageLog(provider="anthropic", model="claude-test", input_tokens=100, output_tokens=100, estimated_cost_usd=cost_usd))
    db.commit()


def test_no_cap_configured_never_exceeds(db, settings):
    _settings(db, daily_llm_spend_cap_usd=None)
    _log_spend(db, 1000.0)  # absurdly high real spend
    assert llm_usage_service.spend_cap_exceeded(db) is False


def test_spend_below_cap_does_not_exceed(db, settings):
    _settings(db, daily_llm_spend_cap_usd=10.0)
    _log_spend(db, 3.0)
    assert llm_usage_service.spend_cap_exceeded(db) is False


def test_spend_at_or_above_cap_exceeds(db, settings):
    _settings(db, daily_llm_spend_cap_usd=5.0)
    _log_spend(db, 5.0)
    assert llm_usage_service.spend_cap_exceeded(db) is True


def test_unpriced_calls_are_excluded_never_overcounted(db, settings):
    """estimated_cost_usd is None whenever a provider had no verified
    price at call time (e.g. every openai_compatible_provider.py call) --
    same convention get_usage_summary already uses. This can only ever
    undercount real spend, never silently inflate it into a false cap
    trip."""
    _settings(db, daily_llm_spend_cap_usd=1.0)
    db.add(LlmUsageLog(provider="openai_compatible", model="local-model", input_tokens=1000, output_tokens=1000, estimated_cost_usd=None))
    db.commit()
    assert llm_usage_service.spend_cap_exceeded(db) is False


def test_only_last_24h_counts_not_all_time(db, settings):
    from datetime import timedelta
    from app.database import utcnow

    _settings(db, daily_llm_spend_cap_usd=1.0)
    old = LlmUsageLog(
        provider="anthropic", model="claude-test", input_tokens=100, output_tokens=100,
        estimated_cost_usd=100.0, created_at=utcnow() - timedelta(hours=48),
    )
    db.add(old)
    db.commit()
    assert llm_usage_service.spend_cap_exceeded(db) is False  # old spend outside the rolling window


def test_last_24h_spend_usd_sums_real_logged_cost(db, settings):
    _settings(db)
    _log_spend(db, 1.5)
    _log_spend(db, 2.5)
    assert llm_usage_service.last_24h_spend_usd(db) == 4.0
