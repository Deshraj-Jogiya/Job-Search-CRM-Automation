"""AnthropicProvider._call must stream unconditionally, not just past some
guessed max_tokens threshold -- the Anthropic SDK raises ValueError for a
non-streaming call once its own duration estimate crosses ~10 minutes, and
that estimate depends on max_tokens and model throughput together, not a
fixed number this code could hardcode against. Real failure hit in
production: a caller requesting deep, uncapped interview-prep coverage
pushed max_tokens to 24000 and hit this wall on the non-streaming path."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.llm.anthropic_provider import AnthropicProvider


def _make_provider():
    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        return AnthropicProvider()


def test_call_uses_streaming_and_returns_text():
    provider = _make_provider()

    fake_block = SimpleNamespace(type="text", text="hello world")
    fake_final_message = SimpleNamespace(content=[fake_block], usage=SimpleNamespace(input_tokens=10, output_tokens=5))
    fake_stream = MagicMock()
    fake_stream.get_final_message.return_value = fake_final_message
    fake_stream_manager = MagicMock()
    fake_stream_manager.__enter__.return_value = fake_stream
    fake_stream_manager.__exit__.return_value = False

    provider.client = MagicMock()
    provider.client.messages.stream.return_value = fake_stream_manager

    result = provider._call("system prompt", "user prompt", 0.4, max_tokens=24000)

    assert result == "hello world"
    provider.client.messages.stream.assert_called_once()
    provider.client.messages.create.assert_not_called()
    _, kwargs = provider.client.messages.stream.call_args
    assert kwargs["max_tokens"] == 24000


def test_call_joins_only_text_blocks():
    provider = _make_provider()

    text_block = SimpleNamespace(type="text", text="part one. ")
    other_block = SimpleNamespace(type="tool_use", text=None)
    fake_final_message = SimpleNamespace(
        content=[text_block, other_block], usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    fake_stream = MagicMock()
    fake_stream.get_final_message.return_value = fake_final_message
    fake_stream_manager = MagicMock()
    fake_stream_manager.__enter__.return_value = fake_stream
    fake_stream_manager.__exit__.return_value = False

    provider.client = MagicMock()
    provider.client.messages.stream.return_value = fake_stream_manager

    result = provider._call("sys", "prompt", 0.4, max_tokens=100)

    assert result == "part one."


class TestUsageLogging:
    """LlmUsageLog rows are written by _call itself, off the real
    response.usage the Anthropic SDK always returns -- added after the
    user asked what a real submission actually cost and the honest
    answer was that nothing tracked it. _log_usage opens its own DB
    session (see its own docstring for why), which lands in the same
    test database this `db` fixture points at."""

    def _fake_call(self, provider, input_tokens=1000, output_tokens=500):
        fake_block = SimpleNamespace(type="text", text="ok")
        fake_final_message = SimpleNamespace(
            content=[fake_block], usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        )
        fake_stream = MagicMock()
        fake_stream.get_final_message.return_value = fake_final_message
        fake_stream_manager = MagicMock()
        fake_stream_manager.__enter__.return_value = fake_stream
        fake_stream_manager.__exit__.return_value = False
        provider.client = MagicMock()
        provider.client.messages.stream.return_value = fake_stream_manager

    def test_a_real_call_writes_a_usage_row_with_real_token_counts(self, db):
        from app.models import LlmUsageLog

        provider = _make_provider()
        provider.model = "claude-sonnet-4-6"
        self._fake_call(provider, input_tokens=2000, output_tokens=1000)

        provider._call("sys", "prompt", 0.4, max_tokens=100)

        row = db.query(LlmUsageLog).order_by(LlmUsageLog.id.desc()).first()
        assert row is not None
        assert row.provider == "anthropic"
        assert row.model == "claude-sonnet-4-6"
        assert row.input_tokens == 2000
        assert row.output_tokens == 1000

    def test_cost_computed_from_the_real_pricing_table(self, db):
        from app.models import LlmUsageLog

        provider = _make_provider()
        provider.model = "claude-sonnet-4-6"  # $3.00/$15.00 per million tokens
        self._fake_call(provider, input_tokens=1_000_000, output_tokens=1_000_000)

        provider._call("sys", "prompt", 0.4, max_tokens=100)

        row = db.query(LlmUsageLog).order_by(LlmUsageLog.id.desc()).first()
        assert row.estimated_cost_usd == 18.00

    def test_unknown_model_leaves_cost_unset_rather_than_guessing(self, db):
        from app.models import LlmUsageLog

        provider = _make_provider()
        provider.model = "some-future-model-not-in-the-pricing-table"
        self._fake_call(provider)

        provider._call("sys", "prompt", 0.4, max_tokens=100)

        row = db.query(LlmUsageLog).order_by(LlmUsageLog.id.desc()).first()
        assert row is not None
        assert row.estimated_cost_usd is None

    def test_a_logging_failure_never_breaks_the_real_call(self, db, monkeypatch):
        # A broken DB write inside _log_usage (e.g. the DB is briefly
        # unreachable) must never take down the real LLM call it's
        # just trying to record -- _call still has to return the real
        # text result. Patches SessionLocal at its real import site
        # (app.database) since _log_usage imports it fresh each call.
        import app.database

        def _boom(*a, **kw):
            raise RuntimeError("db unavailable")

        monkeypatch.setattr(app.database, "SessionLocal", _boom)

        provider = _make_provider()
        self._fake_call(provider)

        result = provider._call("sys", "prompt", 0.4, max_tokens=100)

        assert result == "ok"
