import os
from openai import OpenAI
from .base import LLMProvider


class OpenAICompatibleProvider(LLMProvider):
    """Works with OpenAI directly, or any OpenAI-compatible endpoint --
    including Google's Gemini OpenAI-compat endpoint, which is the free
    option most forkers without Claude access will reach for.

    Config:
        OPENAI_API_KEY
        OPENAI_MODEL     (e.g. "gpt-4o-mini" or "gemini-3.1-flash-lite")
        OPENAI_API_BASE  (optional -- omit for real OpenAI, set for Gemini
                           or any other compatible gateway)
    """

    def __init__(self):
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set in .env")
        self.client = OpenAI(api_key=api_key, base_url=os.getenv("OPENAI_API_BASE") or None)
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    def _call(self, system: str, prompt: str, temperature: float, max_tokens: int) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content.strip()

    def complete_json(self, system: str, prompt: str, temperature: float = 0.3, max_tokens: int = None) -> str:
        return self._call_with_retry(self._call, system, prompt, temperature, max_tokens=max_tokens or 2000)

    def complete_text(self, system: str, prompt: str, temperature: float = 0.4, max_tokens: int = None) -> str:
        return self._call_with_retry(self._call, system, prompt, temperature, max_tokens=max_tokens or 1200)
