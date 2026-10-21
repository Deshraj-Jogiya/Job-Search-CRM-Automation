# 0003: Free mechanical scoring runs before any paid LLM call, under a daily spend cap

**Status:** Accepted

## Context
An early rollout of automatic scoring and tailoring cost several times the estimate, because every ingested posting received a paid
LLM match score, including postings that could never reach the tailoring threshold.

## Options
1. Score every posting with the LLM.
2. Score with rules only.
3. Run the free rule-based components first (role and title match, recency, location, sponsorship signals, company tier) and spend
   on the LLM component only when the posting can still clear the threshold, with a hard daily cap checked before every paid call.

## Decision
Option 3. The LLM match score is one 30-point component among rule-based ones (weights in `WEIGHTING.md`), and it is computed only
when it can still change the outcome. `daily_llm_spend_cap_usd` is checked at every metered call site, and prompt caching is used for the shared profile
block.

## Consequences
- Most postings are filtered for free; spending scales with promising postings, not with intake volume.
- When the cap is reached, work queues instead of failing silently, and the dashboard shows the spend.
