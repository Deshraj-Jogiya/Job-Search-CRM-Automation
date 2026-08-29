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
    fake_final_message = SimpleNamespace(content=[fake_block])
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
    fake_final_message = SimpleNamespace(content=[text_block, other_block])
    fake_stream = MagicMock()
    fake_stream.get_final_message.return_value = fake_final_message
    fake_stream_manager = MagicMock()
    fake_stream_manager.__enter__.return_value = fake_stream
    fake_stream_manager.__exit__.return_value = False

    provider.client = MagicMock()
    provider.client.messages.stream.return_value = fake_stream_manager

    result = provider._call("sys", "prompt", 0.4, max_tokens=100)

    assert result == "part one."
