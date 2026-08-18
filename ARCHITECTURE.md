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
| Base template/styling | `app/templates/dashboard.html`, `app/static/css/style.css` | Functional, not polished — Phase "magic pass" is later, deliberately |
| Living profile (Phase 1) | `app/services/profile_service.py`, `app/routers/profile.py`, `app/templates/profile.html` | Done — profile variants (named flavors), portfolio `resume.json` sync (activates immediately, zero-risk per CLAUDE.md), LinkedIn paste-diff via LLM (creates a **pending** version, requires explicit approve/reject — never auto-activates), manual JSON seed/edit as the bootstrap path since no starter profile data ships with the rebuild. Note: the portfolio site doesn't serve `resume.json` yet, so "Sync from Portfolio" currently fails gracefully with a clear error until that endpoint exists there — manual seed / LinkedIn import are the working paths meanwhile. |
| Job intake (Phase 2, slice 1) | `app/services/sources/` (linkedin, adzuna), `app/services/intake_service.py`, `app/services/scheduler.py`, `app/routers/jobs.py`, `app/templates/jobs.html` | LinkedIn (no credentials needed) and Adzuna (skips gracefully without `ADZUNA_APP_ID`/`ADZUNA_APP_KEY`, hard-capped against its free-tier monthly budget) are live: fuzzy dedup (exact by source+external_id/url, fuzzy by normalized company+title), repost detection, keyword-based scam-pattern flagging, staleness flagging (all warn-only, never filter, per CLAUDE.md), search keyword management UI, per-source status panel, manual "run now" trigger, all gated by the `automation_enabled` kill switch. Direct Greenhouse/Lever/Ashby board discovery is **not** built yet — deferred to slice 2 since it needs a target-company strategy (most naturally: auto-detect board slugs for companies already seen via LinkedIn/Adzuna, once there's a real company list to work from). |
| Matching/tailoring/scoring (Phase 3) | `app/services/matching_service.py`, `app/services/tailoring_service.py`, `app/routers/jobs.py` (detail/score/tailor routes), `app/templates/application_detail.html` | On-demand (not automatic on ingest — real LLM cost) match scoring against the active profile, a genuine tailor→verify→refine loop for experience bullets (real recomputed final score from the last verify pass, never a placeholder — unlike the deleted prototype), tailored summary/skills/project-selection, independent cover-letter generation + scoring. **Includes a mechanical post-tailoring fabrication check**: verified during this build that the LLM (Gemini flash-lite, at least) will inject ATS keywords into experience bullets and the cover letter that have zero support anywhere in the candidate's real profile, despite explicit anti-fabrication instructions in the prompt. The check compares every keyword the refine loop claims to have "resolved" against the full original profile text; any with no match surfaces a clear `attention_reason` warning on the application (visible on both the jobs list and detail page) rather than silently shipping a fabricated resume. This is a real, demonstrated risk in this feature, not theoretical — worth keeping if this logic is ever touched. |
| Confirmation-gated queue (Phase 4) | `app/services/confirmation_service.py`, `app/services/notification_service.py`, `app/services/confirmation_tokens.py`, `app/routers/confirmation.py`, `app/templates/confirm.html` | A tailored application routes to **Needs Review** (no timeout, ever) if Phase 2/3 flagged it (scam pattern or tailoring fabrication warning), otherwise to **Pending Confirmation** with a quiet-hours-aware deadline (fast-track window if high score + fresh, standard window otherwise). On explicit approval or an unattended timeout, status becomes **Approved** — tailored docs ready, but there is still no real portal-submission engine, so this is honestly "ready to submit," not "submitted." A separate explicit **Mark as Applied** action records that the human actually did it. Rejected applications are hard-deleted after `rejected_retention_days` (their `JobPosting` is kept for company memory/repost detection). All of it gated by the `automation_enabled` kill switch, including the scheduler sweeps. **Notification volume fix (2026-08-17, see CLAUDE.md)**: the initial build emailed one-click links per application with no batching — a bulk review page (`/jobs/review`, table + multi-select + bulk approve/reject, structurally separated Pending Confirmation vs. flagged Needs Review sections) is now the primary way to process volume; individual emails are reserved for fast-track only, everything else batches into a single periodic digest pointing at the review page. |

| Outreach automation (Phase 5) | `app/services/outreach_service.py`, `app/services/contact_discovery_service.py`, `app/services/email_utils.py`, `app/routers/outreach.py`, outreach section in `app/templates/application_detail.html` | Draft → Approved → Sent, with **no timers or auto-send anywhere** (unlike Phase 4 — a real email to a real external person is immediately irreversible, so every state change is a live explicit click). Email channel sends via the same daily-cap-enforced, syntax+MX-verified path; LinkedIn channels are drafted but never auto-sent (LinkedIn automation was ruled out for account-risk reasons elsewhere in this project) — the user copies the note and confirms via a separate Mark as Sent. Optional contact discovery (Tavily web search + Hunter.io domain-search, both free-tier/no-card/graceful-degrade) surfaces candidate name/LinkedIn/email-with-confidence as labeled, unverified suggestions — never auto-filled, never algorithmically guessed — that still flow through the same manual draft/approve/send pipeline once picked. See CLAUDE.md's Phase 5 section for the mid-build design correction that added discovery. |

## What's NOT built yet (by design — next phases)

- Direct-ATS job intake (Greenhouse/Lever/Ashby board discovery — Phase 2 slice 2)
- Real portal-submission automation (Playwright-style auto-fill/submit) — explicitly out of Phase 4's scope; a separate, later, ToS-sensitive decision, not assumed by default
- Interview prep generation
- Outcome analytics
- Two-face packaging (personal vs. public showcase config)
- Final design/polish pass

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
