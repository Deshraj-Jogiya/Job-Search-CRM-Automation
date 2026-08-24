# Career Pilot — Job Search CRM & Automation Command Center

A personal job-search command center: it pulls in postings from
multiple sources, scores and tailors your resume/cover letter against
each one with an AI pass that's mechanically checked for fabrication,
and runs a human-confirmation-gated auto-apply and outreach pipeline —
built to run at $0.

Every side effect that matters — submitting an application, sending an
email — stays behind an explicit human decision. Automation handles
the repetitive part (finding postings, drafting tailored materials,
pre-filling forms); a person still reviews and clicks submit.

**Full details:** see [`ARCHITECTURE.md`](./ARCHITECTURE.md) for how
the system is built, and [`CONTRIBUTING.md`](./CONTRIBUTING.md) for
the design principles worth knowing before extending it.

## Stack

- FastAPI + SQLAlchemy + SQLite (Postgres-compatible via `DATABASE_URL`)
- Provider-agnostic LLM layer (`app/services/llm/`) — Claude by default,
  swappable to OpenAI, Gemini's free tier, or a fully local Ollama model
  via one `.env` value, no code changes required
- CSRF protection, fail-closed admin auth, and encrypted credential
  storage are baseline, not bolted on

## Quick start

```bash
cp .env.example .env
# fill in SECRET_KEY, CREDENTIAL_ENCRYPTION_KEY, and your chosen LLM_PROVIDER's keys
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Visit `http://localhost:8000/` — you'll see the dashboard with a
global automation switch and every tunable setting (poll intervals,
confirmation windows, retention days, outreach caps) live-editable, no
code changes needed.

## What's here

Living profile management, multi-source job search (LinkedIn, Adzuna,
and direct Greenhouse/Lever/Ashby board polling), AI matching and
tailoring with a fabrication safeguard, a confirmation-gated auto-apply
queue, real application-form autofill, outreach automation with
contact discovery, interview prep generation, outcome analytics,
encrypted backup/restore, and a deployment setup for running this on a
free-tier cloud VM. See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for how
each piece works.

## Public Showcase Mode

This is primarily a real, personal-use tool, but the same codebase can
run as a public demo (`APP_MODE=showcase` in `.env`) instead of the
default personal mode. Showcase mode:

- Auto-seeds a fictional demo profile ("Jordan Ellis," not a real
  person) on first startup, so there's something to explore
  immediately instead of an empty shell.
- Defaults the automation switch OFF for a brand-new deployment (still
  toggleable from the dashboard — a safe default, not a lock). This
  never affects a deployment that already has settings saved.

**Before turning automation on in a showcase deployment:**

- This is a demonstration of the architecture, not a scraping or spam
  service. Respect the terms of service of every source it touches
  (LinkedIn's guest search endpoint in particular is undocumented and
  sensitive — see [`CONTRIBUTING.md`](./CONTRIBUTING.md)'s Origin note
  on why this project treats it as one source among several, not a
  sole strategy).
- Don't point it at real job sites while impersonating someone else, or
  use the outreach feature to email real people on a fictional
  candidate's behalf — outreach still requires an explicit human click
  per message (no auto-send, by design), but the responsibility for
  what gets sent is yours once you enable it.
- LLM calls (scoring, tailoring, interview prep) use your own API key
  and have a real, small per-call cost — this isn't free to run, even
  though hosting is.
- If you're evaluating the project rather than actually job-searching
  with it, leave automation off and use the manual "Search Now" /
  score / tailor buttons to see the flow without anything running
  unattended.

## License

MIT — see [`LICENSE`](./LICENSE).
