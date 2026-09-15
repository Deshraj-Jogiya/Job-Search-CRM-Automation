import os
from anthropic import Anthropic
from .base import LLMProvider

# $/million tokens, real published rates -- needs a manual update whenever
# Anthropic changes pricing (there's no pricing API to read this from live).
# Checked 2026-09-14. Unknown models fall back to None (cost left unrecorded
# rather than guessed) -- see _log_usage.
_PRICE_PER_MILLION_TOKENS = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
}


def _log_usage(model: str, input_tokens: int, output_tokens: int) -> None:
    """Writes one real row per call, own independent DB session -- kept
    fully decoupled from whatever caller/transaction triggered this call
    (this module has no db parameter anywhere and touching every one of
    its dozens of call sites to thread one through would be a large,
    invasive change for what this needs). A failure here must never
    break the real LLM call it's just trying to record -- swallowed,
    not raised, same posture as this codebase's own log_activity calls
    that are allowed to fail without taking down the thing they're
    logging."""
    try:
        from ...database import SessionLocal
        from ...models import LlmUsageLog

        prices = _PRICE_PER_MILLION_TOKENS.get(model)
        cost = None
        if prices:
            cost = (input_tokens / 1_000_000 * prices["input"]) + (output_tokens / 1_000_000 * prices["output"])

        db = SessionLocal()
        try:
            db.add(LlmUsageLog(
                provider="anthropic", model=model,
                input_tokens=input_tokens, output_tokens=output_tokens,
                estimated_cost_usd=cost,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


class AnthropicProvider(LLMProvider):
    """Your instance's default provider. Requires ANTHROPIC_API_KEY."""

    def __init__(self):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")
        self.client = Anthropic(api_key=api_key)
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    def _call(self, system: str, prompt: str, temperature: float, max_tokens: int) -> str:
        # Streamed unconditionally, not just above some max_tokens
        # threshold -- the Anthropic SDK raises ValueError ("Streaming is
        # required for operations that may take longer than 10 minutes")
        # for a non-streaming call once its own estimate crosses that mark,
        # and that estimate depends on max_tokens AND the model's current
        # throughput, not a fixed number this code could hardcode a
        # threshold against. Real failure hit in production: a caller
        # requesting deep, uncapped interview-prep coverage (high
        # answer_target) pushed max_tokens to 24000 and hit this wall.
        # Streaming works identically for small requests too, so there's
        # no reason to keep the non-streaming path around at all.
        with self.client.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            response = stream.get_final_message()
        if response.usage:
            _log_usage(self.model, response.usage.input_tokens, response.usage.output_tokens)
        return "".join(block.text for block in response.content if block.type == "text").strip()

    def complete_json(self, system: str, prompt: str, temperature: float = 0.3, max_tokens: int = None) -> str:
        return self._call_with_retry(self._call, system, prompt, temperature, max_tokens=max_tokens or 2000)

    def complete_text(self, system: str, prompt: str, temperature: float = 0.4, max_tokens: int = None) -> str:
        return self._call_with_retry(self._call, system, prompt, temperature, max_tokens=max_tokens or 1200)
