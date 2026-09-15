"""Real gap found 2026-09-15 via a full-codebase audit: this provider
(the one README.md points forkers to for Gemini's free tier) never
wrote to LlmUsageLog at all, unlike anthropic_provider.py -- the
dashboard's cost card would silently show nothing the moment anyone
switched LLM_PROVIDER. Mirrors test_anthropic_provider_streaming.py's
TestUsageLogging structure."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.llm.openai_compatible_provider import OpenAICompatibleProvider


def _make_provider():
    with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
        return OpenAICompatibleProvider()


def _fake_response(content="ok", prompt_tokens=1000, completion_tokens=500):
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return SimpleNamespace(choices=[choice], usage=usage)


def test_a_real_call_writes_a_usage_row_with_real_token_counts(db):
    from app.models import LlmUsageLog

    provider = _make_provider()
    provider.model = "gemini-3.1-flash-lite"
    provider.client = MagicMock()
    provider.client.chat.completions.create.return_value = _fake_response(prompt_tokens=2000, completion_tokens=1000)

    result = provider._call("sys", "prompt", 0.4, max_tokens=100)

    assert result == "ok"
    row = db.query(LlmUsageLog).order_by(LlmUsageLog.id.desc()).first()
    assert row is not None
    assert row.provider == "openai_compatible"
    assert row.model == "gemini-3.1-flash-lite"
    assert row.input_tokens == 2000
    assert row.output_tokens == 1000


def test_cost_is_left_unset_rather_than_guessed(db):
    # No verified real pricing table for the open set of OpenAI-compatible
    # endpoints/models this provider can point at -- same honest fallback
    # anthropic_provider.py uses for a model outside ITS OWN table.
    from app.models import LlmUsageLog

    provider = _make_provider()
    provider.client = MagicMock()
    provider.client.chat.completions.create.return_value = _fake_response()

    provider._call("sys", "prompt", 0.4, max_tokens=100)

    row = db.query(LlmUsageLog).order_by(LlmUsageLog.id.desc()).first()
    assert row is not None
    assert row.estimated_cost_usd is None


def test_no_usage_in_response_does_not_crash():
    provider = _make_provider()
    provider.client = MagicMock()
    response = _fake_response()
    response.usage = None
    provider.client.chat.completions.create.return_value = response

    result = provider._call("sys", "prompt", 0.4, max_tokens=100)

    assert result == "ok"
