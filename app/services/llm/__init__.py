"""
Single entry point every AI service should import from:

    from app.services.llm import get_llm_provider, parse_json_response

    llm = get_llm_provider()
    raw = llm.complete_json(system="...", prompt="...")
    data = parse_json_response(raw)

Which provider gets instantiated is controlled entirely by LLM_PROVIDER
in .env -- 'anthropic' (default, Claude), 'openai_compatible' (OpenAI,
Gemini, any compatible gateway), or 'ollama' (local/free).
"""

import os
import json
import functools


@functools.lru_cache(maxsize=1)
def get_llm_provider():
    provider_name = os.getenv("LLM_PROVIDER", "anthropic").lower().strip()

    if provider_name == "anthropic":
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider()
    elif provider_name == "openai_compatible":
        from .openai_compatible_provider import OpenAICompatibleProvider
        return OpenAICompatibleProvider()
    elif provider_name == "ollama":
        from .ollama_provider import OllamaProvider
        return OllamaProvider()
    else:
        raise RuntimeError(
            f"Unknown LLM_PROVIDER '{provider_name}'. Expected one of: "
            f"anthropic, openai_compatible, ollama."
        )


def parse_json_response(raw_text: str):
    """Shared helper: LLMs sometimes wrap JSON in markdown code fences
    regardless of instructions. Strip that before parsing, once, instead
    of every caller reimplementing this.

    Also recovers from mid-JSON truncation (see _recover_truncated_json)
    instead of raising and losing an entire generation. This happened for
    real, repeatedly, in interview_prep_service.py: a fixed max_tokens
    ceiling kept getting outgrown every time the schema gained a field or
    a real company's process was unusually rich, each time requiring a
    manual bump-the-number-and-redeploy cycle. Bumping the ceiling higher
    only delays the next recurrence -- it doesn't fix the underlying
    thing, which is that no fixed number can promise a variable-length,
    uncapped-by-design response (arbitrary round counts, arbitrary
    follow-up counts) never exceeds it again. Recovering the longest
    valid JSON prefix and dropping only the truncated tail means a
    response that runs long degrades to slightly less content instead of
    a hard failure that loses everything -- permanent regardless of how
    the schema or a given company's real process grows from here."""
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        recovered = _recover_truncated_json(text)
        if recovered is None:
            raise
        return recovered


def _longest_balanced_json_prefix(text: str) -> tuple[str, list]:
    """Scans text tracking JSON string state (respecting backslash
    escapes, so braces/brackets that appear inside string content are
    never mistaken for structural ones) and object/array nesting depth.
    Returns the longest prefix that ends immediately after a complete,
    fully-closed value at any nesting level, plus the stack of brackets
    still open at that point (what still needs closing to make the
    prefix valid JSON on its own)."""
    in_string = False
    escape = False
    stack: list = []
    last_safe_end = 0
    last_safe_stack: list = []
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
            last_safe_end = i + 1
            last_safe_stack = list(stack)
    return text[:last_safe_end], last_safe_stack


def _recover_truncated_json(text: str):
    """Returns a parsed dict/list recovered from a truncated JSON
    response, or None if no safe recovery was possible (e.g. the failure
    wasn't truncation at all, just malformed JSON -- callers should raise
    the original error in that case, not silently return something
    wrong)."""
    prefix, open_stack = _longest_balanced_json_prefix(text)
    if not prefix or not open_stack:
        return None
    closing = "".join("}" if ch == "{" else "]" for ch in reversed(open_stack))
    try:
        return json.loads(prefix + closing)
    except json.JSONDecodeError:
        return None
