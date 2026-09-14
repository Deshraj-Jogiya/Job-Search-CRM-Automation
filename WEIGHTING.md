# Match score weighting (Phase 3b)

How `JobApplication.score_breakdown`/`total` is computed, and how it
maps onto the original spec's weights. See `app/services/scoring_service.py`
for the actual code -- this file explains the *why*, not the *how*.

## What "the existing scorer" actually was

The spec asked to "rebalance existing components so the total still
normalizes to 0-100," which assumes there were already multiple named,
weighted sub-scores to rebalance. There weren't. Before this phase,
`match_score` was a single opaque number an LLM returned directly
(`matching_service.evaluate_match()`) -- one holistic judgment, no
decomposition, no separately-stored components at all.

So "rebalancing" here means: that whole prior 0-100 judgment is folded
in as ONE new component, **AI Profile Fit**, scaled down from its old
0-100 range to a 30-point contribution
(`round(match_score / 100 * 30)`). It is not renamed or replaced --
`match_score` and `match_analysis_json` are untouched, still exactly
what they were. `score_breakdown` is new and additive.

## The seven components

| Component | Weight | Source |
|---|---|---|
| AI Profile Fit | 30 | folded-in `match_score` (see above) |
| Sponsorship History | 30 | `Company.tier` (A=30, B=18, C=8, X=0, cap-exempt=30 floor, unknown=4) |
| Wage-Level Fit | 20 | `Company.max_wage_level_15xx` (IV=20, III=15, II=8, I=3, unknown=6) |
| Role/Title Match | 12 | `JobPosting.job_title` vs. `SearchKeyword`/`SeniorityExclusion` (match=12, no keywords configured=6, no match among configured keywords=4, excluded seniority term hit=0) |
| Worksite Clarity | 6 | `JobPosting.location` + `worksite_ambiguous` (specific place=6, bare "Remote"/unset=4, flagged ambiguous=0) |
| Posting Recency | 2 | `JobPosting.first_seen_at` vs. `GlobalSettings.stale_posting_threshold_days` (fresh=2, recent=1, aging=1, stale or `staleness_flag`=0) |
| Sponsorship Signal | +10 bonus | `JobPosting.sponsorship_signal` |

Base components (AI Profile Fit + Sponsorship History + Wage-Level Fit
+ Role/Title Match + Worksite Clarity + Posting Recency) sum to a
100-point subtotal; the Sponsorship Signal bonus is a genuine +10 on
top, clamped at 100 total (`min(100, sum(...))`). This is a literal
reading of the spec's own notation, which marked sponsorship_signal
with a leading "+" distinct from the other flat weights.

## Two components added after a real audit (2026-09-14)

The user asked for a point-by-point audit of this file/module against
the original spec, sharply, after a prior resume-market-research
mistake in the same session where unresearched claims got presented as
fact. The audit found two real gaps -- not folded into an existing
component, not a design choice, genuinely absent:

- **Role/Title Match** didn't exist as its own line item. `AI Profile
  Fit`'s LLM judgment may informally weigh title relevance as part of
  its holistic call, but there was no separate, explainable,
  free/mechanical signal the way sponsorship/wage/worksite each have
  their own -- and several real sources (every direct-ATS board:
  Greenhouse/Lever/Ashby/etc, plus all 4 remote-job boards) pull EVERY
  posting from a company/board with no title filter applied at intake
  at all. `SeniorityExclusion` in particular was created and stored
  but never actually enforced as a hard filter on those sources --
  this component is the first place it's mechanically checked against
  a posting's real title. Built on the same profile-derived
  `SearchKeyword`/`SeniorityExclusion` tables `intake_service.py`
  already uses to build search queries, rather than a new data source.
- **Posting Recency** didn't exist anywhere in the scoring pipeline at
  all. Built on `GlobalSettings.stale_posting_threshold_days`, the
  same already-user-configurable cutoff `staleness_flag` uses, rather
  than a second hardcoded day count.

Weight for both came out of Worksite Clarity's own budget (10 -> 6),
not the three heaviest/best-evidenced components (AI Profile Fit,
Sponsorship History, Wage-Level Fit), which stayed untouched -- both
because the user's own explicit ordering placed Role/Title Match above
Worksite Clarity and Posting Recency below it, and because Worksite
Clarity's own docstring already called it "a heuristic, not precise
geolocation," the least evidence-strong of the pre-existing
components. Neither new component was wired into the Tier 2 adaptive
weight-tuning system (`adaptation_service.py`'s
`_COMPARISON_WEIGHT_PARAMETER`) -- that system compares reply-rate
segments (tier, wage level) that don't exist for title-match/recency,
so making them "adaptive" without that real evidence backing would be
fake precision, not a real safeguard. They're static module constants,
same category as Worksite Clarity and Sponsorship Signal.

Also confirmed correct during this audit, not a gap: absence of a
sponsorship tag/signal in a JD is never treated as a negative --
`sponsorship_blocked` (the hard gate) only fires on explicit negative
language (`sponsorship_signals.py`'s `_NEGATIVE_PATTERNS` -- "no visa
sponsorship," "US citizens only," etc.), never on a JD that simply
says nothing about sponsorship either way. A posting with no
sponsorship language at all stays fully visible and scored; it just
doesn't earn the Sponsorship Signal bonus.

## Two things the spec assumed that don't exist in this schema

1. **`wage_level_fit` was originally company-level only.** The spec's
   own description ("company max pw_wage_level for SOC 15-*") is
   `Company.max_wage_level_15xx`, the highest DOL-filed prevailing-wage
   level this employer has ever used for a Computer/Mathematical
   (SOC 15-*) role, tracked by `ingest/sponsors.py`'s
   `load_dol_lca_data()` -- still the fallback signal. A real per-
   posting signal was added later (2026-09-11, see FUTURE.md): when
   `salary_parser.py` finds an unambiguous salary in the JD (or a user
   manually enters/corrects one) and the posting's location matches a
   loaded OEWS area, `wage_level_service.py` classifies that specific
   offer via Phase 1's `wage_level_for()` helper, and
   `_wage_level_fit_component` prefers it over the company-level
   figure. Most JDs still don't state a salary, so the company-level
   fallback remains the common case in practice.

2. **"Filings across FY24-26" isn't stored as a per-year series.**
   `Company.lca_filings_total` and `h1b_approvals_total` are both
   running cumulative totals across whatever source files have been
   loaded (see `ingest/sponsors.py`), not broken down by fiscal year.
   `company_tier.py`'s tier thresholds use the combined total
   (LCA filings + USCIS approvals) as "filings" -- loading only
   FY24-26 source files is how the FY window gets honored in practice,
   not a stored filter. Documented in `company_tier.py`'s own
   docstring too.

## Worksite Clarity is a heuristic, not geolocation

No exact "is this a specific US metro" algorithm was specified. The
actual rule: `worksite_ambiguous` (regex-flagged, see
`sponsorship_signals.py`) always scores 0; otherwise a location string
containing a comma (`"Austin, TX"`-shaped) scores 10; a bare `"Remote"`
or an empty/missing location scores 6. This is a real approximation
over an un-geocoded source string, not a claim of precision.

## Tier thresholds are config-driven

`GlobalSettings.tier_a_min_filings` / `tier_a_min_wage_level` /
`tier_b_min_filings` -- live-editable, not hardcoded in
`company_tier.py`. Shipped defaults: A needs >=10 combined filings AND
wage level >= III; B needs >=3 filings; C is 1-2 filings or cap-exempt
with none; X is zero filings and not cap-exempt.

## The hard gate is separate from the score

`sponsorship_blocked` postings are excluded from `/queue`'s default
view entirely (`queue_service.py`) -- their score is still computed
and stored for audit, it's just never shown in an active tab. Nothing
about a blocked posting is deleted.
