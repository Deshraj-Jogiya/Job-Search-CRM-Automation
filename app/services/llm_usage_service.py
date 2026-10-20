"""Reads real LlmUsageLog rows (see app/models.py's own docstring on
that table, and app/services/llm/usage_logging.py's log_usage, called by
all three providers as of 2026-09-15 -- this module is pure read/
aggregation, nothing writes here). Added after the user asked what a
real submission actually cost and the honest answer was that nothing
tracked it before this."""

from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import LlmUsageLog, get_or_create_settings


def get_usage_summary(db: Session) -> dict:
    """All-time and last-24h totals. estimated_cost_usd is None on a
    row whenever the provider that wrote it had no real, verified price
    for that model at call time (every openai_compatible_provider.py
    call, currently -- no verified pricing table for the open set of
    OpenAI-compatible endpoints/models it can point at) -- summed
    separately from priced calls so an unpriced call silently
    understating the total stays visible (unpriced_calls > 0) rather
    than hidden inside a smaller-than-real number. Ollama's estimated_
    cost_usd is a real 0.0, not None -- local inference genuinely has no
    per-token charge, so it correctly counts as "priced" at zero.
    unpriced call silently understating the total stays visible
    (unpriced_calls > 0) rather than hidden inside a smaller-than-real
    number."""
    def _totals(query):
        row = query.with_entities(
            func.count(LlmUsageLog.id),
            func.coalesce(func.sum(LlmUsageLog.input_tokens), 0),
            func.coalesce(func.sum(LlmUsageLog.output_tokens), 0),
            func.coalesce(func.sum(LlmUsageLog.estimated_cost_usd), 0.0),
            func.count(LlmUsageLog.id).filter(LlmUsageLog.estimated_cost_usd.is_(None)),
        ).first()
        calls, input_tokens, output_tokens, cost_usd, unpriced_calls = row
        return {
            "calls": calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": round(cost_usd, 4),
            "unpriced_calls": unpriced_calls,
        }

    since_24h = utcnow() - timedelta(hours=24)
    return {
        "all_time": _totals(db.query(LlmUsageLog)),
        "last_24h": _totals(db.query(LlmUsageLog).filter(LlmUsageLog.created_at >= since_24h)),
    }


def last_24h_spend_usd(db: Session) -> float:
    """Real spend, not an estimate of an estimate -- same rolling-24h
    window get_usage_summary already shows on the dashboard, so this
    check and what a human sees always agree. Unpriced calls (None
    estimated_cost_usd) are excluded via coalesce, same as
    get_usage_summary -- this can only ever undercount, never overcount,
    so it's a real floor on spend, not an inflated one."""
    total = (
        db.query(func.coalesce(func.sum(LlmUsageLog.estimated_cost_usd), 0.0))
        .filter(LlmUsageLog.created_at >= utcnow() - timedelta(hours=24))
        .scalar()
    )
    return float(total or 0.0)


def spend_cap_exceeded(db: Session) -> bool:
    """Real incident, 2026-09-18: nothing checked a running spend total
    against a limit before the next paid call -- $164.91 burned in 24h
    against an $18-30 estimate. Checked at every real, metered-API call
    site right before it would spend more money (confirmation_service's
    scoring/tailoring loop, extension_service's real-time add-job path),
    never after. GlobalSettings.daily_llm_spend_cap_usd is nullable --
    None means no cap configured (an explicit opt-out, not a bug), so
    this only ever blocks when a real, human-set limit is in place."""
    settings = get_or_create_settings(db)
    if settings.daily_llm_spend_cap_usd is None:
        return False
    return last_24h_spend_usd(db) >= settings.daily_llm_spend_cap_usd
