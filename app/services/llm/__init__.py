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
    of every caller reimplementing this."""
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)
