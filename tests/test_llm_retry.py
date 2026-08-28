"""LLMProvider._call_with_retry -- shared by every provider's
complete_json/complete_text. Added after a real live failure
(2026-08-28): a multi-pass tailoring run failed outright when one of
several sequential LLM calls came back completely empty (Gemini, via
the OpenAI-compatible endpoint), wasting the several already-successful
passes before it. Retries only a genuinely empty response, never a
malformed-but-nonempty one -- a truncated/bad-JSON response would
likely fail identically on retry with the same prompt/token budget, so
that's a real caller-side issue, not a transient blip.
"""

from unittest.mock import Mock, patch

from app.services.llm.base import LLMProvider


class _FakeProvider(LLMProvider):
    def complete_json(self, system, prompt, temperature=0.3, max_tokens=None):
        raise NotImplementedError

    def complete_text(self, system, prompt, temperature=0.4, max_tokens=None):
        raise NotImplementedError


def test_returns_immediately_on_a_nonempty_first_response():
    provider = _FakeProvider()
    call_fn = Mock(return_value="real content")

    with patch("app.services.llm.base.time.sleep") as sleep:
        result = provider._call_with_retry(call_fn, "sys", "prompt")

    assert result == "real content"
    call_fn.assert_called_once()
    sleep.assert_not_called()


def test_retries_once_after_an_empty_response_then_succeeds():
    provider = _FakeProvider()
    call_fn = Mock(side_effect=["", "real content on retry"])

    with patch("app.services.llm.base.time.sleep") as sleep:
        result = provider._call_with_retry(call_fn, "sys", "prompt")

    assert result == "real content on retry"
    assert call_fn.call_count == 2
    sleep.assert_called_once()


def test_whitespace_only_response_counts_as_empty():
    provider = _FakeProvider()
    call_fn = Mock(side_effect=["   \n  ", "real content"])

    with patch("app.services.llm.base.time.sleep"):
        result = provider._call_with_retry(call_fn, "sys", "prompt")

    assert result == "real content"
    assert call_fn.call_count == 2


def test_gives_up_after_max_retries_and_returns_the_empty_result():
    provider = _FakeProvider()
    call_fn = Mock(return_value="")

    with patch("app.services.llm.base.time.sleep"):
        result = provider._call_with_retry(call_fn, "sys", "prompt")

    assert result == ""
    assert call_fn.call_count == provider._MAX_EMPTY_RETRIES + 1


def test_passes_through_args_and_kwargs_on_every_attempt():
    provider = _FakeProvider()
    call_fn = Mock(side_effect=["", "ok"])

    with patch("app.services.llm.base.time.sleep"):
        provider._call_with_retry(call_fn, "sys", "prompt", 0.5, max_tokens=999)

    call_fn.assert_any_call("sys", "prompt", 0.5, max_tokens=999)
    assert call_fn.call_count == 2
