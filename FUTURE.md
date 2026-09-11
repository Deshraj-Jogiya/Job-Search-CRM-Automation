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

## Bullet-level fabrication detection

`tailoring_service._verify_structural_fidelity()` (added this pass)
catches an LLM inventing/altering a company name, role title, or date
range. It does NOT catch a fabricated metric or outcome embedded
directly in bullet prose (e.g. an invented "40% latency reduction"),
or an invented employer name mentioned only inside a bullet's text
rather than the structural `company` field. See
`tests/test_tailoring_adversarial.py`'s own findings section for the
exact adversarial cases that currently pass through undetected.
Building a real detector here means distinguishing "genuinely
rephrased from a real bullet" from "invented from nothing," which is
a harder problem than a quick mechanical patch can solve honestly --
worth a dedicated design pass, not a rushed fix.

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
