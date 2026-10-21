# 0002: A non-LLM check against the master profile gates every tailored document

**Status:** Accepted

## Context
LLM tailoring was observed adding skills, tools and outcomes that are not in the candidate's record. Asking the same or another
model to check its own output did not catch these reliably.

## Options
1. Trust the model's instructions not to invent anything.
2. Ask a second model to judge the first.
3. A mechanical check that compares every claim in the tailored document with the master profile (the single record of real
   skills, experience and education), plus a model pass only as an extra signal.

## Decision
Option 3. Tailored output is checked by plain code against the master profile: tools, numbers, companies, roles and dates must
already exist there. Hard findings (a claim not in the profile) trigger a bounded rewrite, up to two passes, feeding back the exact
finding; if it still fails, the application goes to manual review. Tone-only findings are logged without blocking.

## Consequences
- Tailored documents can only rearrange and reword real content; match scores are lower than an unchecked tailor would show.
- The check is deterministic and covered by tests, so it can be relied on when the model or provider changes.
- The guardrail must not be weakened to raise scores.
