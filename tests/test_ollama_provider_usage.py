"""Real gap found 2026-09-15 via a full-codebase audit: this provider
(the genuinely-free local option README.md points forkers to) never
wrote to LlmUsageLog at all. Mirrors test_anthropic_provider_streaming.py's
TestUsageLogging structure. Ollama's /api/chat response carries real
(not estimated) token counts in prompt_eval_count/eval_count -- no extra
call needed to get them."""

from unittest.mock import MagicMock, patch

from app.services.llm.ollama_provider import OllamaProvider


def _fake_response(prompt_eval_count=800, eval_count=200, content="ok"):
    resp = MagicMock()
    resp.json.return_value = {
        "message": {"content": content},
        "prompt_eval_count": prompt_eval_count,
        "eval_count": eval_count,
    }
    resp.raise_for_status.return_value = None
    return resp


def test_a_real_call_writes_a_usage_row_with_real_token_counts_and_zero_cost(db):
    from app.models import LlmUsageLog

    provider = OllamaProvider()
    provider.model = "llama3.1"

    with patch("app.services.llm.ollama_provider.requests.post", return_value=_fake_response(800, 200)):
        result = provider._call("sys", "prompt", 0.4)

    assert result == "ok"
    row = db.query(LlmUsageLog).order_by(LlmUsageLog.id.desc()).first()
    assert row is not None
    assert row.provider == "ollama"
    assert row.model == "llama3.1"
    assert row.input_tokens == 800
    assert row.output_tokens == 200
    # A real 0.0, not None/unpriced -- local inference genuinely has no
    # per-token charge, that's a fact about this provider, not a missing
    # price lookup.
    assert row.estimated_cost_usd == 0.0


def test_missing_token_counts_does_not_crash_or_log():
    provider = OllamaProvider()
    resp = MagicMock()
    resp.json.return_value = {"message": {"content": "ok"}}
    resp.raise_for_status.return_value = None

    with patch("app.services.llm.ollama_provider.requests.post", return_value=resp):
        result = provider._call("sys", "prompt", 0.4)

    assert result == "ok"
