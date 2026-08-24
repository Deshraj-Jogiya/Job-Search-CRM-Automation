"""
Mechanical (non-LLM) answers for question categories that recur on
nearly every ATS application and are not genuinely JD-dependent
judgment calls:

- EEO voluntary self-identification (gender, ethnicity, race, veteran
  status, disability status) and application preferences (work
  authorization, visa sponsorship, willingness to relocate, salary
  expectations, notice period, current employment) -- fixed personal
  facts about the candidate, sourced from the profile's own `eeo` and
  `application_preferences` sections (same posture as `contact`), not
  something the per-JD tailoring LLM should ever be asked to reason
  about from a job description.
- "How did you hear about this position?" -- determined by which
  intake source actually surfaced this posting (JobPosting.source),
  not a fact about the candidate's skills/background at all.

Routing these through the per-JD LLM draft call was both wasteful (a
real LLM call spent on a question with a fixed answer) and unreliable
-- the LLM has no real basis to answer "what's your gender?" or "do
you require visa sponsorship?" from a job description, and correctly
refuses (marks it [REVIEW NEEDED]) most of the time, which is exactly
why these were showing up unanswered.
"""

import re

# (pattern, profile section, key within that section). Checked in order
# -- first match wins, so a more specific pattern should sit before a
# more general one if they could both plausibly match the same label.
_COMMON_QUESTION_PATTERNS = [
    (re.compile(r"\bgender\b", re.I), "eeo", "gender"),
    (re.compile(r"hispanic|latino", re.I), "eeo", "hispanic_or_latino"),
    (re.compile(r"\brace\b|racial identity", re.I), "eeo", "race"),
    (re.compile(r"veteran", re.I), "eeo", "veteran_status"),
    (re.compile(r"disab", re.I), "eeo", "disability_status"),
    (re.compile(r"visa sponsorship|require sponsorship|sponsor.{0,15}visa|sponsorship.{0,15}now or in the future", re.I), "application_preferences", "visa_sponsorship"),
    (re.compile(r"authorized to work|legally authorized|work authorization", re.I), "application_preferences", "work_authorization"),
    (re.compile(r"willing(ness)? to relocate|open to relocat", re.I), "application_preferences", "willing_to_relocate"),
    (re.compile(r"notice period|earliest (start|available) date|when.{0,10}(can you|are you able to) start", re.I), "application_preferences", "notice_period"),
    (re.compile(r"currently employed|are you (currently )?employed", re.I), "application_preferences", "currently_employed"),
]

_SALARY_PATTERN = re.compile(r"salary expectation|desired salary|expected salary|compensation expectation", re.I)

_REFERRAL_PATTERN = re.compile(r"how did you (hear|find)|where did you hear|referral source", re.I)


def _salary_answer(profile: dict) -> str | None:
    """Salary is stored as two separate fields (a minimum floor and a
    negotiable flag, set via two distinct controls on the Profile page)
    rather than one pre-combined string, so the form can show each
    control's real current state on reload. Combined into one answer
    string here, at match time, instead."""
    prefs = profile.get("application_preferences") or {}
    minimum = prefs.get("salary_minimum")
    negotiable = prefs.get("salary_negotiable")
    parts = []
    if minimum:
        parts.append(f"{minimum} minimum")
    if negotiable:
        parts.append("negotiable")
    return ", ".join(parts) if parts else None


def mechanical_common_answer(label: str, profile: dict) -> str | None:
    """Returns the profile's stored answer for a question whose label
    matches a known EEO/application-preference category, or None if
    this isn't a recognized question or the profile has no answer for
    that category (falls through to the normal LLM-draft-or-leave-for-
    human path either way -- this is additive, never a hard
    requirement)."""
    if not label:
        return None
    if _SALARY_PATTERN.search(label):
        return _salary_answer(profile)
    for pattern, section, key in _COMMON_QUESTION_PATTERNS:
        if pattern.search(label):
            return (profile.get(section) or {}).get(key) or None
    return None


def is_referral_source_question(label: str) -> bool:
    return bool(label) and bool(_REFERRAL_PATTERN.search(label))


def referral_source_answer(posting_source: str) -> str:
    """LinkedIn when the posting was actually found via LinkedIn intake,
    'Job Board' otherwise (Adzuna, direct Greenhouse/Lever/Ashby board
    polling, JobRight-seeded discovery, manual entry) -- 'Job Board' is
    the closest honest, generic answer available for every non-LinkedIn
    source, not a guess at a more specific channel."""
    return "LinkedIn" if posting_source == "linkedin" else "Job Board"
