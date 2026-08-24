# Career Pilot — Architecture & Build Status

Fresh build, replacing the old `Job-Search-CRM-Automation` repo entirely.
Designed against the full roadmap from the start so later phases extend
this foundation instead of retrofitting it. This file is a concise
current-state summary. See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for
the design principles and conventions behind it.

## Stack

- **Backend:** FastAPI + SQLAlchemy. SQLite is the $0, zero-setup default
  for a fresh install or showcase fork; the real personal instance runs
  on Supabase Postgres via `DATABASE_URL` (migrated 2026-08-19) — both
  are first-class, not "SQLite with Postgres as an afterthought."
- **LLM:** provider-agnostic (`app/services/llm/`) — Claude by default,
  swappable to OpenAI/Gemini/local-Ollama via `LLM_PROVIDER` in `.env`,
  no code changes required
- **Frontend:** server-rendered Jinja2 templates, a shared `_base.html`
  (title/header_title/nav/content/scripts blocks), design tokens matched
  to the portfolio site's real source (Phase 11) — glassmorphism cards,
  Inter/Outfit typography, working light/dark theme toggle. Short,
  potentially-numerous lists (search keywords, seniority/location
  exclusions) render as compact wrapped tag chips, not one full-width
  row per item — the latter looked fine at a handful of entries but
  turned into an extremely long, loud scroll at real-world list sizes.
- **Schema migrations:** Alembic (`alembic/`), adopted 2026-08-23 after
  five separate manual `ALTER TABLE` scripts against the live DB in one
  session. `alembic/env.py` connects through the app's own `engine`
  directly (not a second connection built from `alembic.ini`) and
  permanently excludes four tables that belong to a separate application
  (the portfolio site) sharing this same Supabase project — autogenerate's
  first pass proposed dropping them since they aren't in this app's own
  `models.py`; `env.py`'s `include_object` filter excludes them
  permanently so that can't recur.
  `Base.metadata.create_all()` still runs at startup unchanged, so a
  fresh install/fork needs zero extra steps; Alembic is for evolving an
  already-running deployment's schema from here on.
- **Security baseline:** CSRF protection (signed double-submit cookie,
  constant-time comparison), real signup/login/forgot-password
  (bcrypt-hashed single `AdminAccount`, HMAC-signed session + reset
  tokens -- see Phase 22) with fail-closed fallback to the legacy
  `DASHBOARD_PASSWORD` env var for a deployment that hasn't signed up
  yet, encrypted backup export (Fernet, `CREDENTIAL_ENCRYPTION_KEY`), a
  global `Referrer-Policy: no-referrer` header, mechanical escaping of
  all LLM/JD-derived content before it reaches reportlab's markup-aware
  PDF renderer. A dedicated security review (2026-08-23) found and fixed
  one real credential-leak bug (DB password interpolated into a
  user-facing error message) and one real auth-bypass bug (loopback-trust
  logic silently defeated by any reverse proxy) before either shipped to
  the real deployment.

## What's built

| Phase | Piece | Status |
|---|---|---|
| 0 | Foundation — DB/session, full data model, LLM abstraction, CSRF, kill switch, live-editable settings | Done |
| 1 | Living profile — variants, portfolio sync, LinkedIn paste-diff (approval-gated) | Done |
| 2 | Multi-source intake — LinkedIn, Adzuna, direct Greenhouse/Lever/Ashby board polling, JobRight company-discovery seeding; dedup, repost/scam/staleness/location/hard-eligibility flagging (all warn-only except location, which is a hard filter — a wrong-country posting is a hard eligibility fact, not a risk signal to weigh); self-healing profile-derived keyword/seniority targeting; Adzuna daily budget pacing | Done |
| 3 | Matching/tailoring/scoring — tailor→verify→refine loop over experience AND projects together (JD-relevance-driven project selection, not a fixed count), mechanical fabrication safeguard with tool-category equivalence (real Tableau experience honestly credits a Power BI requirement), required-vs-preferred qualification weighting so a strong required-skills match isn't capped by missing nice-to-haves | Done |
| 4 | Confirmation-gated queue — Needs Review / Pending Confirmation / auto-launch-on-clean-autofill-supported, quiet-hours-aware deadlines, digest notifications | Done |
| 5 | Outreach — draft/approve/send, no timers, optional Tavily/Hunter.io contact discovery (never auto-filled) | Done |
| 6 | Interview prep — general + company-specific (Tavily-grounded when configured) | Done |
| 7 | Outcome analytics — funnel, conversion rates, company memory | Done |
| 8 | Two-face packaging — `APP_MODE` selects personal vs. showcase from one codebase | Done |
| 9 | Encrypted backup/export — SQLite via `sqlite3.backup()`, Postgres via a dialect-agnostic row-level JSON export over this app's own `Base.metadata` (added 2026-08-23 — the original SQLite-only version had silently never worked against the real Supabase deployment). Restore added 2026-08-24 — admin-password-gated, preview-then-confirm, automatic safety-net backup taken first, all-or-nothing/same-dialect only. | Done |
| 10 | $0 deployment hardening — SQLite WAL mode, scheduler tick failure isolation | Done |
| 11 | Design/polish pass — shared base template, accessibility, responsive breakpoints | Done |
| 12 | Real application-form autofill — Playwright pre-fills Greenhouse/Lever/Ashby forms, human's own submit click is the only thing that ever submits; auto-launches automatically for a clean, sufficiently-well-matched (`min_score_for_auto_launch`, default 65), autofill-supported tailored application; mechanical (non-LLM) answers for EEO self-identification and "how did you hear about us" | Done — real end-to-end success confirmed 2026-08-23 (see Phase 16) |
| 13 | Code & comment quality pass | Done |
| 14 | Automated test suite — 50 tests as of 2026-08-23 | Done |
| 15 | Autofill generalization — verified across 10 real companies spanning all 3 ATS's | Done |
| 16 | Real end-to-end auto-launch verification | **Done, 2026-08-23** — a real, root-cause scoring fix (required-vs-preferred qualification weighting) took a real application from the high-70s% to 91%, the first application to ever clear the 85% bar. Tailoring it live surfaced and fixed a real `max_tokens` truncation bug; the resulting tailored resume was correctly fabrication-flagged (Needs Review, no browser opened) for one real overreach. Approving it launched a real, visible browser that pre-filled the real application form — confirmed via activity log and an independent `Get-Process` check of the real Chrome window. First fully successful, unforced run of the entire feature. |
| 17 | Submission auto-detection — the autofill browser's post-fill wait now polls for a real post-submit confirmation signal (URL/page-text pattern match against a captured pre-fill baseline) and auto-calls `mark_applied()` | **Built 2026-08-24** — not yet verified against a real submission |
| 18 | Production resilience — background-thread failure isolation broadened to catch real-world failures (not just each service's own narrow exception type), real `/health` endpoint (public, checks DB + scheduler liveness), retained logging to a local size-rotated file (`logs/app.log`, full tracebacks) layered under the DB activity log | Partially done — process supervision/auto-restart is a deployment-config decision for whenever this moves to the Oracle VM, not app code |
| 19 | API budget & rate-limit hardening — Adzuna's real monthly budget was being exhausted in under 2 days at this project's own default settings; now paced daily across the full period. Tavily/Hunter.io get a monthly counter + hard cap (called on-demand, not polled, so daily pacing doesn't fit) — a real quota exhaustion now logs as a distinct event instead of looking identical to "genuinely found nothing" | Done (LLM-provider rate-limit handling already covered by the Anthropic SDK's own retries — lower priority, not separately built) |
| 20 | Outreach handoff / "connection reaching phase" | **Explicitly on hold** per the user's own request |
| 21 | Release readiness — security review (done, findings above), dependency audit (`pip-audit` against the real vulnerability DB — found and removed two genuinely orphaned packages, `pypdf` and `python-docx`), `datetime.utcnow()` deprecation swept clean (39 call sites), Alembic adopted, `requirements-lock.txt` added (exact pinned versions of the real tested working set — see Setup below), CI (`.github/workflows/tests.yml` on every push/PR, `dependency-audit.yml` weekly + manual — added 2026-08-23, not yet committed), ARCHITECTURE.md sync (this edit) | Only the actual Oracle Cloud deployment remains open |
| 22 | Real login/signup/forgot-password — bcrypt-hashed single `AdminAccount` per deployment (not multi-tenant; each fork runs its own instance with its own `.env`/secrets), HMAC-signed session + password-reset tokens, fail-open fallback to the legacy `DASHBOARD_PASSWORD`-or-open behavior until someone actually signs up. Found and fixed a real, pre-existing CSRF-middleware bug (a genuine token mismatch crashed with an unhandled 500 instead of a clean 403) while testing it. Profile page has a real structured, dropdown-based form (work authorization, visa sponsorship, relocation, salary, notice period, EEO) instead of raw-JSON-paste-only, merge-saved without touching the rest of the profile. Dashboard now has a "Ready to start your job hunt?" onboarding section (a real profile + configured search keywords + automation still off) linking to the Jobs page's existing filter-review UI, ending in a real "Start Hunt" button. | Done, verified live end-to-end 2026-08-23/24 |

## Every tunable is already live-editable

Nothing is hardcoded: poll intervals, confirmation window (+ fast-track
override), retention days, outreach cap, location query, JobRight poll
interval, and more all live in `GlobalSettings` and are editable from
the dashboard.

## Setup

```bash
cp .env.example .env
# fill in SECRET_KEY, CREDENTIAL_ENCRYPTION_KEY, and your chosen LLM_PROVIDER's keys
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

For a reproducible install (the exact package versions actually tested,
not whatever a fresh resolve against `requirements.txt`'s loose `>=`
floors happens to pull that day), use `pip install -r
requirements-lock.txt` instead.

Visit `http://localhost:8000/` — you should see the dashboard shell with
the kill switch and settings panel live. `Base.metadata.create_all()`
builds the full schema on first run automatically.

**Changing the schema on an already-running deployment** (not a fresh
install): use Alembic, not a hand-written `ALTER TABLE`.

```bash
alembic revision --autogenerate -m "describe the change"
# review the generated file before applying -- autogenerate proposes,
# it doesn't always get it right (this project once had it propose
# dropping tables belonging to a different application sharing the
# same database -- see env.py's include_object filter)
alembic upgrade head
```

## Deployment

**The $0 constraint is about hosting, not total cost.** Render was tried
and cost money, so it's ruled out. LLM API calls are a separate, small,
unavoidable per-call cost (on-demand, not automatic) — `LLM_PROVIDER=ollama`
removes even that, at the cost of local model quality/speed.

**Local-first is the default and the recommended mode.** Run
`uvicorn app.main:app --port 8000` continuously on your own machine. For
it to survive logging out or a reboot:
- **Windows**: a Task Scheduler task triggered at log-on, "run whether
  user is logged on or not," with a restart-on-failure action.
- **macOS**: a `launchd` user agent (`~/Library/LaunchAgents/`) with
  `KeepAlive` set.
- **Linux**: a `systemd --user` service with `Restart=on-failure`.

None of these are shipped as files in this repo (no Dockerfile, no
service unit) — a documented decision point, not automated tooling,
since it depends on which OS you're actually running.

**Reliability hardening in place**: SQLite runs in WAL mode with a 10s
`busy_timeout`. The scheduler's periodic concerns each run in their own
session with their own exception isolation, logged to the visible
activity log on failure. Background threads for score/tailor/interview-
prep/intake/autofill (Phase 18) now catch failures broadly rather than
only each service's own narrow custom exception type, so a real
Playwright error, LLM timeout, or network failure surfaces as a visible
`attention_reason` instead of crashing the thread silently. Every
ERROR-level activity log entry, plus every background-task failure
(even ones that don't get their own activity-log row), is also mirrored
with a full traceback to a local rotating log file at `logs/app.log`
(5MB x 5 backups, gitignored) — this is what to check for the real root
cause of a failure beyond the truncated one-line summary shown in the
dashboard or an application's `attention_reason`.

**If 24/7 uptime independent of your own machine is ever needed**:
Oracle Cloud's AMD Always Free micro instance (`VM.Standard.E2.1.Micro`,
1/8 OCPU / 1GB RAM) — chosen 2026-08-22 specifically because it's
untouched by Oracle's June 2026 halving of the Arm/Ampere A1 free tier
(4 OCPU/24GB → 2 OCPU/12GB); the earlier assumption that Ampere A1's
original 4-OCPU/24GB tier would be the fallback is now stale. Fallback
if 1GB proves tight: Oracle's reduced-but-still-free 2 OCPU/12GB Ampere
A1 tier. Watch Oracle's idle-reclamation policy (instances below ~20%
CPU at the 95th percentile over a rolling 7-day window can be
reclaimed) — this app's own periodic polling should generate enough
real activity to avoid it, not yet confirmed live. Not built or
scripted here; a documented fallback path, not a default, since
local-first covers the current real need.

**A reverse proxy in front changes the auth story**: `DASHBOARD_PASSWORD`
auth has a loopback-trust convenience for genuinely local-only use,
gated behind an explicit `TRUST_LOOPBACK_AS_LOCAL=true` opt-in (unset by
default) — a TLS-terminating proxy on the same box forwarding to
`127.0.0.1` would otherwise make every external request look local and
silently bypass the password. Only set that flag if there is genuinely
no reverse proxy in front.

**Backups**: `/settings/backup/export` produces a real encrypted
snapshot against either database backend (SQLite or the real Postgres/
Supabase deployment — both fully working as of 2026-08-23). Restore
(`/settings/backup/restore/preview` → `/settings/backup/restore/
confirm`, added 2026-08-24) is gated behind `ADMIN_PASSWORD`, previews
row counts before touching anything, requires typing "RESTORE" to
confirm, takes an automatic safety-net backup of the current database
immediately before replacing it, and is all-or-nothing (same dialect
only — a SQLite backup can't restore onto a Postgres deployment or vice
versa). Still periodically export if running this unattended for
extended stretches, and keep the downloaded file somewhere outside the
machine running the app — restore existing doesn't replace having a
recent export on hand.

## Production readiness checklist

Phase 21's own explicit deliverable — the bar to point to, not a vague
"is this done yet." Reflects real, verified current state as of
2026-08-23, not aspirational status.

**Security**
- [x] CSRF protection on every state-changing route
- [x] No SQL injection surface (no raw string-built queries anywhere)
- [x] No XSS surface (Jinja2 autoescaping intact, zero `|safe`/`Markup()` usage)
- [x] Credentials never logged or leaked into a user-facing message (a real leak found and fixed 2026-08-23)
- [x] Dashboard auth doesn't silently bypass behind a reverse proxy (fixed 2026-08-23, explicit opt-in required)
- [x] LLM/JD-derived content escaped before reaching the PDF markup renderer
- [x] `Referrer-Policy` set; magic-link confirmation tokens can't leak via an external link click
- [x] Real login/signup/forgot-password (bcrypt-hashed password, signed session + reset tokens) — added 2026-08-23
- [x] Dependency vulnerability scan as a recurring/CI step (`.github/workflows/dependency-audit.yml`, weekly + manual dispatch — added 2026-08-23)
- [ ] HTTPS enforced — depends on the eventual real deployment's reverse-proxy config, not app-level

**Data safety**
- [x] Encrypted backup/export works for both SQLite and the real Postgres deployment
- [x] Backup restore — preview-then-confirm, safety-net backup taken automatically first, admin-password-gated (added 2026-08-24)
- [ ] Automated/scheduled backups (currently manual, on-demand only)

**Operational resilience**
- [x] Background-thread failure isolation (scheduler + every score/tailor/interview-prep/intake/autofill thread)
- [x] Real `/health` endpoint (checks DB + scheduler liveness, not just "the process accepts HTTP")
- [ ] Process supervision / auto-restart on crash — a deployment-config decision for whenever this actually moves to the Oracle VM, not app code
- [x] Structured/retained logging beyond the DB activity log (local size-rotated file, `logs/app.log`, full tracebacks — added 2026-08-23)
- [ ] External uptime monitoring actually pointed at `/health` (the endpoint exists; nothing polls it yet)

**Schema & dependencies**
- [x] Schema migration tool (Alembic) adopted, baselined against the live DB
- [x] Reproducible dependency install (`requirements-lock.txt`)

**Testing**
- [x] Automated test suite (50 tests) covering routing logic, matchers, budget math, eligibility/location detection
- [x] `tests/MANUAL_QA.md` documents what inherently needs a real browser or real LLM call and can't be automated
- [x] CI running the suite automatically (`.github/workflows/tests.yml`, every push/PR — added 2026-08-23, not yet pushed/committed pending the user's go-ahead)

**Deployment**
- [ ] Actually deployed to the Oracle Cloud VM — still running locally on the developer's own machine as of this checklist

**Explicitly deferred, not gaps** (each already made a deliberate call, not forgotten):
Phase 20 (outreach handoff — explicitly on hold pending a separate
discussion). Phase 16 (real end-to-end auto-launch verification) and
Phase 17 (submission auto-detection) are now both done — see the table
above.
