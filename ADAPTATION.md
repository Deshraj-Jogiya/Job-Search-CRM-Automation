# The adaptive layer (Part B)

How Career Pilot tunes itself from its own real usage, and the hard
line between "safe to do automatically" and "needs a human to look at
the evidence first." See `app/services/adaptation_service.py` for the
actual code -- this file explains the *why* and the *guardrails*.

## Two tiers, deliberately not blurred

**Tier 1 (mechanical)** self-tunes continuously, applies immediately,
logs every change. It only ever touches a parameter where the feedback
is deterministic, arrives within one run, and ground truth is
unambiguous -- a human directly saying "that was wrong."

**Tier 2 (strategy)** is outcome-dependent: slow (weeks between
signal), sparse (a personal job search generates dozens of data
points, not thousands), and easy to confound with something else
entirely. It never applies itself. Every proposal sits in front of a
human on `/adaptation` until approved.

Nothing sits between the two. There's no "mostly automatic, occasional
review" middle tier -- a parameter is either safe to move on its own
because the feedback loop closes immediately and unambiguously, or it
isn't, in which case it waits for a person no matter how confident the
math looks.

## Tier 1: what self-tunes, and why each one qualifies

| # | Parameter | Feedback source | Why it's safe to auto-apply |
|---|---|---|---|
| b1.4 | Dedupe fuzzy-match threshold (`intake_service.py`) | User corrects a specific false merge/split | The correction IS the ground truth -- there's no ambiguity about which direction to nudge |
| b1.6 | Source polling order (`recommend_source_reorder`) | Ingested→applied funnel counts, already in the DB | Reordering, never disabling -- worst case is a slightly worse poll order, not lost coverage |
| b1.7 | Typical time-to-close per ATS | `last_seen_at - first_seen_at`, already in the DB | Warn-only input to `staleness_flag`, never a filter |
| b1.8 | Queue supply counts | Postings already in the DB | Pure counting, no judgment involved at all |
| b1.2 | ATS slug guess-order (`board_discovery.py`) | Which candidate form actually resolved | Reordering a guess sequence, never skipping a guess |
| b1.5 | Sponsorship regex misfire *tracking* | User flags "this flag was wrong" | The counting is automatic; the actual regex edit is a code change, so it can only ever be **proposed**, never applied by this app |
| b1.3 | Government-data column header mapping (`column_utils.py`) | A user edits `data/column_mappings.yaml` to correct a fuzzy-guessed header | The edit IS the ground truth for that exact (source, field) pair; a wrong guess a user hasn't corrected yet still requires their edit before anything changes |
| b1.1 | Resume PDF page-fit reductions (`document_render_service.py`) | Reportlab's own deterministic page/height measurement, same run | Only fires when the page genuinely overflows target; each reduction is reversible and floor-bounded, never silently drops content beyond what the catalog explicitly allows |

b1.5 is the one item in this list that looks like Tier 1 (continuous,
no approval to *accumulate* evidence) but never reaches "applied" --
see `adaptation_service.record_sponsorship_misfire`/
`sponsorship_misfire_report`. Narrowing a regex pattern is source code,
not a bounded float; there's no honest way to auto-apply that the way
a numeric threshold can be.

### b1.3 in detail: government-data column mapping

`app/ingest/column_utils.py`'s `resolve_column` (exact, normalized
alias matching) is unchanged and still tried first -- the common case,
since a header rarely drifts release to release. `resolve_column_learned`
wraps it with a fuzzy fallback for the USCIS/DOL LCA/OEWS loaders only:
on an exact-match miss, it fuzzy-matches (stdlib `difflib`, same
technique as b1.4) against the loader's own alias list PLUS every
column string ever confirmed correct for that exact (source, field)
pair, guessing whichever real column scores highest above
`column_mapping_fuzzy_threshold` (config, self-tunable within
[0, 1] the same way b1.4's dedupe threshold is).

Every guess is written to `data/column_mappings.yaml` -- gitignored,
per-deployment state, not shared config -- as `{column, guessed_column,
confidence}`. A user reviewing that file and editing `column` to the
real correct header (leaving `guessed_column` alone) is what b1.3
detects as a correction: the next load sees `column != guessed_column`,
logs it via `record_column_mapping_correction`, and reconciles the two
fields so the same correction isn't logged twice. Confirmed corrections
accumulate in `confirmed_column_names` and join the candidate pool for
every future fuzzy guess on that field -- a header a past release
already proved correct scores at least as well against a similarly-
worded new one as the generic alias list would, no artificial score
bonus needed.

No match above threshold, on a required field, is a loud
`ColumnResolutionError` naming the real columns found -- never a
silent guess below the confidence bar, per this module's own standing
rule from before b1.3 existed.

## Tier 2: the one generic engine

`adaptation_service.evaluate_comparison(db, comparison_type)` is the
single implementation behind all four named comparisons
(`resume_variant`, `source`, `tier`, `wage_level`) -- same evidence
gates, same confound check, same state machine, pointed at whichever
`metrics_service.py` segmentation function matches. Four separate
copies of this logic would drift; one function pointed at four inputs
can't.

### Evidence gates (config/adaptation.yaml, never hardcoded)

A comparison only leaves `NOT_REPORTABLE` once it clears **both**:
- a minimum combined sample size (`min_samples_resume_variant: 30`,
  `min_samples_source_comparison: 50`, `min_samples_tier_comparison: 40`,
  `min_samples_wage_level_comparison: 40`)
- a minimum total reply count (`min_replies_for_any_outcome_claim: 10`)

Below either gate, the raw segment counts still render -- labeled NOT
YET MEANINGFUL -- but there's no best/worst, no confidence interval,
no proposal. A personal job search doesn't generate enough volume for
even a 30-40 sample comparison to happen often; that's the honest
reality this gate is built around, not a formality.

### Confound detection (b3.5)

Clearing the sample-size gate isn't enough on its own. Before a
comparison is allowed to reach `REPORTABLE`, `_is_confounded` checks
whether the two leading segments differ by more than
`confound_share_diff_pct` (25 points, by default) in their composition
along **another** tracked dimension -- company tier for a
source/resume_variant/wage_level comparison, source for a tier
comparison (checking tier as a confound of a tier comparison would be
circular). If they do, the state becomes `CONFOUNDED` and the proposal
is withheld even though the raw numbers alone would have qualified.
This is a real, if blunt, check -- it catches "your best resume
variant also happens to be the one you send to cap-exempt companies"
before that gets mistaken for the variant itself being better.

### The proposal, when there is one

Only `tier` and `wage_level` comparisons produce a
`proposed_adjustment` -- they're the two with a natural *expected*
ordering (Tier A should outperform Tier C; wage level IV should
outperform I) to compare the observed winner against. `resume_variant`
and `source` comparisons stay informational: there's no honest single
numeric knob to auto-propose changing just because one resume variant
or one job source is currently winning.

When there is a proposal, it targets `scoring_service.py`'s
`sponsorship_history`/`wage_level_fit` component weights (see
`WEIGHTING.md`) -- previously hardcoded module constants, now
resolved live via `adaptation_service.get_current_value`, with a
config-declared floor/ceiling (`config/adaptation.yaml`'s
`scoring_weights`) neither approval can ever cross. If the observed
winner matches the assumed ranking, the proposal nudges the weight
*up* (more confidence in the signal); if it contradicts the assumed
ranking, the proposal nudges it *down* (the signal is less trustworthy
than assumed). Either way, `clamp_weight_change` caps a single
approval to `weight_change_clamp_pct` (20%) of the current value --
the same bound used everywhere else in this app that a Tier 2 proposal
can move a number.

### States, plainly

- **NOT_REPORTABLE** -- raw numbers only, explicitly labeled not yet
  meaningful. No winner, no ranking, no recommendation.
- **CONFOUNDED** -- crossed the evidence gate, but withheld because
  another dimension could explain the difference.
- **REPORTABLE** -- best/worst segment, a 95% confidence interval on
  the reply-rate difference, and (for tier/wage_level, only when the
  CI excludes zero) a bounded proposal with Approve/Reject/Snooze.
- **APPLIED** -- not a distinct enum value; an approved proposal is a
  normal `AdaptationLog` row with `status="applied"`, which means the
  existing generic `revert_adaptation` already gives it one-click
  revert for free. No separate "applied Tier 2 change" code path
  exists, on purpose.

Approving re-evaluates the comparison fresh first -- evidence can move
between when the page rendered and when the button was clicked -- and
refuses if it's no longer `REPORTABLE` with a live proposal. Rejecting
just logs the decision for the audit trail; it does not suppress the
same proposal from reappearing if the evidence still supports it next
time, since there's no spec'd cooldown period and inventing one felt
worse than the mild repetition.

**Snooze is not durable.** `AdaptationLog.status`'s own CHECK
constraint (set by migration `dedf7ee1248e`) only allows
`applied`/`proposed`/`approved`/`rejected`/`reverted` -- adding a sixth
value would mean a second schema migration in a pass whose spec
explicitly said "if this pass needs schema changes they go in ONE new
migration." Snooze is therefore a UI-only dismissal: the same
comparison re-evaluates fresh, from real current data, the next time
`/adaptation` loads. Documented here rather than silently faked as
persistent.

## B3: the anti-runaway guardrails

**b3.1 (exploration floor) and b3.2 (never narrows silently)** are
satisfied by construction, not a runtime sampling mechanism. Every
Tier 1 function in the table above either reorders a list the user
already sees in full, or only proposes a change for human review --
none of them remove a posting, application, or source from a
user-facing view. `queue_service.build_queue` itself never truncates
or filters by score (see its own module docstring); every non-
terminal, non-skipped, non-blocked application is always in its tab,
low scorers included. Score only controls order, never inclusion. That
makes a 15% exploration floor trivially true: 100% of the queue is
always visible regardless of what any adaptive ranking favors, so
there's nothing left for a floor to guarantee on top of. See
`tests/test_adaptation_guardrails.py`.

**b3.3 (full audit + revert)** is `AdaptationLog` plus
`revert_adaptation`/`revert_all_since`, used by both tiers uniformly --
there's no separate audit mechanism for Tier 1 vs Tier 2.

**b3.4 (cold start)** falls out of the design rather than needing
special-case code: `AdaptiveParameterValue` holds the CURRENT value of
anything either tier has tuned; a parameter with no row there is still
at its `config/adaptation.yaml` default. Zero rows in either adaptive
table means byte-identical behavior to before this pass existed --
tested directly in `tests/test_adaptation_guardrails.py::TestColdStart`
and `tests/test_scoring_service.py::test_cold_start_matches_the_module_default_exactly`.

**b3.5 (confound detection)** is covered above, under Tier 2.

## b1.1 in detail: the resume PDF page-fit loop

`page_fit_service.py` is the loop; `document_render_service.py`'s
`render_resume_pdf` was split into `build_resume_flow` (pure flow
construction) plus a thin wrapper, so the loop can measure a candidate
render without ever producing a throwaway multi-page PDF just to
answer "how much taller than one page is this" -- `_measure_flow_height_pt`
builds the identical flow against one arbitrarily tall reportlab frame
and reads back its real consumed height, a standard, exact technique
(verified empirically against real rendered output, not assumed from
reportlab's docs).

Two reduction kinds, both declared in `config/resume_rules.yaml`'s
`page_fit.reduction_catalog`: CONTENT reductions
(`drop_weakest_bullet_per_role`, `shorten_summary`, `drop_third_project`)
mutate a copy of the resume content before rendering; VISUAL reductions
(`tighten_line_spacing`, `shrink_margins_one_step`,
`shrink_body_font_one_step`) are render parameters, each floor-bounded
by config (`min_body_font_pt`, `min_margin_in`) so no amount of
"learning" can ever cross them. `drop_weakest_bullet_per_role` never
touches an entry's first bullet; nothing in the catalog ever touches
Education.

Every application of a reduction records a REAL measured lines-saved
delta (before/after height, converted via the current line-leading)
into a running average per reduction id (`page_fit_avg_lines_saved:*`
/ `page_fit_sample_count:*`, the same `AdaptiveParameterValue`
mechanism every other Tier 1 parameter uses) and logs an `AdaptationLog`
row. Cold start -- no reduction anywhere has a learned average yet --
falls back to the catalog's own literal YAML order, exactly as that
file's comment says; once any candidate has history, the loop picks by
lowest `content_value_weight / predicted_lines_saved` among reductions
that still have room to give.

If every reduction is exhausted (all at their floor, or already
single-shot-applied) and the resume still overflows `max_pages`,
`render_resume_pdf_with_fit` raises `PageFitExhaustedError` -- never
returns a silently-2-page PDF. The error carries the real overflow in
lines, the target, every reduction already tried, and the longest
source bullets ranked, so a human has an obvious next cut. Wired into
both real production paths that generate a downloadable/attachable
resume PDF: `/jobs/{id}/tailored/resume/download` and the real
autofill flow (`autofill_service.run_autofill`) -- both surface the
exhaustion message as a normal user-facing error rather than a 500 or
an unhandled exception.

While building this, `config/resume_rules.yaml`'s `min_body_font_pt`
was corrected from `9.0` to `8.0`: the value had been transcribed from
the spec without checking it against the PDF renderer's real,
already-shipped starting font (`8.7pt`, tuned over prior sessions of
real density work) -- a floor ABOVE the current value would have made
`shrink_body_font_one_step` a no-op from the very first render.

## Not in this pass

No new intake sources, no email-sending infra, no LinkedIn automation,
no per-round interview outcome tracking, no general prose-fabrication
detection beyond the two narrow checks (D1/D2) added alongside the
resume rules -- see `ARCHITECTURE.md`'s "Known gaps" section for why
that broader gap stays open by design. No UI framework change, no auth
changes.
