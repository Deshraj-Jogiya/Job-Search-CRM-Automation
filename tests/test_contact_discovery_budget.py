"""Tavily/Hunter.io monthly call-budget tracking (Phase 19). Before this,
a real quota exhaustion looked identical to "genuinely found nothing" in
the logs -- both silently returned an empty list. Covers only the pure
budget bookkeeping (_consume_budget/_reset_monthly_counter_if_needed);
the real HTTP calls themselves aren't exercised here, same as every
other external-API integration in this test suite."""

from datetime import timedelta

import pytest

from app.database import utcnow
from app.services.contact_discovery_service import BudgetExhaustedError, _consume_budget


def test_first_call_this_month_succeeds_and_increments(db, settings):
    _consume_budget(db, "tavily")
    db.refresh(settings)
    assert settings.tavily_calls_used_this_month == 1
    assert settings.tavily_month_reset_at is not None


def test_budget_exhausted_raises_without_incrementing(db, settings):
    settings.tavily_monthly_call_budget = 2
    settings.tavily_calls_used_this_month = 2
    settings.tavily_month_reset_at = utcnow() + timedelta(days=10)  # not due to reset yet
    db.commit()

    with pytest.raises(BudgetExhaustedError):
        _consume_budget(db, "tavily")

    db.refresh(settings)
    assert settings.tavily_calls_used_this_month == 2  # unchanged -- refused call doesn't count


def test_counter_resets_after_the_period_elapses(db, settings):
    settings.hunter_monthly_call_budget = 5
    settings.hunter_calls_used_this_month = 5
    settings.hunter_month_reset_at = utcnow() - timedelta(days=1)  # period already elapsed
    db.commit()

    _consume_budget(db, "hunter")  # should reset first, then succeed

    db.refresh(settings)
    assert settings.hunter_calls_used_this_month == 1


def test_two_providers_track_independently(db, settings):
    settings.tavily_calls_used_this_month = 0
    settings.hunter_calls_used_this_month = 0
    db.commit()

    _consume_budget(db, "tavily")

    db.refresh(settings)
    assert settings.tavily_calls_used_this_month == 1
    assert settings.hunter_calls_used_this_month == 0
