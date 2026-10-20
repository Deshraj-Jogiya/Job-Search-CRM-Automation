import os
from anthropic import Anthropic
from .base import LLMProvider
from .usage_logging import log_usage

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


def _log_usage(
    model: str, input_tokens: int, output_tokens: int,
    cache_creation_tokens: int = 0, cache_read_tokens: int = 0,
) -> None:
    """Real incident, 2026-09-28: once prompt caching is in use, the
    Anthropic API reports cache_creation_input_tokens/cache_read_input_
    tokens SEPARATELY from input_tokens (the response's own input_tokens
    only covers the non-cached portion) -- pricing every input token at
    the flat rate would silently overstate real cost (a cache read is
    ~10% of normal price) or understate it (a cache write is ~1.25x, a
    real premium for the 5-minute-TTL write). input_tokens logged here
    is the REAL total (all three categories summed) for accurate volume
    tracking; cost is computed per-category at each one's real rate so
    the dashboard's own "Real LLM Cost" stays honest once caching is
    live, not just before it."""
    prices = _PRICE_PER_MILLION_TOKENS.get(model)
    total_input_tokens = input_tokens + cache_creation_tokens + cache_read_tokens
    cost = None
    if prices:
        cost = (
            (input_tokens / 1_000_000 * prices["input"])
            + (cache_creation_tokens / 1_000_000 * prices["input"] * 1.25)
            + (cache_read_tokens / 1_000_000 * prices["input"] * 0.1)
            + (output_tokens / 1_000_000 * prices["output"])
        )
    log_usage("anthropic", model, total_input_tokens, output_tokens, cost)


class AnthropicProvider(LLMProvider):
    """Your instance's default provider. Requires ANTHROPIC_API_KEY."""

    def __init__(self):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")
        self.client = Anthropic(api_key=api_key)
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    def _call(
        self, system: str, prompt: str, temperature: float, max_tokens: int, stop: list[str] | None = None,
        cacheable_prefix: str | None = None,
    ) -> str:
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
        kwargs = {}
        if stop:
            kwargs["stop_sequences"] = stop
        # Real cost fix, 2026-09-28: a multi-pass tailoring session resends
        # the full candidate profile (often tens of KB) as fresh input on
        # every one of its 6-16 calls. cache_control on its own content
        # block tells Anthropic to cache it -- a cache read costs ~10% of
        # normal input price (Anthropic's own published pricing), so
        # every call after the first in a session pays a fraction for
        # this block instead of full price every single time. Only used
        # when a caller actually passes cacheable_prefix; every other
        # call keeps the exact same flat-string message shape as before,
        # byte-identical behavior, zero regression risk for callers that
        # don't opt in.
        if cacheable_prefix:
            content = [
                {"type": "text", "text": cacheable_prefix, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": prompt},
            ]
        else:
            content = prompt
        with self.client.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": content}],
            **kwargs,
        ) as stream:
            response = stream.get_final_message()
        if response.usage:
            # getattr with a 0 default -- these two fields only appear on
            # a response when cache_control was actually used; a normal,
            # uncached call's usage object may not have them at all.
            _log_usage(
                self.model, response.usage.input_tokens, response.usage.output_tokens,
                cache_creation_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
                cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            )
        return "".join(block.text for block in response.content if block.type == "text").strip()

    def complete_json(
        self, system: str, prompt: str, temperature: float = 0.3, max_tokens: int = None,
        cacheable_prefix: str | None = None,
    ) -> str:
        return self._call_with_retry(
            self._call, system, prompt, temperature, max_tokens=max_tokens or 2000, cacheable_prefix=cacheable_prefix,
        )

    def complete_text(
        self, system: str, prompt: str, temperature: float = 0.4, max_tokens: int = None,
        stop: list[str] | None = None, cacheable_prefix: str | None = None,
    ) -> str:
        return self._call_with_retry(
            self._call, system, prompt, temperature, max_tokens=max_tokens or 1200, stop=stop,
            cacheable_prefix=cacheable_prefix,
        )
