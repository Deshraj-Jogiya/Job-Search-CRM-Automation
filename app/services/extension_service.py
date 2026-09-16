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


def _contact_answer(label: str, profile: dict) -> str | None:
    if _FIRST_NAME_RE.search(label):
        name_parts = (profile.get("name") or "").split()
        return name_parts[0] if name_parts else None
    if _LAST_NAME_RE.search(label):
        name_parts = (profile.get("name") or "").split()
        return " ".join(name_parts[1:]) if len(name_parts) > 1 else None
    if _EMAIL_RE.search(label):
        return (profile.get("contact") or {}).get("email") or None
    if _PHONE_RE.search(label):
        return (profile.get("contact") or {}).get("phone") or None
    return None


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
            answer = mechanical_common_answer(label, profile)
        if answer is None and is_referral_source_question(label):
            answer = referral_source_answer(application.posting.source)
        if answer is None and _COVER_LETTER_RE.search(label) and cover_letter:
            answer = cover_letter.content

        if answer:
            answers[field_id] = answer
    return answers
