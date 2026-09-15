import os
import requests
from .base import LLMProvider
from .usage_logging import log_usage


class OllamaProvider(LLMProvider):
    """Local, fully free option for forkers who don't want to use any
    paid/rate-limited API at all. Requires Ollama running locally
    (https://ollama.com) with a model already pulled.

    Config:
        OLLAMA_HOST   (default http://localhost:11434)
        OLLAMA_MODEL  (e.g. "llama3.1" -- must already be pulled)
    """

    def __init__(self):
        self.host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self.model = os.getenv("OLLAMA_MODEL", "llama3.1")

    def _call(self, system: str, prompt: str, temperature: float) -> str:
        response = requests.post(
            f"{self.host}/api/chat",
            json={
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": temperature},
            },
            timeout=120,
        )
        response.raise_for_status()
        body = response.json()
        # Real gap found 2026-09-15: never logged usage at all, unlike
        # anthropic_provider.py -- the dashboard's cost card would
        # silently show nothing for forkers running the genuinely-free
        # local option. prompt_eval_count/eval_count are Ollama's own
        # real token counts (not estimated) from the same response, no
        # extra call needed. cost_usd is a real 0.0, not None/unpriced --
        # local inference has no per-token charge, that's a fact about
        # this provider, not a missing price lookup.
        if "prompt_eval_count" in body and "eval_count" in body:
            log_usage("ollama", self.model, body["prompt_eval_count"], body["eval_count"], 0.0)
        return body["message"]["content"].strip()

    def complete_json(self, system: str, prompt: str, temperature: float = 0.3, max_tokens: int = None) -> str:
        # Ollama's local /api/chat endpoint isn't token-capped the same
        # way here -- max_tokens accepted for interface parity with the
        # other providers, not forwarded (matches complete_text below).
        return self._call_with_retry(self._call, system, prompt, temperature)

    def complete_text(self, system: str, prompt: str, temperature: float = 0.4, max_tokens: int = None) -> str:
        return self._call_with_retry(self._call, system, prompt, temperature)
