"""Intake targeting: the keyword rotation that keeps a per-keyword-call
source (Adzuna, LinkedIn) from re-querying the same handful of keywords
every cycle, or blowing a rate-limited budget by querying all of them
at once. Also covers the mechanical eligibility-flag detector -- the
free, JD-text-based check for hard compliance/eligibility requirements
(citizenship, clearance, HIPAA) that confirmation_service.has_hard_stop_flag
treats as a hard-stop, independent of the LLM-based fabrication check --
and the daily budget-pacing math for Adzuna's hard monthly cap.
"""

from datetime import timedelta
from types import SimpleNamespace

from app.database import utcnow
from app.services.intake_service import _adzuna_daily_budget, _detect_eligibility_flags, _quota_cost, _rotating_keyword_subset


def test_rotation_returns_full_list_unchanged_when_shorter_than_the_cap():
    subset, next_offset = _rotating_keyword_subset(["A", "B", "C"], offset=0, count=5)
    assert subset == ["A", "B", "C"]
    assert next_offset == 0


def test_rotation_returns_a_bounded_subset_and_advances_the_offset():
    keywords = [f"kw{i}" for i in range(10)]
    subset, next_offset = _rotating_keyword_subset(keywords, offset=0, count=3)
    assert subset == ["kw0", "kw1", "kw2"]
    assert next_offset == 3


def test_rotation_wraps_around_the_end_of_the_list():
    keywords = [f"kw{i}" for i in range(10)]
    subset, next_offset = _rotating_keyword_subset(keywords, offset=9, count=3)
    assert subset == ["kw9", "kw0", "kw1"]
    assert next_offset == 2


def test_rotation_eventually_covers_every_keyword_over_successive_cycles():
    keywords = [f"kw{i}" for i in range(11)]  # not evenly divisible by the cap, a real edge case
    offset = 0
    seen = set()
    for _ in range(len(keywords)):  # enough cycles to guarantee full coverage regardless of remainder
        subset, offset = _rotating_keyword_subset(keywords, offset, count=3)
        seen.update(subset)
    assert seen == set(keywords)


def test_rotation_handles_an_empty_keyword_list():
    subset, next_offset = _rotating_keyword_subset([], offset=0, count=5)
    assert subset == []
    assert next_offset == 0


def test_eligibility_flag_detects_citizenship_requirement():
    jd = "Must be a U.S. Citizen to be considered for this role."
    assert "citizenship" in _detect_eligibility_flags(jd)


def test_eligibility_flag_detects_security_clearance():
    jd = "Candidate must hold an active security clearance at the Secret level."
    assert "clearance" in _detect_eligibility_flags(jd)


def test_eligibility_flag_detects_hipaa():
    jd = "You will work directly with HIPAA-regulated clinical data."
    assert "HIPAA" in _detect_eligibility_flags(jd)


def test_eligibility_flag_returns_none_for_a_clean_jd():
    jd = "We build data pipelines with Python and SQL. Remote-friendly, US-based team."
    assert _detect_eligibility_flags(jd) is None


def test_eligibility_flag_returns_none_for_empty_jd():
    assert _detect_eligibility_flags("") is None
    assert _detect_eligibility_flags(None) is None


def _fake_source(calls_used_this_period, days_left):
    now = utcnow()
    return SimpleNamespace(
        calls_used_this_period=calls_used_this_period,
        period_reset_at=now + timedelta(days=days_left),
    ), now


def test_daily_budget_spreads_a_fresh_period_evenly_across_the_month():
    # Real project defaults: 900/month budget, a fresh 30-day period.
    source, now = _fake_source(calls_used_this_period=0, days_left=30)
    daily = _adzuna_daily_budget(source, now, monthly_budget=900)
    # Must keep Adzuna alive for the whole month, not exhaust it in the
    # first couple of days like an unpaced version would (900 / 5 keywords
    # per cycle / (15 min cadence) = exhausted in <2 days). 30/day * 30
    # days = exactly 900.
    assert daily == 30


def test_daily_budget_shrinks_as_the_period_gets_used_up():
    source, now = _fake_source(calls_used_this_period=850, days_left=10)
    daily = _adzuna_daily_budget(source, now, monthly_budget=900)
    assert daily == 5  # 50 calls left / 10 days


def test_daily_budget_is_zero_once_the_monthly_cap_is_hit():
    source, now = _fake_source(calls_used_this_period=900, days_left=10)
    assert _adzuna_daily_budget(source, now, monthly_budget=900) == 0


def test_daily_budget_never_goes_below_one_call_per_day_while_budget_remains():
    # A single call remaining, many days left -- still worth trying once
    # a day rather than rounding down to zero and going fully dark.
    source, now = _fake_source(calls_used_this_period=899, days_left=20)
    assert _adzuna_daily_budget(source, now, monthly_budget=900) == 1


def test_quota_cost_for_adzuna_is_per_keyword_regardless_of_results():
    # Adzuna bills per search call, so cost tracks the keyword list even
    # if the search happened to return nothing (or a lot).
    assert _quota_cost("adzuna", keywords=["a", "b", "c"], raw_postings=[]) == 3
    assert _quota_cost("adzuna", keywords=["a", "b", "c"], raw_postings=[1, 2, 3, 4, 5]) == 3


def test_quota_cost_for_jobspipe_is_per_job_returned_regardless_of_keywords():
    # JobsPipe bills "1 credit = 1 job returned" from a single call that
    # can carry many keywords at once, so cost tracks the result count,
    # not the keyword list -- the opposite of Adzuna's accounting.
    assert _quota_cost("jobspipe", keywords=["a", "b", "c", "d", "e"], raw_postings=[1, 2]) == 2
    assert _quota_cost("jobspipe", keywords=["a", "b", "c", "d", "e"], raw_postings=[]) == 0


def test_quota_cost_defaults_to_per_keyword_for_unbudgeted_sources():
    # Greenhouse/Lever/Ashby/etc aren't quota-budgeted at all, but
    # _quota_cost is still called for them (see _run_source) -- should
    # fall back to the same per-keyword accounting as Adzuna rather than
    # erroring on an unrecognized source name.
    assert _quota_cost("greenhouse", keywords=["a", "b"], raw_postings=[1, 2, 3]) == 2
