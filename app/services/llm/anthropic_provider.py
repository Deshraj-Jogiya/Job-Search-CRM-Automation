import os
from anthropic import Anthropic
from .base import LLMProvider


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
        return "".join(block.text for block in response.content if block.type == "text").strip()

    def complete_json(self, system: str, prompt: str, temperature: float = 0.3, max_tokens: int = None) -> str:
        return self._call_with_retry(self._call, system, prompt, temperature, max_tokens=max_tokens or 2000)

    def complete_text(self, system: str, prompt: str, temperature: float = 0.4, max_tokens: int = None) -> str:
        return self._call_with_retry(self._call, system, prompt, temperature, max_tokens=max_tokens or 1200)
