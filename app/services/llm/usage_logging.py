"""Shared LlmUsageLog writer, used by all three providers.

Real gap found 2026-09-15 via a full-codebase audit: only
anthropic_provider.py ever wrote to LlmUsageLog. The dashboard's cost
card would silently show $0/stale numbers the moment anyone switched
LLM_PROVIDER away from the Anthropic default (openai_compatible covers
OpenAI AND Gemini's free tier, which README.md explicitly tells forkers
they can use for free -- exactly the audience most likely to flip this
switch and least likely to notice a silently blind cost dashboard).
"""

def log_usage(provider: str, model: str, input_tokens: int, output_tokens: int, cost_usd: float | None) -> None:
    """Writes one real row per call, own independent DB session -- kept
    fully decoupled from whatever caller/transaction triggered this call.
    A failure here must never break the real LLM call it's just trying to
    record -- swallowed, not raised, same posture as this codebase's own
    log_activity calls that are allowed to fail without taking down the
    thing they're logging. Imports deliberately INSIDE the function (not
    module-level) -- a real test (test_anthropic_provider_streaming.py)
    monkeypatches app.database.SessionLocal to verify write failures are
    swallowed; a module-level import would bind a stale reference that
    patch could never reach."""
    try:
        from ...database import SessionLocal
        from ...models import LlmUsageLog

        db = SessionLocal()
        try:
            db.add(LlmUsageLog(
                provider=provider, model=model,
                input_tokens=input_tokens, output_tokens=output_tokens,
                estimated_cost_usd=cost_usd,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass
