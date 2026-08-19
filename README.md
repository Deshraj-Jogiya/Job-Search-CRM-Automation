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
build status and phase roadmap, and [`CLAUDE.md`](./CLAUDE.md) for the full
project context (useful if you're picking this up in Claude Code or handing
it to another AI assistant).

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
outcome analytics, and encrypted backup/export are all built and live.
Two-face packaging (personal vs. public-showcase config), further
deployment hardening, and a dedicated design/polish pass are what's
left — see `ARCHITECTURE.md` for the full, up-to-date phase table.
