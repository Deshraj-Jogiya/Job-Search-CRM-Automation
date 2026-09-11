# Deferred / out of scope

Things explicitly not built in the Phase 3/4 (H-1B Targeting Upgrade)
pass, per the scope freeze in that spec ("no new job sources, no auth
changes, no new interview-prep features, no UI framework changes, no
Docker/K8s... if you think something is missing, append to FUTURE.md
and move on"). Not a roadmap commitment -- just a record of what was
consciously deferred and why, so a future session doesn't have to
re-derive it.

## Phase 2b -- DONE (2026-09-10)

RemoteOK, Remotive, WeWorkRemotely, Jobspresso -- all 4 built, real
endpoints verified live (see each source module's own docstring for
the exact terms/constraints found). All 4 tagged `prod_only` in
`source_visibility.py` deliberately -- not because their terms flatly
forbid a personal tracking tool, but because Remotive's specifically
warns against displaying its jobs "to collect signups... to show a
listing," which a public demo instance sits too close to for a
default "yes." Revisit only with a deliberate, separate review if the
demo instance ever wants these too.

## Per-round interview outcome tracking

The weekly funnel (`metrics_service.weekly_funnel`) only has
Sent -> Replied -> Interviewing (first round reached) -> Offer. The
original ask included a "final round" stage; this schema has no
signal for which round was reached beyond the single
`interviewing_at` timestamp, and inventing one from data that doesn't
exist would be dishonest. A real fix needs new per-round self-report
tracking (which round, reached when) -- a real feature, not a
one-line addition, deliberately not built as a side effect of the
metrics dashboard.

## Bullet-level fabrication detection -- DONE (2026-09-11)

`tailoring_service._verify_structural_fidelity()` catches an LLM
inventing/altering a company name, role title, or date range. The
Adaptive Layer + Resume Rules pass (2026-09-11) added two narrow,
non-LLM checks: `resume_rules.check_years_claim` (D1) rejects a
generated years-of-experience figure exceeding what
`total_experience_months` actually computes, and
`check_unverified_bare_percentage` (D2) rejects a bare percentage not
marked `verified: true`. Both run against the raw tailored bullets
BEFORE C4's hedging mutates them, so a real violation survives for
human review even though the saved document always renders safely
hedged regardless. That pass deliberately scoped out general prose
fabrication detection (its own Part D: "Do NOT attempt general prose
fabrication detection").

The general gap those left open -- a fabricated metric/outcome/
organization name embedded directly in bullet prose, not just a bare
percentage -- is now closed by `tailoring_service.check_bullet_fabrication`,
a dedicated LLM verification pass added the same day once the pass
above finished: for each tailored bullet, compares it against its
ORIGINAL counterpart (valid only when `_verify_structural_fidelity`
already confirmed entry order/company/role/date didn't change) and
flags a genuinely new claim -- metric, scope, outcome, responsibility,
or a company/organization/client/tool name -- not supported by the
original. Run ONCE per tailoring against the final tailored bullets,
not woven into every intermediate refine pass, to keep the added LLM
cost to exactly one extra call. Surfaced through the same
`attention_reason` mechanism as every other check, never
auto-rewritten. This is a real LLM judgment call, not a mechanical
guarantee the way `_verify_structural_fidelity` is -- see
`tests/test_tailoring_adversarial.py`'s findings #2/#3 for the honest
scope, and `tests/test_bullet_fabrication_check.py` for the wiring
tests (mocked LLM -- verifying the LLM's own judgment quality isn't
something an offline unit test can do).

## Wage-level fit is company-level, not per-posting

`Company.max_wage_level_15xx` (the wage_level_fit score component)
reflects an employer's historical DOL-filed wage level for
Computer/Mathematical roles, not the specific offered salary for a
given posting -- this app has no per-posting offered-salary field to
compare against Phase 1's `wage_level_for()` helper. Adding one would
mean parsing salary ranges out of raw JD text (unreliable, most JDs
don't state one) or a manual entry field -- neither attempted here.

## Everything else the H-1B spec's own scope freeze already named

No user accounts/multi-user, no email integration/job-alert emailer,
no browser extension/Chrome plugin, no LLM-based matching, no data
sources beyond what Phases 1/2a/(frozen 2b) already name. See the
original spec conversation for the full list -- not re-derived here.
