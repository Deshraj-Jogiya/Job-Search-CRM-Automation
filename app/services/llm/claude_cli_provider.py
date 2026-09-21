"""LLM provider that shells out to the real, locally-installed `claude`
program (the same binary you log in to interactively) instead of the
Anthropic API SDK -- every call draws on your Claude subscription's own
usage allowance, at $0 of real Anthropic-API billing, rather than the
metered API key AnthropicProvider uses.

Why this exists: the 2026-09-18 auto score/tailor rollout burned $173.36
on the metered API in about 6 hours (see feedback_llm_spend_guardrails
memory). Deshraj has a Pro subscription but no API budget he's willing to
keep spending from. Anthropic's own support article confirms personal
automation via `claude -p` on your own machine draws from your
subscription's plan limits, not separate metered billing -- see
https://support.claude.com/en/articles/15036540 . This is NOT the
Anthropic Python SDK and does NOT use ANTHROPIC_API_KEY at all; it
requires the `claude` CLI to already be installed and logged in
(`claude` then `/login`) on whatever machine runs this provider -- that
login lives outside this app entirely, same as any other locally
installed program, so this deliberately does not manage or store any
credential itself.

Only meant to run wherever a human is logged in to the real `claude`
program (your own PC) -- NEVER point this at a shared server process
under CLAUDE_CLI_PATH, since that would mean parking a personal
subscription login on a machine other people can reach. There is no
code-level way to enforce that; it's an operational rule, documented
here and in CLAUDE.md.

`total_cost_usd` in the CLI's own JSON output is a NOTIONAL
API-equivalent cost (what the same call would have cost on the metered
API) -- useful for comparing against the old $173 baseline and for
budget math elsewhere in the app, but it is not a real charge; nothing
is actually billed per call under a subscription."""

import json
import os
import subprocess
from .base import LLMProvider
from .usage_logging import log_usage

# Same real published per-token rates AnthropicProvider uses, so a
# CLI-vs-API budget comparison is apples to apples. Kept in sync with
# anthropic_provider.py's own table -- update both if pricing changes.
_PRICE_PER_MILLION_TOKENS = {
    "sonnet": {"input": 3.00, "output": 15.00},
    "haiku": {"input": 1.00, "output": 5.00},
    "opus": {"input": 5.00, "output": 25.00},
}


class ClaudeCliError(RuntimeError):
    """Raised when the `claude` binary itself can't be found or isn't
    logged in -- distinct from a normal empty-completion retry, since no
    amount of retrying fixes a missing login."""


def _price_for(model_alias: str, input_tokens: int, output_tokens: int) -> float | None:
    prices = _PRICE_PER_MILLION_TOKENS.get(model_alias)
    if not prices:
        return None
    return (input_tokens / 1_000_000 * prices["input"]) + (output_tokens / 1_000_000 * prices["output"])


class ClaudeCliProvider(LLMProvider):
    """Requires CLAUDE_CLI_PATH in .env pointing at your local `claude`
    binary (e.g. C:\\Users\\<you>\\.local\\bin\\claude.exe), already
    logged in via `claude` -> /login. CLAUDE_CLI_MODEL_SCORING and
    CLAUDE_CLI_MODEL_TAILORING pick the model alias per call type
    ('haiku' or 'sonnet'); complete_json (used for scoring) defaults to
    the scoring alias, complete_text (used for tailoring/cover letters)
    defaults to the tailoring alias."""

    def __init__(self):
        self.binary = os.getenv("CLAUDE_CLI_PATH")
        if not self.binary:
            raise RuntimeError("CLAUDE_CLI_PATH is not set in .env")
        if not os.path.isfile(self.binary):
            raise RuntimeError(f"CLAUDE_CLI_PATH does not point at a real file: {self.binary}")
        self.scoring_model = os.getenv("CLAUDE_CLI_MODEL_SCORING", "haiku")
        self.tailoring_model = os.getenv("CLAUDE_CLI_MODEL_TAILORING", "sonnet")
        # Seconds -- a hung `claude` process (e.g. waiting on a login
        # prompt that will never come non-interactively) must not block
        # a batch run forever.
        # 300s, not the CLI's own default -- confirmed live that a single
        # call can legitimately run past 180s under extended thinking
        # (a real test call ran 220s on nothing more than a large, if
        # nonsensical, prompt). A real end-to-end tailoring run (scoring
        # + multiple tailoring passes + a fabrication-safeguard retry)
        # took 1259s total across several separate calls -- this timeout
        # is per CALL, not per application, so that's expected and fine.
        self.timeout_seconds = int(os.getenv("CLAUDE_CLI_TIMEOUT_SECONDS", "300"))

    def _call(self, system: str, prompt: str, temperature: float, max_tokens: int,
              model_alias: str, stop: list[str] | None = None) -> str:
        # temperature/max_tokens aren't exposed as CLI flags (the CLI is a
        # session tool, not a raw completion API) -- accepted for
        # interface compatibility with the other providers but unused
        # here. stop sequences aren't exposed either; the base retry
        # logic and each caller's own prompt wording are the only guard
        # against runaway generation on this provider.
        #
        # Real bug found live: passing `prompt` as a CLI argument (as
        # the `claude -p "<prompt>"` docs show) hit Windows's own total
        # command-line length limit the moment a real scoring/tailoring
        # prompt (full profile + JD + rubric, tens of thousands of
        # characters) was used -- confirmed live via
        # `FileNotFoundError: [WinError 206] The filename or extension
        # is too long`, which is how Python's subprocess module surfaces
        # that specific Windows error, not a real missing-binary
        # condition. `system` stays a short, fixed instruction string
        # (safe as a flag); `prompt` -- the genuinely unbounded one --
        # goes over stdin instead, which has no such length cap on
        # either Windows or Linux.
        args = [
            self.binary, "-p",
            "--output-format", "json",
            "--model", model_alias,
            "--system-prompt", system,
            "--tools", "",  # no tool use -- this is a pure text-completion call
        ]
        try:
            result = subprocess.run(
                args, input=prompt, capture_output=True, text=True, encoding="utf-8",
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise ClaudeCliError(f"claude binary not found at {self.binary}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCliError(
                f"claude CLI call timed out after {self.timeout_seconds}s -- "
                "is it logged in? Run `claude` then /login on this machine."
            ) from exc

        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            # Nothing parseable came back at all -- treat as an empty
            # completion so the base class's retry loop handles it,
            # same as a genuinely blank API response.
            return ""

        if payload.get("is_error"):
            message = payload.get("result") or "unknown claude CLI error"
            if "not logged in" in message.lower() or "/login" in message.lower():
                raise ClaudeCliError(
                    "claude CLI is not logged in on this machine -- run `claude` then /login."
                )
            # Any other CLI-level error (rate limit, usage-limit reached
            # for the day, etc.) -- treat as empty so the caller's normal
            # empty-completion handling applies rather than crashing the
            # whole batch on one bad call.
            return ""

        usage = payload.get("usage") or {}
        input_tokens = (usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0) \
            + (usage.get("cache_read_input_tokens") or 0)
        output_tokens = usage.get("output_tokens") or 0
        notional_cost = payload.get("total_cost_usd")
        if notional_cost is None:
            notional_cost = _price_for(model_alias, input_tokens, output_tokens)
        log_usage(f"claude_cli_subscription", f"claude-{model_alias}", input_tokens, output_tokens, notional_cost)

        return (payload.get("result") or "").strip()

    def complete_json(self, system: str, prompt: str, temperature: float = 0.3, max_tokens: int = None) -> str:
        return self._call_with_retry(self._call, system, prompt, temperature, max_tokens, self.scoring_model)

    def complete_text(
        self, system: str, prompt: str, temperature: float = 0.4, max_tokens: int = None,
        stop: list[str] | None = None,
    ) -> str:
        return self._call_with_retry(
            self._call, system, prompt, temperature, max_tokens, self.tailoring_model, stop=stop,
        )
