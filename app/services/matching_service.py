"""
Phase 3: match evaluation. Compares a JobApplication's posting against
a candidate profile (from Phase 1's ProfileVariant/ProfileVersion) via
the LLM provider abstraction and stores the result on the application.

Scoring is deliberately NOT run automatically on every ingested
posting -- each call is a real LLM request with real cost, and intake
can produce dozens of postings per cycle (see Phase 2). It's triggered
on demand (viewing/scoring a specific application), same "user stays
in control" posture as the rest of this build.
"""

import json

from sqlalchemy.orm import Session

from ..models import JobApplication, ProfileVariant, ProfileVersion
from .activity_logger import log_activity
from .llm import get_llm_provider, parse_json_response


class MatchingServiceError(Exception):
    """Raised for user-facing failures (no application, no usable
    profile, bad LLM response) -- callers show the message instead of
    letting a 500 through."""


# Re-scoring one of these would silently undo a decision the human
# already made (submitted, cleared to submit, or explicitly passed on).
_FINAL_STATUSES = ("Applied", "Approved", "Rejected", "Interviewing", "Offer", "Not Selected")


def get_profile_content_for_application(db: Session, application: JobApplication) -> tuple[dict, int]:
    """Resolve which profile content to score against: the variant
    already pinned to this application if set, otherwise the default
    variant's active version. Returns (content, variant_id)."""
    variant = None
    if application.profile_variant_id:
        variant = db.query(ProfileVariant).filter(ProfileVariant.id == application.profile_variant_id).first()

    if not variant:
        variant = db.query(ProfileVariant).filter(ProfileVariant.is_default == True).first()  # noqa: E712

    if not variant:
        raise MatchingServiceError(
            "No profile variant is set up yet. Create one and seed its content on the Profile page first."
        )

    version = (
        db.query(ProfileVersion)
        .filter(ProfileVersion.variant_id == variant.id, ProfileVersion.is_active == True)  # noqa: E712
        .first()
    )
    if not version:
        raise MatchingServiceError(
            f"Profile variant '{variant.name}' has no active content yet -- seed it on the Profile page first."
        )

    return json.loads(version.content_json), variant.id


def evaluate_match(profile_content: dict, jd_text: str) -> dict:
    """Ask the LLM to score a candidate profile against a job
    description. Returns the raw parsed dict -- callers decide what to
    persist."""
    profile_summary = (
        f"Name: {profile_content.get('name')}\n"
        f"Title: {profile_content.get('title')}\n"
        f"Summary: {profile_content.get('summary')}\n"
        f"Skills: {json.dumps(profile_content.get('skills', {}))}\n"
        f"Experience: {[e.get('role', '') + ' at ' + e.get('company', '') for e in profile_content.get('experience', [])]}"
    )

    llm = get_llm_provider()
    raw = llm.complete_json(
        system="You are an expert technical recruiter and ATS analyzer. You return only raw JSON.",
        prompt=(
            "Compare this candidate profile with the job description. Evaluate the match percentage, "
            "key skill overlaps, missing key terms/keywords, candidate strengths, and gap analysis.\n\n"
            f"Candidate Profile:\n{profile_summary}\n\n"
            f"Job Description:\n{jd_text}\n\n"
            "Respond in EXACTLY this JSON shape:\n"
            "{\n"
            '  "match_score": 85,\n'
            '  "matching_skills": ["Python", "SQL"],\n'
            '  "missing_keywords": ["Spark", "Docker"],\n'
            '  "visa_sponsorship": "Sponsors | No Sponsorship | Unknown -- based only on what the JD explicitly states",\n'
            '  "strengths": "2-3 sentences on why the candidate is a strong fit",\n'
            '  "gaps_analysis": "key gaps between the candidate and the requirements"\n'
            "}\n"
            "match_score must be an integer 0-100. Do not wrap the output in markdown code fences."
        ),
        temperature=0.2,
    )
    return parse_json_response(raw)


def score_application(db: Session, application_id: int) -> JobApplication:
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise MatchingServiceError(f"Application {application_id} not found.")
    if application.status in _FINAL_STATUSES:
        raise MatchingServiceError(
            f"Application is '{application.status}' -- can't re-score a finalized application."
        )

    profile_content, variant_id = get_profile_content_for_application(db, application)
    jd_text = application.posting.job_description

    try:
        result = evaluate_match(profile_content, jd_text)
    except Exception as e:
        raise MatchingServiceError(f"Match evaluation failed: {e}") from e

    application.match_score = int(result.get("match_score", 0))
    application.match_analysis_json = json.dumps(result)
    application.visa_sponsorship = result.get("visa_sponsorship", "Unknown")
    application.profile_variant_id = variant_id
    db.commit()

    log_activity(
        db,
        f"Scored '{application.posting.job_title}' at {application.posting.company_name_raw}: "
        f"{application.match_score}% match.",
        "INFO",
    )
    return application
