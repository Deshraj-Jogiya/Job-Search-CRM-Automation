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

## What's NOT built yet (by design — next phases)

- Direct-ATS job intake (Greenhouse/Lever/Ashby board discovery — Phase 2 slice 2)
- Matching/tailoring/scoring engine (multi-pass refinement, cover-letter scoring)
- Confirmation-gated auto-apply + fast-track logic + one-click approve UI
- Review-gated outreach with daily cap
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
