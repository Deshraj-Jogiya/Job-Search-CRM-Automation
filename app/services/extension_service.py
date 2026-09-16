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

import re
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from .autofill.common_answers import is_referral_source_question, mechanical_common_answer, referral_source_answer
from .matching_service import MatchingServiceError, get_profile_content_for_application
from .profile_service import profile_completeness_warnings
from ..models import JobApplication, JobPosting, TailoredDocument

_FIRST_NAME_RE = re.compile(r"\bfirst\s*name\b", re.I)
_LAST_NAME_RE = re.compile(r"\blast\s*name\b", re.I)
_EMAIL_RE = re.compile(r"\be-?mail\b", re.I)
_PHONE_RE = re.compile(r"\bphone\b", re.I)
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
       treated the same as no match at all."""
    if not answer or not options:
        return None
    answer_norm = answer.strip().lower()

    for option in options:
        if option.strip().lower() == answer_norm:
            return option

    candidates = [
        o for o in options
        if _contains_as_phrase(answer_norm, o.strip().lower()) or _contains_as_phrase(o.strip().lower(), answer_norm)
    ]
    return candidates[0] if len(candidates) == 1 else None


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
_COUNTRY_RE = re.compile(r"\bcountry\b", re.I)
_RECENT_EMPLOYER_RE = re.compile(r"(most recent|current|last) employer|employer name", re.I)
_PREVIOUSLY_WORKED_RE = re.compile(r"previously work(ed)? (at|for|here)|worked (at|for|here) before", re.I)
_US_PHONE_RE = re.compile(r"^\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}$")


def _contact_answer(label: str, profile: dict) -> str | None:
    contact = profile.get("contact") or {}
    if _FIRST_NAME_RE.search(label):
        name_parts = (profile.get("name") or "").split()
        return name_parts[0] if name_parts else None
    if _LAST_NAME_RE.search(label):
        name_parts = (profile.get("name") or "").split()
        return " ".join(name_parts[1:]) if len(name_parts) > 1 else None
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
    if _COUNTRY_RE.search(label) and contact.get("phone") and _US_PHONE_RE.match(contact["phone"].strip()):
        return "United States"
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
