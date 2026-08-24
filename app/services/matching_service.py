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
    persist.

    Scores against the candidate's real experience bullets and projects,
    not just their job titles and skills list -- an earlier version
    reduced each experience entry to a bare "role at company" string and
    omitted projects/education/certifications entirely, which starved
    the LLM of the actual evidence (quantified achievements, specific
    technologies used) it needs to recognize a genuinely strong match.
    Contact info is deliberately excluded -- not needed for a match
    evaluation, no reason to send it to the LLM provider."""
    scoring_profile = {
        "name": profile_content.get("name"),
        "title": profile_content.get("title"),
        "summary": profile_content.get("summary"),
        "skills": profile_content.get("skills", {}),
        "experience": profile_content.get("experience", []),
        "projects": profile_content.get("projects", []),
        "education": profile_content.get("education", []),
        "certifications": profile_content.get("certifications", []),
    }

    llm = get_llm_provider()
    raw = llm.complete_json(
        system="You are an expert technical recruiter and ATS analyzer. You return only raw JSON.",
        prompt=(
            "Compare this candidate profile with the job description. Evaluate the match percentage, "
            "key skill overlaps, missing key terms/keywords, candidate strengths, and gap analysis. Base "
            "the score on the full evidence below -- a specific accomplishment or technology named in an "
            "experience bullet or project counts as real evidence of that skill, not just whether it "
            "appears in the standalone skills list. Skill and tool are not the same thing: if the JD names "
            "a specific tool the candidate hasn't used, but their profile shows hands-on experience with a "
            "directly comparable tool in the exact same category (e.g. Tableau vs. Power BI -- both BI/"
            "data-visualization tools; Airflow vs. Prefect/Dagster -- both workflow orchestrators; GitHub "
            "Actions vs. Jenkins/CircleCI -- all CI/CD tools), credit that as transferable evidence for the "
            "underlying skill rather than listing the JD's specific tool as a missing/blocking gap -- note "
            "it as a minor, easily-bridged difference instead. Only apply this to genuinely interchangeable "
            "tools within the same narrow category, never to different technology classes or entire "
            "platforms/ecosystems.\n\n"
            "Critically, weight REQUIRED qualifications far more heavily than PREFERRED/nice-to-have ones "
            "when computing match_score -- most JDs explicitly separate the two (e.g. a 'What You'll Bring' "
            "or 'Requirements' section vs. a 'Nice to Have', 'Bonus Points', 'Preferred Qualifications' "
            "section, or inline phrasing like 'is a plus', 'familiarity with', 'exposure to', 'awareness "
            "of or interest in'). A candidate who strongly satisfies every REQUIRED qualification but lacks "
            "several explicitly-optional/preferred ones should score 85%+, not be capped in the 60-70s just "
            "because the gaps list has several items on it -- the JD's own language about whether something "
            "is required vs. optional matters more than how many items are missing. Only let a missing "
            "qualification meaningfully cap the score if the JD phrases it as required, a must-have, or a "
            "hard qualification (e.g. required years of experience, a required degree/certification, a "
            "required clearance) -- an unstated seniority/years bar should be read from the JD's own level "
            "framing (e.g. 'Entry Level', '1-3 years', 'or equivalent demonstrated skills') and weighted "
            "accordingly, not assumed to require more than what's actually written.\n\n"
            f"Candidate Profile:\n{json.dumps(scoring_profile, indent=2)}\n\n"
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
