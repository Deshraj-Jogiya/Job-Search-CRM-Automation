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
from ..models import LlmUsageLog


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
