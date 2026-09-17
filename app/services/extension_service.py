"""Backend for the companion browser extension (2026-09-16 Long Build,
LNG-02) -- the real fix for the employer-wrapped/bot-blocked ATS case
documented in autofill_service.py's _uses_employer_wrapped_domain
(confirmed live on Samsara: 5/5 real attempts, 0 fields filled, likely
Oracle-VM-datacenter-IP bot-management blocking). Running the fill logic
from the browser extension means it executes in the user's own real
Chrome on their own real residential IP, not the VM -- that's what
actually solves it, not any change to the field-matching logic itself.

Deliberately thin: this module does the SAME answer-matching this app
already does for Playwright-driven autofill (mechanical_common_answer,
contact fields, cover letter text) -- it does not reimplement per-ATS
DOM logic in a second language. The extension's content script does
only generic field DETECTION (label text + input type, no per-ATS
knowledge); this module turns detected labels into real answers, or
leaves a field unanswered rather than ever guessing one. See
extension/README.md for the actual browser-extension code."""

import json
import re
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from . import document_render_service, page_fit_service
from .autofill.common_answers import is_referral_source_question, mechanical_common_answer, referral_source_answer
from .intake_service import get_or_create_company
from .matching_service import MatchingServiceError, get_profile_content_for_application
from .profile_service import profile_completeness_warnings
from ..database import utcnow
from ..models import JobApplication, JobPosting, TailoredDocument

# English + German synonyms -- real gap found live 2026-09-16 on a real
# German-language Personio posting (The Mobility House): "Vorname"/
# "Nachname"/"Telefon" matched none of the English-only patterns, so
# name and phone silently went unanswered while "E-Mail" happened to
# already match _EMAIL_RE's existing e-?mail pattern. Not full i18n --
# just the 4 fields confirmed to actually appear on a real non-English
# form so far; a genuinely new language showing up live is the same
# "extend the pattern" fix, not a redesign.
_FIRST_NAME_RE = re.compile(r"\bfirst\s*name\b|\bvorname\b", re.I)
_LAST_NAME_RE = re.compile(r"\blast\s*name\b|\bsurname\b|\bnachname\b", re.I)
_EMAIL_RE = re.compile(r"\be-?mail\b", re.I)
_PHONE_RE = re.compile(r"\bphone\b|\btelephone\b|\btelefon\b", re.I)
# A real single combined "Full name" field -- found live on a real
# Recruitee posting (Metyis AG): label was literally "Full name *", which
# neither _FIRST_NAME_RE nor _LAST_NAME_RE matches (no "first"/"last"),
# so it silently went unanswered even though the profile's own `name` is
# exactly what it needs. Bare "Name" (trimmed of "*"/"(required)"/
# punctuation) is matched too, but deliberately anchored to the WHOLE
# label -- "Name" is common enough as a substring (Company Name, Employer
# Name) that a loose \bname\b search would misfire on those.
_FULL_NAME_RE = re.compile(r"\bfull\s*name\b", re.I)
_BARE_NAME_RE = re.compile(r"^name\s*[:\*]?\s*(\(required\))?$", re.I)
_COVER_LETTER_RE = re.compile(r"cover letter", re.I)


def _hostname(url: str) -> str | None:
    try:
        return (urlparse(url).netloc or "").lower().removeprefix("www.")
    except ValueError:
        return None


def find_fillable_application(db: Session, current_url: str) -> JobApplication | None:
    """Matches the page the user is actually looking at to a real,
    ready-to-submit application -- "Approved" is the same real state the
    existing autofill/detail-page "Mark Applied" button already requires,
    not a new concept. Hostname match against the posting's own job_url,
    not an exact URL match -- the real page reached after following a
    link is very often a slightly different path than the one the
    posting's API originally returned (e.g. a real application sub-page
    vs. the listing page), but it's always the same real employer
    domain."""
    target_host = _hostname(current_url)
    if not target_host:
        return None
    candidates = (
        db.query(JobApplication)
        .join(JobPosting)
        .filter(JobApplication.status == "Approved")
        .all()
    )
    for application in candidates:
        if _hostname(application.posting.job_url) == target_host:
            return application
    return None


_PHRASE_RE_CACHE: dict[str, re.Pattern] = {}
_LEADING_YES_NO_RE = re.compile(r"^(yes|no)\b")

_APOSTROPHE_VARIANTS = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "´": "'", "`": "'"})


def _normalize_apostrophes(text: str) -> str:
    """Real gap found live 2026-09-16: the real Samsara Greenhouse form's
    education-level options use a curly apostrophe ("Master’s") where
    this app's own canonical phrases (_DEGREE_LEVEL_KEYWORDS) use a plain
    ASCII one -- an otherwise-correct match silently failed on nothing but
    that Unicode difference, invisible to a human glancing at both."""
    return text.translate(_APOSTROPHE_VARIANTS)


def _contains_as_phrase(haystack: str, needle: str) -> bool:
    """True if `needle` appears in `haystack` as real, whole words --
    never a bare substring check. That distinction is the whole point:
    a naive `"no" in "none of the above"` is True, which would wrongly
    match the option "No" inside an unrelated answer containing "None".
    Word-boundary-anchoring both ends of the (possibly multi-word)
    needle avoids that without losing the case that matters, e.g.
    needle "yes" correctly matching inside answer "Yes, in the future"."""
    if not needle:
        return False
    pattern = _PHRASE_RE_CACHE.get(needle)
    if pattern is None:
        pattern = re.compile(r"\b" + r"\s+".join(re.escape(w) for w in needle.split()) + r"\b")
        _PHRASE_RE_CACHE[needle] = pattern
    return pattern.search(haystack) is not None


def _best_option_match(answer: str, options: list[str]) -> str | None:
    """A select can't be set to arbitrary text -- it can only become one
    of its own real options. Never returns anything that isn't literally
    one of `options`; when nothing matches safely and unambiguously, the
    field is left for the human rather than guessed. Deliberately
    conservative given real stakes on questions like visa sponsorship --
    a confidently wrong Yes/No pick on a real application is worse than
    leaving it blank.

    Cascade, most to least confident:
    1. Exact match (case-insensitive) -- e.g. answer "Job Board" against
       an option literally "Job Board".
    2. Either string contains the other AS WHOLE WORDS (never a bare
       substring check -- see _contains_as_phrase), AND exactly one
       option qualifies. More than one candidate means real ambiguity,
       treated the same as no match at all.
    3. Same leading Yes/No word, for a verbose custom option a real
       employer wrote out as a full sentence (e.g. "Yes, I would require
       visa sponsorship or another type of work authorization support.")
       that shares no other phrase at all with the profile's own short
       stored answer ("Yes, in the future") -- real gap found live
       2026-09-16 on a real Hack The Box posting, where step 2 above
       never matches since neither string contains the other. Only
       applied when the ANSWER itself starts with yes/no (never fires
       for an unrelated free-text answer), and only when exactly one
       option shares that same leading word."""
    if not answer or not options:
        return None
    answer_norm = _normalize_apostrophes(answer.strip().lower())

    for option in options:
        if _normalize_apostrophes(option.strip().lower()) == answer_norm:
            return option

    candidates = [
        o for o in options
        if _contains_as_phrase(answer_norm, _normalize_apostrophes(o.strip().lower()))
        or _contains_as_phrase(_normalize_apostrophes(o.strip().lower()), answer_norm)
    ]
    if len(candidates) == 1:
        return candidates[0]

    answer_lead = _LEADING_YES_NO_RE.match(answer_norm)
    if answer_lead:
        lead_matches = [
            o for o in options
            if (m := _LEADING_YES_NO_RE.match(_normalize_apostrophes(o.strip().lower())))
            and m.group(1) == answer_lead.group(1)
        ]
        if len(lead_matches) == 1:
            return lead_matches[0]
    return None


def application_match_summary(db: Session, application: JobApplication) -> dict:
    """Everything the popup shows about a matched application, beyond
    the bare "found it" the original version returned -- researched
    against a real competitor (JobRight's own extension popup) before
    building, not invented: a prominent match score, which real document
    (tailored resume/cover letter, vs. falling back to the base profile)
    will ground the fill, and a completeness signal for the underlying
    profile data. Every one of these already exists elsewhere in this
    codebase (match_score on JobApplication, TailoredDocument rows,
    profile_service.profile_completeness_warnings) -- this only bundles
    them for the one caller that needs all three together.

    profile_warnings is deliberately PROFILE-level, not per-job: an
    application can only ever reach "Approved" (the only status this
    endpoint matches) after clearing every per-job hard-stop flag
    (confirmation_service.has_hard_stop_flag), so a per-job "needs
    attention" signal here could never actually fire -- checked before
    building this, not after."""
    has_tailored_resume = (
        db.query(TailoredDocument)
        .filter(TailoredDocument.application_id == application.id, TailoredDocument.document_type == "resume")
        .first()
        is not None
    )
    has_tailored_cover_letter = (
        db.query(TailoredDocument)
        .filter(TailoredDocument.application_id == application.id, TailoredDocument.document_type == "cover_letter")
        .first()
        is not None
    )
    try:
        profile, _variant_id = get_profile_content_for_application(db, application)
        warnings = profile_completeness_warnings(profile)
    except MatchingServiceError:
        warnings = []

    return {
        "application_id": application.id,
        "job_title": application.posting.job_title,
        "company_name": application.posting.company_name_raw,
        "match_score": application.match_score,
        "has_tailored_resume": has_tailored_resume,
        "has_tailored_cover_letter": has_tailored_cover_letter,
        "profile_warnings": warnings,
    }


_LINKEDIN_RE = re.compile(r"linkedin", re.I)
_GITHUB_RE = re.compile(r"\bgithub\b", re.I)
_PORTFOLIO_RE = re.compile(r"portfolio|personal website|website\b", re.I)
# Real, serious bug found live 2026-09-16: this used to be a bare
# \bcountry\b search over the WHOLE label, which matches any label that
# merely mentions the word "country" -- including real visa-sponsorship/
# work-authorization questions phrased as "...legally authorized to work
# in your country of residence?" (confirmed live on both a real Workable
# and a real Recruitee posting). Since _contact_answer runs before
# mechanical_common_answer in resolve_field_answers' cascade, this
# silently returned "United States" for those questions -- which then
# failed to match any real Yes/No or verbose visa option and left the
# field blank, even though mechanical_common_answer had the actual
# correct answer the whole time. Narrowed to only match when "country"
# (optionally "country code"/"calling code") is essentially the WHOLE
# label -- the real narrow case this was built for (a bare "Country"
# field next to a phone number), never a country mentioned in passing
# inside a longer sentence.
_COUNTRY_RE = re.compile(r"^country(\s*(code|calling code))?$", re.I)
_COUNTRY_LABEL_STRIP_RE = re.compile(r"[\*:]|\(required\)", re.I)
_RECENT_EMPLOYER_RE = re.compile(r"(most recent|current|last) employer|employer name", re.I)
_PREVIOUSLY_WORKED_RE = re.compile(r"previously work(ed)? (at|for|here)|worked (at|for|here) before", re.I)
_US_PHONE_RE = re.compile(r"^\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}$")
_ZIP_RE = re.compile(r"zip code|postal code|\bzip\b", re.I)


def _contact_answer(label: str, profile: dict) -> str | None:
    contact = profile.get("contact") or {}
    if _FIRST_NAME_RE.search(label):
        name_parts = (profile.get("name") or "").split()
        return name_parts[0] if name_parts else None
    if _LAST_NAME_RE.search(label):
        name_parts = (profile.get("name") or "").split()
        return " ".join(name_parts[1:]) if len(name_parts) > 1 else None
    if _FULL_NAME_RE.search(label) or _BARE_NAME_RE.match(label.strip()):
        return profile.get("name") or None
    if _EMAIL_RE.search(label):
        return contact.get("email") or None
    if _PHONE_RE.search(label):
        return contact.get("phone") or None
    # linkedin/github/portfolio checked before the generic company/name
    # patterns below since a label like "LinkedIn Profile" could
    # otherwise partially match something broader -- these are real
    # stored URLs (contact.linkedin/github/portfolio), same fields
    # lever_autofill.py already extracts for Lever's own URL fields, not
    # a new concept invented for the extension.
    if _LINKEDIN_RE.search(label):
        return contact.get("linkedin") or None
    if _GITHUB_RE.search(label):
        return contact.get("github") or None
    if _PORTFOLIO_RE.search(label):
        return contact.get("portfolio") or None
    # A phone "country" (or country-code) field -- inferred from the
    # stored phone number's own format, not a separate stored fact,
    # since the profile has no dedicated country field. Deliberately
    # narrow: only answers when the stored phone genuinely looks like a
    # real US number (matches this candidate's actual real data), never
    # a blind default for every fork/profile.
    stripped_label = _COUNTRY_LABEL_STRIP_RE.sub("", label).strip()
    if _COUNTRY_RE.match(stripped_label) and contact.get("phone") and _US_PHONE_RE.match(contact["phone"].strip()):
        return "United States"
    # No zip/postal code field exists in the profile schema today (only
    # a free-text contact.location like "Tempe, Arizona") -- checked the
    # real stored data directly rather than assume, confirmed there's
    # nothing to extract yet. This matches contact.zip/zip_code/
    # postal_code so it starts working the moment one of those is added
    # via the Profile page, without needing another round of this.
    if _ZIP_RE.search(label):
        return contact.get("zip") or contact.get("zip_code") or contact.get("postal_code") or None
    return None


def _recent_employer_answer(label: str, profile: dict) -> str | None:
    """experience[0] is the real most-recent entry -- this profile
    schema stores experience reverse-chronologically (confirmed against
    the real stored `date` strings, not assumed), same order every
    resume/tailoring code path in this app already relies on."""
    if not _RECENT_EMPLOYER_RE.search(label):
        return None
    experience = profile.get("experience")
    if not isinstance(experience, list) or not experience:
        return None
    company = experience[0].get("company") if isinstance(experience[0], dict) else None
    return company or None


_EDUCATION_LEVEL_RE = re.compile(r"highest level of education|education level|level of education", re.I)
_DEGREE_LEVEL_KEYWORDS = (
    (re.compile(r"ph\.?d|doctorate", re.I), "Doctorate"),
    (re.compile(r"master", re.I), "Master's Degree"),
    (re.compile(r"bachelor", re.I), "Bachelor's Degree"),
    (re.compile(r"associate", re.I), "Associate's Degree"),
    (re.compile(r"high school", re.I), "High School Diploma"),
)


def _education_level_answer(label: str, profile: dict) -> str | None:
    """education[0] is the real highest/most-recent degree -- same
    reverse-chronological convention already confirmed for experience,
    and true here too (checked the real stored dates before relying on
    it). Returns a canonical phrase ("Master's Degree") matched against
    the field's real options via the same two-directional phrase
    cascade _best_option_match already uses for every other select --
    covers the common real phrasings ("Master's Degree", "Master's",
    "Graduate Degree") without inventing a match for one this app has
    never actually seen."""
    if not _EDUCATION_LEVEL_RE.search(label):
        return None
    education = profile.get("education")
    if not isinstance(education, list) or not education:
        return None
    degree_text = education[0].get("degree") if isinstance(education[0], dict) else None
    if not degree_text:
        return None
    for pattern, canonical in _DEGREE_LEVEL_KEYWORDS:
        if pattern.search(degree_text):
            return canonical
    return None


_RELOCATION_ASSISTANCE_RE = re.compile(r"relocation assistance|assistance.{0,20}relocat", re.I)


def _relocation_assistance_answer(label: str, profile: dict) -> str | None:
    """Deliberately narrower than mechanical_common_answer's own
    willing_to_relocate pattern -- "do you REQUIRE relocation
    ASSISTANCE" is a real, different question from "are you willing to
    relocate" (financial/logistical help vs. general openness), and a
    candidate can honestly be open to relocating without needing help
    doing it. Only answers the safe, unambiguous direction: genuinely
    willing to relocate implies "No, I don't need help" is a reasonable
    real default (nothing in the stored preference suggests otherwise).
    "Depends on the role" is a real, genuine maybe -- left blank rather
    than guessed either way. Every answer here still only ever lands on
    a form the candidate reviews before the real, human, manual submit
    click -- this platform's one hard rule that never changes -- so a
    reasonable default on a required dropdown is a real, bounded risk,
    not a fabrication that could slip out unseen."""
    if not _RELOCATION_ASSISTANCE_RE.search(label):
        return None
    willing = (profile.get("application_preferences") or {}).get("willing_to_relocate")
    if willing in ("Yes", "No"):
        return "No"
    return None


_YEARS_EXPERIENCE_RE = re.compile(r"years.{0,20}experience|experience.{0,20}years", re.I)
_DATA_ROLE_TITLE_RE = re.compile(r"data|machine learning|\bml\b|\bai\b|analytics", re.I)
_MONTH_YEAR_RE = re.compile(r"([A-Za-z]{3,9})\s+(\d{4})")
_MONTH_NUMBERS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DATE_RANGE_SPLIT_RE = re.compile(r"[–—-]")
_PRESENT_RE = re.compile(r"present|current", re.I)


def _parse_month_year(text: str) -> int | None:
    """An absolute month index (year*12 + month), for sortable/
    subtractable date math without a full date-parsing library. Matches
    the real stored format ("Aug 2021") -- month name matched by its
    first 3 letters so both abbreviated and full month names work."""
    match = _MONTH_YEAR_RE.search(text)
    if not match:
        return None
    month = _MONTH_NUMBERS.get(match.group(1).strip().lower()[:3])
    if not month:
        return None
    return int(match.group(2)) * 12 + month


def _parse_date_range(date_text: str) -> tuple[int, int] | None:
    """(start_month_index, end_month_index) from a real stored "date"
    string like "Aug 2021 - Mar 2022" or "May 2026 - Present". Splits on
    either a real en-dash (what this app's own stored data actually
    uses -- checked directly, not assumed) or a plain hyphen, since
    other forks' data might use either."""
    if not date_text:
        return None
    parts = _DATE_RANGE_SPLIT_RE.split(date_text, maxsplit=1)
    if len(parts) != 2:
        return None
    start = _parse_month_year(parts[0])
    if start is None:
        return None
    end_text = parts[1].strip()
    if _PRESENT_RE.search(end_text):
        now = utcnow()
        end = now.year * 12 + now.month
    else:
        end = _parse_month_year(end_text)
    if end is None or end < start:
        return None
    return start, end


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Standard interval merge -- so two overlapping/concurrent roles
    (e.g. a part-time role alongside a full-time one) never get their
    shared months double-counted."""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _real_years_in_data_related_roles(profile: dict) -> float | None:
    """Sums real elapsed time (interval-merged, never double-counted)
    across every experience entry whose own TITLE contains a data/ML/AI/
    analytics keyword -- an objective, title-based criterion, not a
    subjective judgment call about what each role's day-to-day actually
    involved. Checked against the real live data before relying on this
    as a criterion: every one of the 6 real stored experience entries
    this was built against genuinely has one of these words in its own
    title already, not a hypothetical."""
    experience = profile.get("experience")
    if not isinstance(experience, list):
        return None
    intervals = []
    for entry in experience:
        if not isinstance(entry, dict):
            continue
        if not _DATA_ROLE_TITLE_RE.search(entry.get("role") or ""):
            continue
        parsed = _parse_date_range(entry.get("date") or "")
        if parsed:
            intervals.append(parsed)
    if not intervals:
        return None
    # +1 per merged interval, not a bare end-start difference: both the
    # start and end month labels are real months actually worked ("Aug
    # 2021 - Mar 2022" is 8 real months -- Aug through Mar inclusive --
    # not 7). Found live 2026-09-16: this undercounted every real
    # interval by one month, which on Deshraj's real profile was the
    # real difference between landing in no bucket at all and correctly
    # landing in "3+ years".
    total_months = sum(end - start + 1 for start, end in _merge_intervals(intervals))
    return round(total_months / 12, 1)


_RANGE_PATTERN = re.compile(r"(\d+)\s*(?:-|–|to)\s*(\d+)", re.I)
_PLUS_PATTERN = re.compile(r"(\d+)\s*\+")
_UNDER_PATTERN = re.compile(r"(?:less than|under|fewer than)\s*(\d+)", re.I)


def _bucket_candidates(value: float, options: list[str]) -> list[str]:
    candidates = []
    for option in options:
        range_match = _RANGE_PATTERN.search(option)
        if range_match:
            low, high = float(range_match.group(1)), float(range_match.group(2))
            if low <= value <= high:
                candidates.append(option)
            continue
        plus_match = _PLUS_PATTERN.search(option)
        if plus_match:
            if value >= float(plus_match.group(1)):
                candidates.append(option)
            continue
        under_match = _UNDER_PATTERN.search(option)
        if under_match and value < float(under_match.group(1)):
            candidates.append(option)
    return candidates


def _match_years_to_bucketed_option(years: float, options: list[str]) -> str | None:
    """A select's real options are almost always buckets ("2-3 years",
    "3+ years", "Less than 1 year"), never a free-text number field --
    parses each option's own real numeric range and returns the one the
    computed real number actually falls into. Only returns a match when
    exactly one option's range contains it; genuine ambiguity (e.g.
    poorly-formed or overlapping-looking options) is left for the human
    rather than guessed.

    The raw figure is tried FIRST and, if it cleanly matches exactly one
    option, returned immediately -- rounding is only ever consulted as a
    fallback when the raw figure matches NO option at all (real employer
    bucket sets aren't guaranteed to cover every fractional value;
    confirmed live: Samsara's own options are "0-1 years" / "1-2 years" /
    "3+ years", note the gap, no "2-3" bucket exists at all). A real bug
    caught by this function's own test before shipping: trying the
    rounded figure unconditionally, even when the raw one already had a
    clean match, could introduce a NEW ambiguity the raw figure never
    had (e.g. 1.4 years cleanly matches "1-2 years" alone on the raw
    value, but rounds to 1, which sits on the shared boundary of BOTH
    "0-1 years" and "1-2 years") -- checking raw-first, and only ever
    falling back to rounding on a genuine gap, avoids that regression
    while still correctly landing 2.9 years on "3+ years"."""
    raw_candidates = _bucket_candidates(years, options)
    if raw_candidates:
        return raw_candidates[0] if len(raw_candidates) == 1 else None
    rounded_candidates = _bucket_candidates(round(years), options)
    return rounded_candidates[0] if len(rounded_candidates) == 1 else None


def _years_experience_answer(label: str, profile: dict, field_type: str | None, options: list[str] | None) -> str | None:
    if not _YEARS_EXPERIENCE_RE.search(label):
        return None
    years = _real_years_in_data_related_roles(profile)
    if years is None:
        return None
    if field_type == "select":
        return _match_years_to_bucketed_option(years, options or [])
    # A free-text/number field, not a select -- a plain real number,
    # not run through the bucket matcher (there's nothing to match --
    # options is empty/irrelevant here).
    return str(years)


def _previously_worked_here_answer(label: str, profile: dict, current_company_name: str) -> str | None:
    """A real Yes/No inferable straight from the candidate's own work
    history: does any past employer's name match the company this
    application is actually for? Case-insensitive, and only answers
    when there's a real company name to compare against -- never
    guesses "No" for a candidate with no experience list at all, since
    that's a missing-data case, not a genuine "never worked there"."""
    if not _PREVIOUSLY_WORKED_RE.search(label):
        return None
    experience = profile.get("experience")
    if not isinstance(experience, list) or not current_company_name:
        return None
    target = current_company_name.strip().lower()
    for entry in experience:
        if isinstance(entry, dict) and (entry.get("company") or "").strip().lower() == target:
            return "Yes"
    return "No"


def resolve_field_answers(db: Session, application_id: int, fields: list[dict]) -> dict[str, str]:
    """fields: [{"field_id": ..., "label": ...}, ...] from the content
    script's generic DOM scan. Returns only the fields it has a real,
    profile-grounded answer for -- a field this can't confidently answer
    is simply absent from the result, left for the human, same posture
    as mechanical_common_answer's own "None means don't guess" contract.
    Never invokes an LLM: matches Playwright's autofill in NOT spending a
    real API call on fixed-fact fields, and keeps this endpoint fast
    enough to feel instant while the user is looking at the real page."""
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        return {}

    # Base profile, not resolve_grounding_profile's tailored-resume-
    # preferring variant -- name/email/phone/EEO/visa are fixed facts
    # about the candidate that tailoring never touches (only wording/
    # emphasis does), matching autofill_service.py's own precedent for
    # these exact same fields.
    try:
        profile, _variant_id = get_profile_content_for_application(db, application)
    except MatchingServiceError:
        profile = {}

    cover_letter = (
        db.query(TailoredDocument)
        .filter(TailoredDocument.application_id == application_id, TailoredDocument.document_type == "cover_letter")
        .first()
    )

    answers = {}
    for field in fields:
        label = (field.get("label") or "").strip()
        if not label:
            continue
        field_id = field.get("field_id")

        # Handled separately, not through the generic select-matching
        # cascade below: a numeric years-of-experience bucket needs
        # actual range math ("does 2.7 fall inside '2-3 years'"), not
        # the phrase-containment matching _best_option_match does for
        # every other select -- see _match_years_to_bucketed_option.
        years_answer = _years_experience_answer(label, profile, field.get("type"), field.get("options"))
        if years_answer:
            answers[field_id] = years_answer
            continue

        answer = _contact_answer(label, profile)
        if answer is None:
            answer = _recent_employer_answer(label, profile)
        if answer is None:
            answer = _previously_worked_here_answer(label, profile, application.posting.company_name_raw)
        if answer is None:
            answer = _education_level_answer(label, profile)
        if answer is None:
            answer = _relocation_assistance_answer(label, profile)
        if answer is None:
            answer = mechanical_common_answer(label, profile)
        if answer is None and is_referral_source_question(label):
            answer = referral_source_answer(application.posting.source)
        if answer is None and _COVER_LETTER_RE.search(label) and cover_letter:
            answer = cover_letter.content

        if answer and field.get("type") == "select":
            # The free-text answer above is never itself a valid value
            # for a <select> -- it has to become one of the real options
            # the content script actually found on the page.
            answer = _best_option_match(answer, field.get("options") or [])

        if answer:
            answers[field_id] = answer
    return answers


class ExtensionServiceError(Exception):
    """User-facing failure -- the router turns this into a real error
    message shown in the popup/on-page badge, never a bare 500."""


def render_document_for_attachment(db: Session, application_id: int, document_type: str) -> tuple[bytes, str]:
    """Renders the exact same real PDF the existing download route and
    Playwright's own (bot-blocked-on-Samsara) autofill already produce
    from this application's real tailored content -- see
    routers/jobs.py's download_tailored_document, which this mirrors
    rather than duplicates the actual rendering logic of. The one new
    thing this enables: the extension's content script can fetch these
    real bytes and attach them to a real <input type="file"> via the
    DataTransfer API (setting a file's real bytes, not a path string --
    the thing a script actually CAN do; researched properly after an
    earlier, incomplete claim that no extension could ever attach a
    file at all, which was wrong). Returns (pdf_bytes, filename)."""
    if document_type not in ("resume", "cover_letter"):
        raise ExtensionServiceError(f"Unknown document type '{document_type}'.")

    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise ExtensionServiceError(f"Application {application_id} not found.")

    doc = (
        db.query(TailoredDocument)
        .filter(TailoredDocument.application_id == application_id, TailoredDocument.document_type == document_type)
        .first()
    )
    if not doc:
        raise ExtensionServiceError("Nothing tailored yet for this document type -- generate it in Career Pilot first.")

    if document_type == "resume":
        try:
            pdf_bytes = page_fit_service.render_resume_pdf_with_fit(db, doc.content)["pdf_bytes"]
        except page_fit_service.PageFitExhaustedError as e:
            raise ExtensionServiceError(str(e)) from e
        name_part = "resume"
    else:
        resume_doc = (
            db.query(TailoredDocument)
            .filter(TailoredDocument.application_id == application_id, TailoredDocument.document_type == "resume")
            .first()
        )
        candidate_name = json.loads(resume_doc.content).get("name", "") if resume_doc else ""
        pdf_bytes = document_render_service.render_cover_letter_pdf(doc.content, candidate_name)
        name_part = "cover-letter"

    filename = f"{name_part}-{application.posting.company_name_raw}-{application.posting.job_title}.pdf".replace(" ", "-")
    return pdf_bytes, filename


def add_job_from_page(db: Session, url: str, job_title: str, company_name: str, job_description: str) -> JobApplication:
    """Real fix for the biggest, most-repeated gap this popup had:
    JobRight's own popup lets a user add an unrecognized posting and
    generate tailored documents right there, without ever leaving the
    page -- researched directly against real competitor popups before
    building this (JobRight, Simplify Copilot), not guessed at. Same
    one-click "+ Add This Job" capability, reusing the exact same
    Company get-or-create/dedupe logic intake_service.py already uses
    for every other source (source="manual" here, a value the
    JobPosting model already documented but nothing had ever actually
    created until now) rather than a second copy.

    Only creates the real DB rows and returns fast -- scoring/tailoring/
    approval run afterward in a background thread the router kicks off
    (see score_and_tailor_new_application), since a real multi-pass LLM
    tailoring run takes 30-90s and this call needs to stay fast enough
    for the popup to feel responsive, not hang open waiting."""
    job_title = (job_title or "").strip()
    job_description = (job_description or "").strip()
    company_name = (company_name or "").strip() or "Unknown Company"
    if not job_title:
        raise ExtensionServiceError("Couldn't find a real job title on this page -- add it from the Jobs page instead.")
    if len(job_description) < 50:
        raise ExtensionServiceError(
            "Couldn't find enough real job description text on this page -- add it from the Jobs page instead."
        )

    if url:
        existing_posting = db.query(JobPosting).filter(JobPosting.job_url == url).first()
        if existing_posting:
            existing_application = (
                db.query(JobApplication).filter(JobApplication.posting_id == existing_posting.id).first()
            )
            if existing_application:
                return existing_application  # already added from this exact URL -- never create a duplicate

    company = get_or_create_company(db, company_name)
    posting = JobPosting(
        company_id=company.id,
        company_name_raw=company_name,
        job_title=job_title,
        job_url=url or None,
        job_description=job_description,
        source="manual",
        first_seen_at=utcnow(),
        last_seen_at=utcnow(),
    )
    db.add(posting)
    db.commit()
    db.refresh(posting)

    application = JobApplication(posting_id=posting.id, status="Ingested")
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


def score_and_tailor_new_application(db: Session, application_id: int) -> None:
    """Real-time counterpart to confirmation_service.evaluate_and_enqueue's
    asynchronous-discovery path. That function's "Pending Confirmation"
    timed window exists to give a human time to notice and object before
    an UNATTENDED Playwright auto-launch runs on a posting nobody has
    looked at yet -- it was built for automated background discovery.
    None of that applies here: this is only ever called right after
    add_job_from_page, whose entire point is that a human is already
    looking at this exact posting, right now, and chose to add it
    themselves. A clean tailoring result (no real hard-stop fabrication
    flag) is approved immediately so the extension's own
    match_current_page (Approved-only) picks it up without the user
    ever leaving this page -- the same real human-review-before-submit
    safety net still applies to whatever gets filled, this only skips
    an asynchronous waiting period that was never meaningful here.

    A genuine hard-stop flag still goes to Needs Review exactly like
    every other source -- this never bypasses that safety net, only the
    asynchronous-specific confirmation window a manually-added,
    already-attended posting was never really subject to."""
    from . import matching_service, tailoring_service
    from .confirmation_service import approve_application

    matching_service.score_application(db, application_id)
    tailoring_service.tailor_application(db, application_id)

    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if application and application.status == "Pending Confirmation":
        approve_application(db, application_id)
