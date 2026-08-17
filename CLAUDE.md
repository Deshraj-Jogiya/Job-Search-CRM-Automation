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
- Phase 4's auto-apply timing was **clarified through discussion, not a
  wrong assumption to fix** — the open question was how to stay fast for
  good matches without either (a) missing them during extended
  unreachable periods (sleep, 7+ hours — common to any job seeker, not
  unique to this user) or (b) letting anything risky fire unattended.
  Settled shape: every application still gets a confirmation window +
  notification, including fast-track ones ("fast" = a short tunable
  window, e.g. minutes, never a literal zero-delay auto-submit); whether
  a timeout is allowed to auto-proceed is gated by **flag severity, not
  just score/freshness** — a hard-stop flag (tailoring fabrication
  warning, scam-pattern match) never auto-proceeds no matter how long it
  waits, while clean/unflagged ones (including lower-stakes staleness/
  repost) do proceed on timeout if untouched; and confirmation deadlines
  should be **quiet-hours-aware** (a configurable daily unreachable
  window) so a deadline never silently lapses while the user is asleep.
  See Phase 4 below for the full settled design.

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
      (Slice 1 done: LinkedIn + Adzuna, dedup, repost/scam/staleness
      flagging, company memory via `Company` rows, keyword management,
      kill-switch-gated scheduler. Slice 2 candidates, not yet built:
      direct Greenhouse/Lever/Ashby board discovery -- needs a target-
      company strategy, most naturally built once slice 1's `Company`
      table has real data to auto-detect board slugs against; and/or a
      JobRight public-repo source (see note below) -- lower effort, no
      target-company problem to solve first.)
      **JobRight research (2026-08-17, user has a paid Turbo
      subscription)**: no public API found for JobRight's paid auto-
      apply/matching -- it's UI/extension-only, so it stays a parallel,
      independent tool rather than a technical integration with this
      one. Did find a concretely useful free source though: JobRight
      publishes public GitHub repos (e.g. `jobright-ai/Daily-H1B-Jobs-
      In-Tech`, role-specific new-grad/internship lists) with daily-
      updated markdown tables of listings -- no login, no API key, no
      subscription needed by anyone using this project. Worth building
      as an intake source (`is_configured()` always `True`, same as
      LinkedIn) since it needs zero credentials from any user, personal
      or showcase-fork. One caveat: its "apply" links route through
      jobright.ai rather than the employer's own ATS page.
- [x] **Phase 3**: Matching/tailoring/scoring — wire in the multi-pass
      refine-and-verify tailoring loop, independent cover-letter scoring,
      real recomputed post-tailor score (no placeholder/random score).
      Scoring/tailoring are on-demand (per-application button), not
      automatic on ingest -- real LLM cost per call. **Important finding
      from live testing**: the tailoring LLM will fabricate skills/tech
      it injects to hit the target ATS score, even when explicitly told
      not to -- confirmed with Gemini flash-lite claiming Docker/
      Kubernetes/Spark experience that appeared nowhere in the seed
      profile, in both the resume bullets and the cover letter. Added a
      mechanical (non-LLM) post-check that catches this and surfaces an
      `attention_reason` warning rather than silently shipping a
      fabricated document -- keep this check if this code is touched
      again, and treat any future tailoring-prompt change as needing
      the same live scrutiny, not just a "does it return JSON" check.
- [x] **Phase 4**: Confirmation-gated auto-apply — `Pending Confirmation`
      status, tunable countdown with fast-track override for very-fresh/
      very-high-match jobs, one-click approve UI, notifications, retention
      sweep for Rejected jobs. **Design settled and built 2026-08-17**
      (reasoning in Corrections/refinements above; implementation in
      `app/services/confirmation_service.py`,
      `app/services/notification_service.py`,
      `app/services/confirmation_tokens.py`, `app/routers/confirmation.py`):
      - Every application gets a confirmation window + notification, no
        exceptions -- "fast-track" means a short tunable window (minutes,
        via the existing `fast_track_window_hours`/`fast_track_score_
        threshold`/`fast_track_freshness_minutes` settings), never a
        literal zero-delay auto-submit with no checkpoint at all.
      - Whether a timeout is allowed to auto-proceed is gated by **flag
        severity**, independent of score/freshness: a hard-stop flag
        (Phase 3's tailoring-fabrication warning, or a scam-pattern match
        from Phase 2 intake) means the application waits for an explicit
        approve click no matter how long that takes -- hours, a day,
        whatever. Clean/unflagged applications (staleness/repost alone
        don't count as hard-stop -- lower stakes) auto-proceed if the
        window elapses untouched, but can still be approved/rejected
        early if the user acts first.
      - Confirmation deadlines should be **quiet-hours-aware**: a
        configurable daily unreachable window (e.g. sleep) pushes any
        deadline that would fall inside it to after the window ends,
        instead of silently lapsing while the user has no way to respond.
        This is generically useful, not personal to one schedule --
        keep it a per-installation setting for the eventual public
        showcase/fork too.
      - "Auto-proceed on timeout" means flipping to a ready-to-submit
        state with tailored docs on hand -- there is still no real
        portal-submission engine (the old prototype's Playwright auto-
        fill/submit was deliberately dropped in the rebuild). Building
        real submission automation is an explicitly separate, later
        decision given genuine ToS exposure -- do not fold it into this
        phase's scope by default; ask before building it.
      - Notification-driven one-click approval (email first, since SMTP
        config already exists; Telegram/Discord possible later) is what
        actually makes "fast" achievable without removing the human --
        it solves "I can't check the dashboard right now" by pushing the
        decision to the user wherever they are, instead of requiring
        them to remember to go look.
      - **Revised and built 2026-08-17 -- notification volume fix**:
        the first build sent one individual email per application with
        no batching at all. Caught via a direct question: if a bulk
        action (or, later, an auto-tailor pass) queues many
        applications in one go -- 75 was the example -- that's 75
        emails, a disaster, not a feature. Fixed:
        - The **bulk review page** (`/jobs/review`, `app/routers/jobs.py`
          + `app/templates/review.html`) is the primary way to process
          volume -- table of everything in `Pending Confirmation` +
          `Needs Review`, checkboxes, bulk Approve Selected / Reject
          Selected, per-row link into the detail page.
        - Individual one-click emails (`notification_service.
          send_confirmation_notification`) are reserved for **fast-track
          only** (rare by design -- very-high-score + very-fresh).
        - Everything else gets **no per-item email** -- `notification_
          service.send_digest()`, run from the scheduler tick and
          gated by a tunable `notification_digest_interval_minutes`,
          batches every application with `notification_sent=False`
          into one email pointing at the bulk review page.
        - Safety nuance, built in: `Pending Confirmation` and `Needs
          Review` rows live in structurally separate `<form>`s/tables
          on the review page, each with its own "select all" -- a bulk
          action in one section can never touch a flagged item in the
          other, so the confirmation queue's core safety mechanism
          can't be bypassed by habit.
      - Caveat worth remembering: scam/fabrication detection is
        heuristic, not a guarantee -- "clean/unflagged" means the
        specific checks didn't catch anything, not that nothing could
        possibly be wrong. The auto-proceed path inherits whatever blind
        spots those checks have.
      - "Applied" is reserved for a genuine human confirmation that they
        actually submitted somewhere (`Mark as Applied`) -- approval or
        timeout only ever produces "Approved" (ready, not submitted).
        Don't repurpose "Applied" to mean "the system stopped waiting";
        that would misrepresent real-world state the user might rely on.
      - **Lesson from building this**: calling `evaluate_and_enqueue()`
        with real SMTP credentials configured sends a real email --
        caught this only after it had already fired twice during
        verification (harmless test content, but still an unintended
        real side effect). If touching this code with live SMTP config
        present, seed/test data in ways that don't reach the
        notification call, or ask before triggering it.
      - **Bug found and fixed during verification**: `reject_application()`
        originally had no status guard at all (unlike `approve_application`),
        so it would "reject" an already-Applied application -- nonsensical,
        since you can't un-submit something real. Now blocks rejecting
        `Applied` or already-`Rejected` applications with a clear error.
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
