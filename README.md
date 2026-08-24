# Career Pilot — Job Search CRM & Automation Command Center

A personal job-search command center: ingest postings, AI-score and tailor
resumes/cover letters, and (in later phases) run a human-confirmation-gated
auto-apply and outreach pipeline — built to run at $0.

This repo was rebuilt from scratch. It replaces an earlier prototype that
had grown past its own documentation and carried real security issues
(hardcoded secrets, plaintext credential storage, an insecure default admin
password, and unattended auto-apply/auto-email with no human checkpoint).
None of that old code is carried forward.

**Full details:** see [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the current
build status and phase roadmap, and [`CONTRIBUTING.md`](./CONTRIBUTING.md)
for the design principles and conventions worth knowing before extending
this codebase.

## Stack

- FastAPI + SQLAlchemy + SQLite (Postgres-compatible via `DATABASE_URL`)
- Provider-agnostic LLM layer (`app/services/llm/`) — Claude by default,
  swappable to OpenAI, Gemini's free tier, or a fully local Ollama model
  via one `.env` value, no code changes required
- CSRF protection, fail-closed admin auth, and encrypted-credential-storage
  scaffolding are baseline from day one

## Quick start

```bash
cp .env.example .env
# fill in SECRET_KEY, CREDENTIAL_ENCRYPTION_KEY, and your chosen LLM_PROVIDER's keys
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Visit `http://localhost:8000/` — you'll see the dashboard shell with a
global automation kill switch and every tunable setting (poll intervals,
confirmation windows, retention days, outreach caps) live-editable.

## Status

Foundation, living profile, multi-source job intake (LinkedIn, Adzuna,
and direct Greenhouse/Lever/Ashby board polling), AI matching/tailoring
with a fabrication safeguard, the confirmation-gated auto-apply queue,
outreach automation with contact discovery, interview prep generation,
outcome analytics, encrypted backup/export, $0 deployment hardening,
two-face packaging, and a dedicated design/polish pass (glassmorphism
UI, light/dark theme, responsive layout, accessibility) are all built
and live — every phase on the original roadmap is done. See
`ARCHITECTURE.md` for the full phase table.

## Public Showcase Mode

This is primarily a real, personal-use tool, but the same codebase can
run as a public demo (`APP_MODE=showcase` in `.env`) instead of the
default personal mode. Showcase mode:

- Auto-seeds a fictional demo profile ("Jordan Ellis," not a real
  person) on first startup, so there's something to explore
  immediately instead of an empty shell.
- Defaults the automation kill switch OFF for a brand-new deployment
  (still toggleable from the dashboard -- this is a safe default, not
  a lock). This never affects a deployment that already has settings
  saved, including the real personal instance this project is actually
  used from.

**Before turning automation on in a showcase deployment:**

- This is a demonstration of the architecture, not a scraping or spam
  service. Respect the ToS of every source it touches (LinkedIn's
  guest search endpoint in particular is undocumented and ToS-
  sensitive — see `CONTRIBUTING.md`'s Origin note on why this project
  treats it as one source among several, not a sole strategy).
- Don't point it at real job sites while impersonating someone else, or
  use the outreach feature to email real people on a fictional
  candidate's behalf — outreach still requires an explicit human click
  per message (no auto-send, by design), but the responsibility for
  what gets sent is yours once you enable it.
- LLM calls (scoring, tailoring, interview prep) use your own API key
  and have a real, small per-call cost — this isn't free to run, even
  though hosting is.
- If you're running this to evaluate the project rather than to
  actually job-search with it, leave automation off and use the manual
  "Run Intake Now" / score / tailor buttons to see the flow without
  anything running unattended.
