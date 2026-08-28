"""
Provider-agnostic LLM interface. Every AI-driven service (matching,
tailoring, cover letters, outreach drafts, interview prep, email
classification) talks to this interface, never to a specific SDK
directly -- so switching providers is a config change, not a code
change.
"""

import time
from abc import ABC, abstractmethod


class LLMProvider(ABC):
    # A genuinely empty completion is a real, if uncommon, transient
    # provider hiccup -- confirmed live (2026-08-28): Gemini, via the
    # OpenAI-compatible endpoint, returned a blank response mid-way
    # through a real multi-pass tailoring run, failing the whole
    # operation (6+ sequential LLM calls, most already succeeded) on
    # JSON-parsing an empty string. A malformed-but-NONEMPTY response
    # (e.g. truncated by max_tokens) is deliberately NOT retried here --
    # the same prompt/token budget would likely truncate again, so
    # that's a real caller-side sizing issue, not a blip worth blindly
    # retrying. Concrete (not abstract) so every provider gets this for
    # free; each provider's complete_json/complete_text calls this
    # instead of its own _call directly.
    _MAX_EMPTY_RETRIES = 2

    def _call_with_retry(self, call_fn, *args, **kwargs) -> str:
        result = ""
        for attempt in range(self._MAX_EMPTY_RETRIES + 1):
            result = call_fn(*args, **kwargs)
            if result.strip():
                return result
            if attempt < self._MAX_EMPTY_RETRIES:
                time.sleep(1.5 * (attempt + 1))
        return result  # retries exhausted -- return whatever came back (empty); the
        # caller's own JSON/text handling surfaces the real failure, same as before

    @abstractmethod
    def complete_json(self, system: str, prompt: str, temperature: float = 0.3, max_tokens: int = None) -> str:
        """Send a prompt expecting a raw-JSON response back. Returns the
        raw text response (callers handle their own JSON parsing / code
        -fence stripping, since that's shared logic, not provider logic).
        max_tokens defaults to each provider's own standard JSON budget;
        pass an explicit value for a call whose expected response is
        larger than usual (e.g. asking for multiple structured sections
        at once) -- a response cut off mid-string by the token limit
        fails JSON parsing outright, it doesn't just truncate content.
        """
        raise NotImplementedError

    @abstractmethod
    def complete_text(self, system: str, prompt: str, temperature: float = 0.4, max_tokens: int = None) -> str:
        """Send a prompt expecting free-form text back (cover letters,
        outreach notes, interview prep narrative)."""
        raise NotImplementedError
