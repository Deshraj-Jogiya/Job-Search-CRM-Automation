# Career Pilot — Architecture

Technical reference for how the system is built. For setup, see
[`README.md`](./README.md). For the design principles behind these
choices, see [`CONTRIBUTING.md`](./CONTRIBUTING.md).

## Stack

- **Backend:** FastAPI + SQLAlchemy. SQLite is the zero-setup default
  for a fresh install; a hosted Postgres database (e.g. Supabase) is
  a first-class option via `DATABASE_URL`, not an afterthought.
- **LLM:** provider-agnostic (`app/services/llm/`) — Claude by default,
  swappable to OpenAI, Gemini's free tier, or a fully local Ollama
  model via `LLM_PROVIDER` in `.env`, no code changes required.
- **Frontend:** server-rendered Jinja2 templates, a shared `_base.html`
  layout, glassmorphism cards, Inter/Outfit typography, a working
  light/dark theme toggle. Short, potentially-numerous lists (search
  keywords, exclusions) render as compact wrapped tag chips rather than
  one full-width row per item, which stays legible at real-world list
  sizes.
- **Schema migrations:** Alembic (`alembic/`). `alembic/env.py`
  connects through the app's own `engine` directly rather than a
  second connection built from `alembic.ini`, and its `include_object`
  filter excludes any tables outside this app's own `models.py` — a
  safeguard for the common case of a shared database with another
  application's tables in it, so `alembic revision --autogenerate`
  can never propose dropping something it doesn't own.
  `Base.metadata.create_all()` still runs at startup, so a fresh
  install needs zero migration steps; Alembic is for evolving an
  already-running deployment's schema from there.
- **Security baseline:** CSRF protection (signed double-submit
  cookie, constant-time comparison), real signup/login/forgot-password
  (bcrypt-hashed `AdminAccount`, HMAC-signed session and reset tokens)
  with a fail-closed fallback to a legacy `DASHBOARD_PASSWORD` env var
  for a deployment that hasn't signed up yet, encrypted backup export
  (Fernet), a global `Referrer-Policy: no-referrer` header, and
  mechanical escaping of all LLM/job-description-derived content
  before it reaches the PDF renderer's markup-aware layer.

## What it does

- **Living profile.** Multiple named variants (e.g. "Data Engineering"
  vs. "ML Engineering"), synced from a portfolio site's `resume.json`,
  imported from a pasted LinkedIn export (AI-diffed, requires
  approval before it goes live), or edited directly. Every change is
  versioned.
- **Multi-source job intake.** LinkedIn, Adzuna, and direct
  Greenhouse/Lever/Ashby board polling, plus company discovery via
  JobRight. Deduplicates across sources and flags reposts, staleness,
  scam patterns, and eligibility issues — all warn-only except
  location, which is a hard filter (a wrong-country posting is a fact,
  not a risk to weigh). Search keywords and seniority targeting
  self-heal from the active profile if left unconfigured.
- **Matching, tailoring, and scoring.** A tailor → verify → refine
  loop across both experience and projects together, with
  JD-relevance-driven project selection rather than a fixed count. A
  mechanical (non-LLM) fabrication safeguard checks tailored content
  against the real profile, with tool-category equivalence so real
  Tableau experience can honestly credit a Power BI requirement
  without inventing anything. Qualification weighting distinguishes a
  job description's required skills from its "nice to have" list, so
  a strong required-skills match isn't capped by a missing optional
  one.
- **Confirmation-gated auto-apply.** Every tailored application gets
  a review window before it proceeds — quiet-hours-aware so a deadline
  never silently lapses overnight, with a shorter fast-track window
  for very strong, very fresh matches. A flagged application (a
  fabrication warning, a scam-pattern match) never auto-proceeds
  regardless of how long it waits.
- **Application-form autofill.** A real, visible Playwright browser
  pre-fills Greenhouse/Lever/Ashby application forms — contact fields,
  file uploads, screening questions, EEO self-identification answered
  mechanically rather than guessed by an LLM. The human's own submit
  click is the only thing that ever actually submits; nothing in this
  codebase clicks Submit or solves a CAPTCHA. A background watcher
  recognizes real post-submission confirmation signals and marks the
  application as applied automatically, with a manual fallback for
  when it doesn't.
- **Outreach.** Draft, review, and send — no timers, no auto-send.
  Optional contact discovery (Tavily + Hunter.io) finds a likely
  recruiter contact and their verified email; discovered contacts are
  never auto-filled into a message without review.
- **Interview prep.** General talking points from your own background,
  plus company-specific angles grounded in real research when
  configured.
- **Outcome analytics.** Funnel conversion rates, source performance,
  and company memory (so a company that's gone silent before is
  visible next time it appears).
- **Encrypted backup and restore.** Export produces an encrypted
  snapshot of the whole database. Restore is deliberately a two-step,
  admin-password-gated, preview-then-confirm flow — never one click —
  with an automatic safety-net backup of the current database taken
  immediately before anything is replaced.
- **Two-face packaging.** One codebase, `APP_MODE` selects between a
  real personal deployment and a public showcase (a fictional seeded
  profile, automation off by default) — see the README's
  [Public Showcase Mode](./README.md#public-showcase-mode) section.

## Every setting is live-editable

Nothing operationally significant is hardcoded: poll intervals,
confirmation windows, retention days, outreach caps, search location,
and every other tunable lives in `GlobalSettings` and is editable from
the dashboard, not buried in code or a config file.

## Setup

```bash
cp .env.example .env
# fill in SECRET_KEY, CREDENTIAL_ENCRYPTION_KEY, and your chosen LLM_PROVIDER's keys
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

For a reproducible install (the exact package versions this project is
actually tested against, not whatever a fresh resolve against
`requirements.txt`'s loose `>=` floors happens to pull), use
`pip install -r requirements-lock.txt` instead.

Visit `http://localhost:8000/` — `Base.metadata.create_all()` builds
the full schema automatically on first run.

**Changing the schema on an already-running deployment** (not a fresh
install): use Alembic, not a hand-written `ALTER TABLE`.

```bash
alembic revision --autogenerate -m "describe the change"
# review the generated file before applying -- autogenerate proposes,
# it doesn't always get it right (see env.py's include_object filter
# for one real example of what it can get wrong on a shared database)
alembic upgrade head
```

## Deployment

Local-first is the default: run `uvicorn app.main:app` continuously on
your own machine, kept alive across reboots via your OS's own service
manager (Task Scheduler on Windows, a `launchd` agent on macOS, a
`systemd --user` service on Linux). None of these are shipped as files
in this repo — which one applies depends on your OS.

For 24/7 uptime independent of your own machine, see
[`deploy/`](./deploy/) for a complete, verified setup on an Oracle
Cloud Always Free instance: systemd services, a real HTTPS reverse
proxy via Caddy (works even without an owned domain), and a virtual
display setup for the interactive autofill browser on a headless
server. `deploy/README.md` documents the real gotchas of that specific
platform (SELinux, IPv6-only database hostnames, Playwright's Linux
dependency support) so they don't need rediscovering.

**Reliability.** SQLite runs in WAL mode with a busy timeout so the
scheduler and a manual action don't collide. Every scheduled concern
runs in its own session with its own exception isolation, logged to
the activity log on failure. Background threads for scoring,
tailoring, interview prep, intake, and autofill catch failures broadly
rather than only each service's own narrow exception type, so a real
browser error, LLM timeout, or network failure surfaces as a visible
reason on the affected application instead of crashing the thread
silently. Every error-level activity log entry is also mirrored with a
full traceback to a local rotating log file (`logs/app.log`, gitignored)
for deeper debugging than the one-line summary shown in the dashboard.

**A reverse proxy in front changes the auth story.** `DASHBOARD_PASSWORD`
auth has a loopback-trust convenience for genuinely local-only use,
gated behind an explicit `TRUST_LOOPBACK_AS_LOCAL=true` opt-in (unset
by default) — a TLS-terminating proxy on the same box forwarding to
`127.0.0.1` would otherwise make every external request look local and
silently bypass the password. Only set that flag if there is genuinely
no reverse proxy in front.

**Backups.** `/settings/backup/export` produces an encrypted snapshot
against either database backend on demand. A daily automated backup
also runs on its own from the scheduler, straight to
`backups/scheduled/` (gitignored, retention count live-editable from
the dashboard, default 14) -- live-editable and on by default whenever
`CREDENTIAL_ENCRYPTION_KEY` is set, independent of the
`automation_enabled` kill switch. Restore is gated behind
`ADMIN_PASSWORD`, previews what it will change before touching
anything, requires typing a confirmation phrase, and takes an
automatic safety-net backup of the current database first. It's
all-or-nothing and same-dialect-only (a SQLite backup can't restore
onto a Postgres deployment or vice versa). The automated copies still
only live on the same machine running the app -- periodically move a
downloaded export somewhere outside it too.

## Security

- CSRF protection on every state-changing route
- No SQL injection surface — no raw string-built queries anywhere
- No XSS surface — Jinja2 autoescaping intact, no `|safe`/`Markup()` usage
- Credentials never logged or leaked into a user-facing message
- Dashboard auth doesn't silently bypass behind a reverse proxy
- LLM/job-description-derived content escaped before reaching the PDF renderer
- `Referrer-Policy` set; confirmation-email links can't leak via an external click
- Real login/signup/forgot-password with bcrypt-hashed passwords and signed tokens
- Dependency vulnerability scanning as a recurring CI step
- Row Level Security enabled on every table when running on Supabase, closing off its
  auto-generated public REST API even though this app never uses that path itself
- HTTPS is a deployment-config concern (reverse proxy / Caddy), not app-level

## Testing

`pytest` (`tests/`) runs against a disposable SQLite database, never
real data, and covers routing logic, matchers, budget math, and
eligibility/location detection. `tests/MANUAL_QA.md` documents what
inherently needs a real browser or a real LLM call and can't be
automated. CI (`.github/workflows/tests.yml`) runs the suite on every
push and PR; `dependency-audit.yml` runs a vulnerability scan weekly.

## Known gaps

- No external uptime monitoring configured against `/api/health` by default -- a per-deployment choice of provider left to whoever's running it (see [`deploy/README.md`](./deploy/README.md#7-health-check-watchdog))
- Outreach handoff (the "we're now emailing back and forth" phase) is intentionally out of scope for now
