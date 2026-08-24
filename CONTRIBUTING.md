# Contributing / Project Context

This doc covers the design principles and conventions worth knowing
before extending this codebase — the "why," where it isn't obvious from
the code itself. For current build status and the full phase-by-phase
history, see [`ARCHITECTURE.md`](./ARCHITECTURE.md). For setup, see
[`README.md`](./README.md).

(The author also keeps a much more detailed, personal working-notes file
locally, used with Claude Code during development — it isn't part of
this repo, since it includes real job-search activity specific to that
one deployment. Nothing about running or extending the project depends
on it.)

## Origin note

This project replaced an earlier prototype that had grown organically
past its own README and carried real issues: hardcoded secrets,
plaintext credential storage, an insecure default admin password,
LinkedIn/Google scraping as a sole strategy, blind auto-email to guessed
addresses, a dead "Rejected" Kanban column (jobs were hard-deleted
instead of retained), and no confirmation layer before real-world side
effects (submitting applications, sending emails). None of that old code
was carried forward — this is a from-scratch rebuild informed by those
lessons. If you're extending this project, don't reintroduce those
patterns.

## Product vision, in priority order

1. Personalized job search from the web — not LinkedIn-only. Legitimate
   free sources (Adzuna, direct Greenhouse/Lever/Ashby endpoints) since
   raw LinkedIn scraping is fragile and ToS-risky as a sole source.
2. Hands-free filtering with full user control (keywords, location,
   remote, salary, visa, seniority).
3. Resume tailoring + individually scored cover letters, tailored to
   each job description's actual language — not an abstract "market
   trend" model.
4. Auto-apply with a **human confirmation layer** (tunable window)
   before firing — never instant blind auto-submit.
5. Mailing/outreach automation — same review-gated confirmation pattern
   as #4. No blind auto-email to guessed addresses; a daily send cap.
6. Personalized interview prep — general (candidate background) +
   company-specific (job description + light company research).
7. Runs at **$0** — no paid hosting required. Local-first by default;
   a free-tier cloud VM is the documented fallback for 24/7 uptime
   independent of your own machine.

## Two-face product (one codebase, config-driven)

- **Personal instance**: real profile, real credentials, full
  automation, runs until you're hired.
- **Public showcase**: demo profile, automation off by default, clear
  setup + ethical-use docs. Same code, different config — see
  `APP_MODE` in `.env.example` and the README's "Public Showcase Mode"
  section.

## Working conventions

- **Every tunable number is a `GlobalSettings` field, live-editable from
  the dashboard — never a hardcoded constant.** Poll intervals,
  confirmation windows, retention days, score thresholds, budgets, all
  of it. If you add a new tunable behavior, add it here too.
- **Scam/repost flags and any automated warning are informational,
  never a silent filter.** The user stays in control of the call —
  surface a flag, don't act on it unilaterally.
- **No blind side effects.** Applications and outreach both go through
  a confirmation queue; nothing auto-fires without either explicit
  approval or an expired, visible countdown. Automated form
  submission never clicks the actual submit button itself — only the
  human's own click does, in a real, visible browser.
- **Destructive actions get their own careful design, not a rushed side
  effect of an adjacent feature.** Backup restore, for example, was
  deliberately built well after backup/export, with its own explicit
  design (safety-net snapshot first, admin-password gate, preview
  before touching anything, all-or-nothing).
- **Every AI-calling service goes through the LLM provider abstraction**
  (`app/services/llm/`, selected via `LLM_PROVIDER` in `.env`) — never
  import a provider SDK directly in a feature file. This is what keeps
  Claude, OpenAI-compatible providers (including Gemini's free tier),
  and a fully local Ollama model interchangeable with one env var.
- **Extend the existing schema before reaching for an ad-hoc migration.**
  `app/models.py` was designed with every planned phase's fields in mind
  up front (profile versioning, company memory, confirmation-queue
  fields, outreach caps, etc.) — check whether the field you need
  already exists before adding a new one. When you do add or change a
  column on an already-deployed database, use Alembic
  (`alembic revision --autogenerate`, review the generated migration
  before applying) rather than a hand-written `ALTER TABLE`.
- **A mechanical (non-LLM) safeguard catches AI fabrication in tailored
  documents** (`tailoring_service.py`'s unsupported-keyword check) — an
  LLM asked to tailor a resume to a job description will fabricate
  skills it doesn't have evidence for, even when explicitly told not
  to. Keep this check if you touch the tailoring prompts, and treat any
  future prompt change as needing live verification against a real LLM
  call, not just "does it return valid JSON."

## Testing

`pytest` (`tests/`, `pytest.ini`) runs against a disposable file-based
SQLite database — never a real deployment's data. See
`tests/MANUAL_QA.md` for the parts that inherently need a real browser
against a real third-party site or a real LLM call, which aren't
covered by the automated suite.
