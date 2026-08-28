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
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in response.content if block.type == "text").strip()

    def complete_json(self, system: str, prompt: str, temperature: float = 0.3, max_tokens: int = None) -> str:
        return self._call_with_retry(self._call, system, prompt, temperature, max_tokens=max_tokens or 2000)

    def complete_text(self, system: str, prompt: str, temperature: float = 0.4, max_tokens: int = None) -> str:
        return self._call_with_retry(self._call, system, prompt, temperature, max_tokens=max_tokens or 1200)
