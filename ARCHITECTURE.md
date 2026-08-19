# Career Pilot — Architecture & Build Status

Fresh build, replacing the old `Job-Search-CRM-Automation` repo entirely.
Designed against the full roadmap from the start so later phases extend
this foundation instead of retrofitting it.

## Stack

- **Backend:** FastAPI + SQLAlchemy + SQLite ($0, single-user-first; Postgres-
  compatible via `DATABASE_URL` if ever needed)
- **LLM:** provider-agnostic (`app/services/llm/`) — Claude by default,
  swappable to OpenAI/Gemini/local-Ollama via `LLM_PROVIDER` in `.env`,
  no code changes required
- **Frontend:** server-rendered Jinja2 templates (matches the original
  project's approach; the "magic" visual layer is a dedicated later phase,
  not skipped — just sequenced last so it's built on a stable product)
- **Security baseline (built in from day one, not bolted on later):**
  CSRF protection, fail-closed admin auth, encrypted credential storage
  helper ready for when portal accounts are needed, no hardcoded secrets

## What's built (this drop)

| Piece | File(s) | Status |
|---|---|---|
| DB engine/session | `app/database.py` | Done |
| Full data model | `app/models.py` | Done — schema covers every phase (profile versioning, companies/repost tracking, confirmation queue, outreach, interview prep, tunable settings) up front |
| Credential encryption helper | `app/services/crypto_utils.py` | Done, not yet wired to a model (no portal-account feature exists yet in this rebuild) |
| LLM provider abstraction | `app/services/llm/` | Done — Anthropic, OpenAI-compatible, Ollama |
| CSRF protection | `app/csrf.py` | Done |
| Activity logging | `app/services/activity_logger.py` | Done |
| App skeleton | `app/main.py` | Done — health check, dashboard shell, kill switch, live-editable settings, mounts `app/routers/` |
| Base template/styling & design polish (Phase 11) | `app/templates/_base.html`, `app/static/css/style.css`, `app/static/js/theme.js` | Done — every page extends a shared base template (title/header_title/nav/content/scripts blocks) instead of duplicating head/header boilerplate 7 times. Design tokens pulled directly from the portfolio site's (`Deshraj-Jogiya.github.io`) actual source: glassmorphism cards, Inter/Outfit typography, the indigo/purple/emerald/amber palette, and the same working light/dark theme-toggle pattern (localStorage + `prefers-color-scheme`, applied to `<html>` via a synchronous inline script to avoid a flash of the wrong theme). Responsive breakpoints at 768px/480px; wide tables sit in a scrollable container instead of overflowing the page; every form input has a real accessible label; deliberate `:focus-visible` styling throughout. |
| Living profile (Phase 1) | `app/services/profile_service.py`, `app/routers/profile.py`, `app/templates/profile.html` | Done — profile variants (named flavors), portfolio `resume.json` sync (activates immediately, zero-risk per CLAUDE.md), LinkedIn paste-diff via LLM (creates a **pending** version, requires explicit approve/reject — never auto-activates), manual JSON seed/edit as the bootstrap path since no starter profile data ships with the rebuild. Note: the portfolio site doesn't serve `resume.json` yet, so "Sync from Portfolio" currently fails gracefully with a clear error until that endpoint exists there — manual seed / LinkedIn import are the working paths meanwhile. |
| Job intake (Phase 2, both slices) | `app/services/sources/` (linkedin, adzuna, greenhouse, lever, ashby), `app/services/board_discovery.py`, `app/services/intake_service.py`, `app/services/scheduler.py`, `app/routers/jobs.py`, `app/templates/jobs.html` | LinkedIn (no credentials needed), Adzuna (skips gracefully without `ADZUNA_APP_ID`/`ADZUNA_APP_KEY`, hard-capped against its free-tier monthly budget), and direct Greenhouse/Lever/Ashby board polling are all live: fuzzy dedup (exact by source+external_id/url, fuzzy by normalized company+title), repost detection, keyword-based scam-pattern flagging, staleness flagging (all warn-only, never filter, per CLAUDE.md), search keyword management UI, per-source status panel, manual "run now" trigger, all gated by the `automation_enabled` kill switch. The three direct-ATS sources are driven by `Company.greenhouse_slug`/`lever_slug`/`ashby_slug` (not keyword search, which these platforms don't offer across companies) -- auto-detected the first time a company is seen via any source, via a capped, concurrent backfill sweep (`board_discovery.py`) rather than inline at company-creation time, since probing scales with network calls, not intake volume; manual add/override is available from the Jobs page for companies auto-detection misses. |
| Matching/tailoring/scoring (Phase 3) | `app/services/matching_service.py`, `app/services/tailoring_service.py`, `app/routers/jobs.py` (detail/score/tailor routes), `app/templates/application_detail.html` | On-demand (not automatic on ingest — real LLM cost) match scoring against the active profile, a genuine tailor→verify→refine loop for experience bullets (real recomputed final score from the last verify pass, never a placeholder — unlike the deleted prototype), tailored summary/skills/project-selection, independent cover-letter generation + scoring. **Includes a mechanical post-tailoring fabrication check**: verified during this build that the LLM (Gemini flash-lite, at least) will inject ATS keywords into experience bullets and the cover letter that have zero support anywhere in the candidate's real profile, despite explicit anti-fabrication instructions in the prompt. The check compares every keyword the refine loop claims to have "resolved" against the full original profile text; any with no match surfaces a clear `attention_reason` warning on the application (visible on both the jobs list and detail page) rather than silently shipping a fabricated resume. This is a real, demonstrated risk in this feature, not theoretical — worth keeping if this logic is ever touched. |
| Confirmation-gated queue (Phase 4) | `app/services/confirmation_service.py`, `app/services/notification_service.py`, `app/services/confirmation_tokens.py`, `app/routers/confirmation.py`, `app/templates/confirm.html` | A tailored application routes to **Needs Review** (no timeout, ever) if Phase 2/3 flagged it (scam pattern or tailoring fabrication warning), otherwise to **Pending Confirmation** with a quiet-hours-aware deadline (fast-track window if high score + fresh, standard window otherwise). On explicit approval or an unattended timeout, status becomes **Approved** — tailored docs ready, but there is still no real portal-submission engine, so this is honestly "ready to submit," not "submitted." A separate explicit **Mark as Applied** action records that the human actually did it. Rejected applications are hard-deleted after `rejected_retention_days` (their `JobPosting` is kept for company memory/repost detection). All of it gated by the `automation_enabled` kill switch, including the scheduler sweeps. **Notification volume fix (2026-08-17, see CLAUDE.md)**: the initial build emailed one-click links per application with no batching — a bulk review page (`/jobs/review`, table + multi-select + bulk approve/reject, structurally separated Pending Confirmation vs. flagged Needs Review sections) is now the primary way to process volume; individual emails are reserved for fast-track only, everything else batches into a single periodic digest pointing at the review page. |
| Outreach automation (Phase 5) | `app/services/outreach_service.py`, `app/services/contact_discovery_service.py`, `app/services/email_utils.py`, `app/routers/outreach.py`, outreach section in `app/templates/application_detail.html` | Draft → Approved → Sent, with **no timers or auto-send anywhere** (unlike Phase 4 — a real email to a real external person is immediately irreversible, so every state change is a live explicit click). Email channel sends via the same daily-cap-enforced, syntax+MX-verified path; LinkedIn channels are drafted but never auto-sent (LinkedIn automation was ruled out for account-risk reasons elsewhere in this project) — the user copies the note and confirms via a separate Mark as Sent. Optional contact discovery (Tavily web search + Hunter.io domain-search, both free-tier/no-card/graceful-degrade) surfaces candidate name/LinkedIn/email-with-confidence as labeled, unverified suggestions — never auto-filled, never algorithmically guessed — that still flow through the same manual draft/approve/send pipeline once picked. See CLAUDE.md's Phase 5 section for the mid-build design correction that added discovery. |
| Encrypted backup/export (Phase 9) | `app/services/backup_service.py`, route in `app/main.py` | On-demand encrypted DB export from the dashboard -- consistent SQLite snapshot via `sqlite3`'s own online `.backup()` API (safe under concurrent writes from the scheduler thread), encrypted with the `CREDENTIAL_ENCRYPTION_KEY` Fernet key that's existed since Phase 0 but was unused until now. **Export only, on purpose** -- restore is a separate, deliberately unbuilt decision, since overwriting a live database is destructive and deserves its own careful design rather than a rushed side effect. Picked ahead of Phase 6/7 in the numbered order since real personal usage was about to start. |
| Interview prep (Phase 6) | `app/services/interview_prep_service.py`, routes in `app/routers/jobs.py`, section in `app/templates/application_detail.html` | On-demand (real LLM cost, not automatic), two independent passes: general prep grounded only in the candidate's real profile (role-generic), and company-specific prep grounded in the JD plus optional live company research via Tavily (`contact_discovery_service.tavily_search`, promoted to shared) -- gracefully degrades to JD-only when Tavily isn't configured, same posture as every other optional integration here. Available for any application except Rejected. |
| Outcome analytics (Phase 7) | `app/services/analytics_service.py`, `app/routers/analytics.py`, `app/templates/analytics.html` | Read-only `/analytics` page: status funnel, apply/interview/offer conversion rates, speed-to-apply, by-source and by-score-band apply rates, flagged-vs-clean apply rates, company memory (deprioritized/blocked, most "Not Selected"). Required first wiring up `Interviewing`/`Offer`/a new `Not Selected` status (manual self-report via `confirmation_service.mark_interviewing/mark_offer/mark_not_selected`, same trust model as `mark_applied`) -- those states existed only in a schema comment before this, so there was no real data past `Applied` to analyze. `Not Selected` is deliberately distinct from `Rejected` (which means declined-before-applying and gets swept/deleted) since it's real applied-and-declined history worth keeping. |

| $0 deployment hardening (Phase 10) | `app/database.py`, `app/services/scheduler.py` | SQLite WAL mode + busy_timeout to close a known concurrent-writer "database is locked" risk; scheduler tick failure isolation (4 independent concerns, each own session/exception handling, failures logged visibly instead of printed to an unwatched console). See the Deployment section below for the local-first-vs-Oracle-fallback operational guidance (documentation, not new automation). |
| Two-face packaging (Phase 8) | `app/app_mode.py`, `app/fixtures/demo_profile.json`, wiring in `app/models.py`/`app/main.py`, `app/templates/dashboard.html` | One `APP_MODE` env var (default `personal`) selects personal vs. public-showcase deployment from the same codebase, no fork needed. Showcase mode auto-seeds a fictional demo profile on first startup (via the same `profile_service` path a human uses, only when no profile exists yet) and defaults automation OFF for a brand-new deployment -- a real, toggleable default, not a hard lock, and one that never touches an existing deployment's already-created settings row. Dashboard shows an informational banner; README documents ethical-use expectations for anyone running a public instance. |

## What's NOT built yet (by design — next phases)

- Real portal-submission automation (Playwright-style auto-fill/submit) — explicitly out of Phase 4's scope; a separate, later, ToS-sensitive decision, not assumed by default
- Backup restore (deliberately deferred, see Phase 9 above)
- Final design/polish pass (Phase 11 — deliberately last, now that every backend phase is built)

## Every tunable is already live-editable

Nothing from our planning conversation is hardcoded: poll intervals,
confirmation window (+ fast-track override), retention days, outreach cap
all live in `GlobalSettings` and are editable from the dashboard right now,
even though the features that consume most of them don't exist yet. This
was intentional — the settings surface and the features that read it can
now be built independently, in any order.

## Setup

```bash
cp .env.example .env
# fill in SECRET_KEY, CREDENTIAL_ENCRYPTION_KEY, and your chosen LLM_PROVIDER's keys
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Visit `http://localhost:8000/` — you should see the dashboard shell with
the kill switch and settings panel live.

## Deployment (Phase 10)

**The $0 constraint is about hosting, not total cost.** Render was tried
and cost money, so it's ruled out — there is no paid hosting anywhere in
this project's design. LLM API calls (Claude by default) are a separate,
small, unavoidable per-call cost (a few cents per score/tailor/prep
generation, since those are on-demand, not automatic) — switching
`LLM_PROVIDER` to `ollama` in `.env` removes even that, at the cost of
local model quality/speed.

**Local-first is the default and the recommended mode.** Run
`uvicorn app.main:app --port 8000` continuously on your own machine.
For it to survive logging out or a reboot without you remembering to
restart it by hand:
- **Windows**: a Task Scheduler task triggered at log-on, "run whether
  user is logged on or not," with a restart-on-failure action.
- **macOS**: a `launchd` user agent (`~/Library/LaunchAgents/`) with
  `KeepAlive` set.
- **Linux**: a `systemd --user` service with `Restart=on-failure`.

None of these are shipped as files in this repo (no Dockerfile, no
service unit) — this is a documented decision point, not automated
tooling, since it depends on which OS you're actually running on.

**Reliability hardening now in place** (this phase): SQLite runs in WAL
mode with a 10s `busy_timeout` (`app/database.py`) so the scheduler
thread and per-request background threads (intake/score/tailor, each
opens its own connection) don't throw "database is locked" when they
write at the same moment — a risk flagged since the original design
discussion, now closed. The scheduler's 4 periodic concerns (intake,
expired-confirmation sweep, rejected-retention sweep, notification
digest) each now run in their own session with their own exception
isolation, logged to the visible activity log on failure — previously
one shared try/except meant an intake failure silently skipped the
other three for that entire tick.

**If 24/7 uptime independent of your own machine is ever needed**:
Oracle Cloud's Always Free tier includes a small ARM VM (Ampere A1, up
to 4 OCPU / 24GB RAM) that is genuinely free indefinitely, not a trial
— point `DATABASE_URL` at a file on that VM and run the same way as
local-first. Not built or scripted here; this is the documented
fallback path, not a default, since local-first covers the actual
current need.

**Backups**: since backup *restore* is deliberately not built (see
Phase 9 above), periodically use `/settings/backup/export` if running
this unattended for extended stretches, and keep the downloaded file
somewhere outside the machine running the app.
