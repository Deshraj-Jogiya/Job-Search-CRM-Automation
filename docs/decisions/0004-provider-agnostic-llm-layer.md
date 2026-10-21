# 0004: One LLM interface with interchangeable providers behind it

**Status:** Accepted

## Context
The project is meant to be usable by others at no cost, while the main deployment uses Anthropic models. Hard-wiring one provider would
block free-tier and local use.

## Decision
All model calls go through `app/services/llm/` with `complete_json` and `complete_text`. The provider is chosen with `LLM_PROVIDER`:
Anthropic by default, the Claude command-line client, Ollama for local models, and an OpenAI-compatible provider that also covers
Gemini. Every provider logs usage to `LlmUsageLog`, accepts stop sequences and supports
cache-aware cost logging where the provider offers caching.

## Consequences
- A fork can run on Gemini's free tier or a local Ollama model without code changes.
- Features that depend on provider behaviour (stop sequences for the ReAct agent, prompt caching) are implemented in each provider, and
  the cost dashboard stays accurate whichever provider is active.
