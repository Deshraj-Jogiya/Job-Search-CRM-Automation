"""
Provider-agnostic LLM interface. Every AI-driven service (matching,
tailoring, cover letters, outreach drafts, interview prep, email
classification) talks to this interface, never to a specific SDK
directly -- so switching providers is a config change, not a code
change.
"""

from abc import ABC, abstractmethod


class LLMProvider(ABC):
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
