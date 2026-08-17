# Career Pilot — Project Context

Personal job-search CRM + automation platform. Built to also be published
later as a public showcase/fork-able template (see "Two-face product"
below) — but the primary, working instance is personal-use for Deshraj
Jogiya, a Data/AI-ML Engineer based in Tempe, AZ.

## Product vision (from design discussion, in priority order)

1. Personalized job search from the web — not LinkedIn-only; add legitimate
   free sources (Adzuna, direct Greenhouse/Lever/Ashby endpoints) since raw
   LinkedIn scraping is fragile and ToS-risky as a sole source.
2. Hands-free filtering with full user control (keywords, location, remote,
   salary, visa, seniority).
3. Resume tailoring + individually scored cover letters, tailored to each
   JD's actual language (not an abstract "market trend" model).
4. Auto-apply with a **human confirmation layer** (tunable window, default
   ~15h) before firing — not instant blind auto-submit. On timeout,
   auto-applies. Rejected applications retained briefly (tunable, default
   7 days) then hard-deleted.
5. Mailing/outreach automation — also review-gated, same confirmation
   pattern as #4. No blind auto-email to guessed addresses. Daily send cap.
6. Personalized interview prep — general (candidate background) + company-
   specific (JD + light company research).
7. Must run at **$0** — no paid hosting (Render was tried and cost money;
   ruled out). Local-first is the default answer; Oracle Cloud Always Free
   VM is the fallback if 24/7-independent-of-laptop uptime is needed later.

### Corrections/refinements made during design (important — don't regress)

- "Top 5 applications" was a **misunderstanding on my part** — the actual
  goal is being an **early applicant** (top-5-ish among total applicants
  on a listing), not capping application volume. Poll frequency (~5–15
  min, tunable) exists to minimize time-from-posting-to-application, not
  to gate volume. Freshness of the *source's own indexing* matters as much
  as poll frequency — prefer sources with low indexing lag (direct ATS
  endpoints tend to beat aggregators here).
- Scam/ghost-job/repost detection is a required intake-layer feature:
  flag reposts, unusually long-open listings, and scam-pattern JDs
  (payment requests, off-platform-only contact, no verifiable company
  presence). **Surface as a warning, never silently filter.**
- Every numeric setting discussed (poll interval, confirmation window,
  retention days, outreach cap, fast-track thresholds) is a **tunable
  config value, not a hardcoded constant** — this is already reflected in
  `GlobalSettings` in the current schema; keep it that way as features
  are added.
- LinkedIn profile auto-scraping was explicitly ruled out (too aggressively
  defended, risks the real LinkedIn account). Profile auto-update instead
  uses: portfolio-hosted `resume.json` as the source of truth (zero-risk,
  it's the user's own site) + user-provided LinkedIn export/paste text that
  gets AI-diffed into the profile with user approval, versioned.
- Confirmation UX must be **one-click approve** — everything needed to
  decide (score, tailored diff, scam flags) visible at a glance, Approve/
  Edit/Reject equal weight, reminder notification (email or Telegram/
  Discord) links straight into that same view. This "clean, easy,
  informative" bar applies to the whole product, not just this one screen.

### Two-face product (both from one codebase, config-driven — not two repos)

- **Personal instance**: real profile, real credentials, full automation,
  runs until the user is hired.
- **Public showcase**: demo profile, automation off by default, clear
  setup + ethical-use docs. Same code, different config/fixtures.

## Origin note

This replaces an earlier prototype repo (`Job-Search-CRM-Automation`) that
had grown organically past its own README and had real issues: hardcoded
secrets, plaintext credential storage, insecure default admin password,
LinkedIn/Google scraping as a sole strategy, blind auto-email to guessed
addresses, a dead "Rejected" Kanban column (jobs were hard-deleted instead
of retained), and no confirmation layer before real-world side effects
(submitting applications, sending emails). We are **not** porting that
code forward — this is a from-scratch rebuild informed by those lessons,
not a patch of the old repo. Don't reintroduce those patterns.

## Architecture (current, as of last hand-off from chat)

- FastAPI + SQLAlchemy + SQLite (Postgres-compatible via `DATABASE_URL`)
- LLM provider abstraction in `app/services/llm/` — `LLM_PROVIDER` env var
  selects `anthropic` (Claude, this instance's default), `openai_compatible`
  (OpenAI or Gemini's free-tier OpenAI-compat endpoint, for forkers without
  Claude access), or `ollama` (fully local/free). All AI-calling services
  must go through `get_llm_provider()` — never import a provider SDK
  directly in a feature file.
- CSRF protection (`app/csrf.py`) and fail-closed admin auth are baseline,
  not optional — every new POST route needs the CSRF token in its form.
- `app/models.py` already has the full schema for every planned phase
  (profile versioning/variants, company memory, job postings with
  repost/scam flags, confirmation-queue fields, outreach with daily-cap
  tracking, interview prep). Extend behavior against this schema; avoid
  ad-hoc migrations if the field already exists.

## Build phases (roughly in order; check off as completed)

- [x] **Phase 0 (foundation)**: DB, models, LLM abstraction, CSRF, kill
      switch, live-editable settings, base dashboard shell.
- [x] **Phase 1**: Living profile — portfolio `resume.json` sync, LinkedIn
      paste-diff with approval, profile variants. (Portfolio sync is
      built against a configurable `PORTFOLIO_RESUME_URL` but the
      portfolio site doesn't serve that endpoint yet, so it fails
      gracefully with a clear error until that's added there; manual
      JSON seed and LinkedIn paste-diff are the working paths meanwhile.)
- [ ] **Phase 2**: Multi-source job intake — LinkedIn + Adzuna + direct
      Greenhouse/Lever/Ashby, tiered polling (cheap check vs. full
      ingest), fuzzy dedup, scam/repost/staleness flagging, company memory.
- [ ] **Phase 3**: Matching/tailoring/scoring — wire in the multi-pass
      refine-and-verify tailoring loop, independent cover-letter scoring,
      real recomputed post-tailor score (no placeholder/random score).
- [ ] **Phase 4**: Confirmation-gated auto-apply — `Pending Confirmation`
      status, tunable countdown with fast-track override for very-fresh/
      very-high-match jobs, one-click approve UI, notifications, retention
      sweep for Rejected jobs.
- [ ] **Phase 5**: Outreach automation — same confirmation pattern, daily
      cap, email verification before ever offering "send".
- [ ] **Phase 6**: Interview prep generation.
- [ ] **Phase 7**: Outcome analytics — surface what's already being
      logged (status transitions, email classifications) as real
      keyword/source/score-band conversion insight.
- [ ] **Phase 8**: Two-face packaging (personal vs. showcase config).
- [ ] **Phase 9**: Data safety — encrypted local DB backup/export.
- [ ] **Phase 10**: $0 deployment hardening (local-first; Oracle Always
      Free VM path documented as fallback).
- [ ] **Phase 11**: Design/polish pass — this is deliberately LAST. Don't
      over-invest in UI before the backend phases are solid; the "magic"
      layer (glassmorphism, live dashboards — see the user's portfolio at
      https://deshraj-jogiya.github.io for the target aesthetic) is its
      own dedicated pass once functionality is proven.

## Known open risks (raised during design, keep in view)

- Free-tier API budgets (Adzuna ~1,000 calls/month) don't trivially
  support 5–15 min polling — the cheap-check-vs-expensive-ingest split
  has to actually be cheap, or the quota blows in days. Budget this
  concretely before wiring up Phase 2's polling loop.
- SQLite + multiple concurrent background writers (crawl, tailor,
  autofill, outreach, email-scan) risks "database is locked" — consider
  serializing writes or a lightweight queue before Phase 2/4 land
  simultaneously.
- Automated form submission is against many ATS platforms' ToS even with
  a human confirmation gate reducing (not eliminating) that exposure.
- The public showcase reflects on the author if someone runs it
  irresponsibly — ship it with automation off by default and clear
  ethical-use docs (Phase 8).

## Working conventions

- Every tunable number is a `GlobalSettings` field, live-editable from the
  dashboard — never a hardcoded constant.
- Scam/repost flags and any automated warning are informational, never a
  silent filter — the user stays in control of the call.
- No blind side effects: applications and outreach both go through the
  confirmation queue; nothing auto-fires without either explicit approval
  or an expired, visible countdown.
