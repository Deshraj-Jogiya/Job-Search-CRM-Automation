"""
Phase 3: resume/cover-letter tailoring. Unlike the deleted prototype
(which called a single-shot LLM tailoring pass and then set the score
to a hardcoded random.randint(95, 98) -- explicitly called out in
CLAUDE.md as something not to carry forward), this runs a genuine
tailor -> verify -> refine loop against the experience section and
reports whatever score the LAST verify pass actually produced, capped
at max_refine_passes so a stubborn JD can't loop forever.

Cover letter scoring is a separate, independent LLM pass -- it is not
derived from the resume's ATS score, since a resume can be a strong
keyword match while the letter itself is weak, or vice versa.
"""

import json

from sqlalchemy.orm import Session

from ..models import JobApplication, TailoredDocument
from .activity_logger import log_activity
from .llm import get_llm_provider, parse_json_response
from .matching_service import MatchingServiceError, get_profile_content_for_application

TARGET_ATS_SCORE = 90
MAX_REFINE_PASSES = 2

# Re-tailoring one of these would silently undo a decision the human
# already made (submitted, cleared to submit, or explicitly passed on) --
# refuse rather than reverting status back into the confirmation queue.
_FINAL_STATUSES = ("Applied", "Approved", "Rejected")


def _tailor_experience_pass(experience: list, jd_text: str) -> list:
    llm = get_llm_provider()
    raw = llm.complete_json(
        system="You are an expert resume writer. You return only raw JSON.",
        prompt=(
            "Rewrite these professional experience bullets to highlight skills and achievements relevant to "
            "the job description below.\n\n"
            "RULES: Do not fabricate any work history, dates, companies, or metrics -- every statement must "
            "stay genuine. Align framing and emphasis with high-frequency, important keywords from the JD.\n\n"
            f"Original Experience:\n{json.dumps(experience, indent=2)}\n\n"
            f"Job Description:\n{jd_text}\n\n"
            "Respond with EXACTLY this JSON shape (same roles, same order, same dates -- only bullets change):\n"
            '[{"role": "...", "company": "...", "location": "...", "date": "...", "bullets": ["...", "..."]}]\n'
            "Do not wrap the output in markdown code fences."
        ),
        temperature=0.3,
    )
    return parse_json_response(raw)


def _verify_ats_score(experience: list, jd_text: str) -> dict:
    llm = get_llm_provider()
    raw = llm.complete_json(
        system="You are an ATS parser. You return only raw JSON.",
        prompt=(
            "Score how well this candidate experience matches the job description (0-100), and list any "
            "high-priority JD keywords still missing from the experience.\n\n"
            f"Candidate Experience:\n{json.dumps(experience, indent=2)}\n\n"
            f"Job Description:\n{jd_text}\n\n"
            'Respond with EXACTLY this JSON shape: {"score": 90, "missing_keywords": ["Spark", "Kubernetes"]}\n'
            "Do not wrap the output in markdown code fences."
        ),
        temperature=0.2,
    )
    return parse_json_response(raw)


def _refine_experience_pass(experience: list, jd_text: str, missing_keywords: list) -> list:
    llm = get_llm_provider()
    raw = llm.complete_json(
        system="You are an expert resume writer. You return only raw JSON.",
        prompt=(
            "Refine these experience bullets to naturally and genuinely weave in the following missing "
            "keywords, without fabricating anything. Only add a keyword where it genuinely fits an existing "
            "achievement (e.g. only mention 'Docker' if a bullet already involves containerization/deployment).\n\n"
            f"Missing Keywords: {json.dumps(missing_keywords)}\n\n"
            f"Current Experience:\n{json.dumps(experience, indent=2)}\n\n"
            f"Job Description:\n{jd_text}\n\n"
            "Respond with EXACTLY the same JSON shape as the input (same roles, same order, same dates -- only "
            "bullets change). Do not wrap the output in markdown code fences."
        ),
        temperature=0.3,
    )
    return parse_json_response(raw)


def run_multi_pass_tailoring(experience: list, jd_text: str) -> tuple[list, int, list, list]:
    """The real tailor -> verify -> refine loop. Returns
    (final_experience, final_score, initial_missing_keywords,
    remaining_missing_keywords) -- the score is whatever the LAST
    verify pass actually reported, never a placeholder. initial_missing
    (the gap BEFORE any refinement) lets the caller detect fabrication:
    any keyword that moved from "missing" to "resolved" needs checking
    against the candidate's real, untouched profile -- see
    _find_unsupported_keywords()."""
    tailored = _tailor_experience_pass(experience, jd_text)
    verification = _verify_ats_score(tailored, jd_text)
    score = int(verification.get("score", 0))
    missing = verification.get("missing_keywords", [])
    initial_missing = list(missing)

    passes = 0
    while score < TARGET_ATS_SCORE and missing and passes < MAX_REFINE_PASSES:
        tailored = _refine_experience_pass(tailored, jd_text, missing)
        verification = _verify_ats_score(tailored, jd_text)
        score = int(verification.get("score", score))
        missing = verification.get("missing_keywords", [])
        passes += 1

    return tailored, score, initial_missing, missing


def _find_unsupported_keywords(original_profile_content: dict, resolved_keywords: list, extra_text: str = "") -> list:
    """Keywords the refinement loop (or cover letter) claims to have
    resolved/used that don't appear ANYWHERE in the candidate's real,
    untouched profile -- a strong fabrication signal, since there'd be
    no genuine source material for the LLM to have drawn from. Checked
    mechanically (substring match) rather than by another LLM call, so
    it can't be fooled by the same failure mode it's checking for."""
    haystack = (json.dumps(original_profile_content) + " " + extra_text).lower()
    return [kw for kw in resolved_keywords if kw.lower() not in haystack]


def _tailor_summary_skills_projects(profile_content: dict, jd_text: str) -> dict:
    llm = get_llm_provider()
    raw = llm.complete_json(
        system="You are an expert resume writer. You return only raw JSON.",
        prompt=(
            "Tailor this candidate's professional summary, skills grouping, and project selection to align "
            "with the job description. Select exactly 3 projects from the candidate's project list that are "
            "most relevant, and rewrite each into 2 concise, metrics-driven bullets. Do not fabricate anything.\n\n"
            f"Candidate Profile:\n{json.dumps(profile_content, indent=2)}\n\n"
            f"Job Description:\n{jd_text}\n\n"
            "Respond with EXACTLY this JSON shape:\n"
            "{\n"
            '  "summary": "tailored professional summary",\n'
            '  "skills": { ...same grouping keys as the input profile skills... },\n'
            '  "projects": [{"name": "...", "bullets": ["...", "..."], "technologies": ["..."]}]\n'
            "}\n"
            "projects must contain exactly 3 items. Do not wrap the output in markdown code fences."
        ),
        temperature=0.3,
    )
    return parse_json_response(raw)


def clean_cover_letter(text: str) -> str:
    """Strip any greeting/sign-off lines the LLM added despite
    instructions -- the print template supplies its own."""
    if not text:
        return text
    lines = text.strip().split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip().lower()
        if not stripped:
            cleaned.append("")
            continue
        if stripped.startswith(("dear", "to the", "hello", "hi ", "attention:")):
            continue
        if stripped.startswith(("sincerely", "best regards", "warm regards", "respectfully", "thank you for your time")):
            continue
        cleaned.append(line)
    result = "\n".join(cleaned).strip()
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    return result


def generate_cover_letter(profile_content: dict, company_name: str, job_title: str, jd_text: str) -> str:
    projects_context = ""
    for proj in profile_content.get("projects", []):
        bullets = proj.get("bullets") or ([proj["description"]] if "description" in proj else [])
        projects_context += f"- {proj.get('name', '')}: {' '.join(bullets)}\n"

    llm = get_llm_provider()
    text = llm.complete_text(
        system=(
            "You are a professional cover letter writer. You return ONLY the 3 body paragraphs -- no "
            "greeting, no closing, no signature, since the template supplies those."
        ),
        prompt=(
            "Write an extremely professional, compelling, tailored cover letter (exactly 3 paragraphs, under "
            "300 words total) for this candidate applying to this job.\n\n"
            f"Candidate: {profile_content.get('name')}\n"
            f"Profile: {profile_content.get('summary')}\n"
            f"Recent Projects:\n{projects_context}\n\n"
            f"Target Company: {company_name}\n"
            f"Target Role: {job_title}\n"
            f"Job Description:\n{jd_text}\n\n"
            "Paragraph 1: direct hook expressing strong, specific interest in this role at this company.\n"
            "Paragraph 2: map 1-2 of the candidate's most relevant projects/experience directly to the JD's "
            "technical requirements, with concrete impact.\n"
            "Paragraph 3: reiterate value, professional call-to-action.\n\n"
            "No generic template phrases ('I am excited to apply', 'please find my resume attached'). No "
            "greetings or sign-offs -- start directly with paragraph 1's body text."
        ),
        temperature=0.5,
        max_tokens=700,
    )
    return clean_cover_letter(text)


def score_cover_letter(cover_letter_text: str, jd_text: str) -> int:
    """Independent scoring pass -- deliberately not derived from the
    resume's ATS score, since the two can diverge."""
    llm = get_llm_provider()
    raw = llm.complete_json(
        system="You are an expert hiring manager evaluating cover letters. You return only raw JSON.",
        prompt=(
            "Score how compelling and well-targeted this cover letter is for the job description below, on "
            "a 0-100 scale. Consider specificity, relevance to the JD's actual requirements, and whether it "
            "reads as genuine rather than generic.\n\n"
            f"Cover Letter:\n{cover_letter_text}\n\n"
            f"Job Description:\n{jd_text}\n\n"
            'Respond with EXACTLY this JSON shape: {"score": 85}\n'
            "Do not wrap the output in markdown code fences."
        ),
        temperature=0.2,
    )
    return int(parse_json_response(raw).get("score", 0))


def _upsert_document(db: Session, application_id: int, document_type: str, content: str, ats_score: int = None):
    doc = (
        db.query(TailoredDocument)
        .filter(TailoredDocument.application_id == application_id, TailoredDocument.document_type == document_type)
        .first()
    )
    if not doc:
        doc = TailoredDocument(application_id=application_id, document_type=document_type)
        db.add(doc)
    doc.content = content
    doc.ats_score = ats_score
    db.commit()
    return doc


def tailor_application(db: Session, application_id: int) -> JobApplication:
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise MatchingServiceError(f"Application {application_id} not found.")
    if application.status in _FINAL_STATUSES:
        raise MatchingServiceError(
            f"Application is '{application.status}' -- can't re-tailor a finalized application."
        )

    profile_content, variant_id = get_profile_content_for_application(db, application)
    posting = application.posting
    jd_text = posting.job_description

    try:
        tailored_experience, final_score, initial_missing, remaining_missing = run_multi_pass_tailoring(
            profile_content.get("experience", []), jd_text
        )
        extras = _tailor_summary_skills_projects(profile_content, jd_text)
    except Exception as e:
        raise MatchingServiceError(f"Resume tailoring failed: {e}") from e

    resolved_keywords = [kw for kw in initial_missing if kw not in remaining_missing]
    unsupported = _find_unsupported_keywords(profile_content, resolved_keywords)

    resume_doc = {
        "name": profile_content.get("name"),
        "title": profile_content.get("title"),
        "contact": profile_content.get("contact"),
        "summary": extras.get("summary", profile_content.get("summary")),
        "skills": extras.get("skills", profile_content.get("skills")),
        "experience": tailored_experience,
        "projects": extras.get("projects", profile_content.get("projects", []))[:3],
        "education": profile_content.get("education", []),
        "certifications": profile_content.get("certifications", []),
    }
    _upsert_document(db, application.id, "resume", json.dumps(resume_doc, indent=2), ats_score=final_score)

    try:
        cl_text = generate_cover_letter(
            profile_content, posting.company_name_raw, posting.job_title, jd_text
        )
        cl_score = score_cover_letter(cl_text, jd_text)
    except Exception as e:
        raise MatchingServiceError(f"Cover letter generation failed: {e}") from e

    _upsert_document(db, application.id, "cover_letter", cl_text, ats_score=cl_score)

    # Independent check: which JD keywords does the cover letter (a
    # separate LLM call/prompt from the resume loop) actually claim,
    # and are those claims backed by the real profile? Checked against
    # initial_missing (not `unsupported`) so this can't just be a
    # subset of what the resume check already found.
    cl_mentioned = [kw for kw in initial_missing if kw.lower() in cl_text.lower()]
    cl_unsupported = _find_unsupported_keywords(profile_content, cl_mentioned)
    all_unsupported = sorted(set(unsupported) | set(cl_unsupported))

    application.match_score = final_score
    application.cover_letter_score = cl_score
    application.profile_variant_id = variant_id
    application.status = "Tailored"

    if all_unsupported:
        application.attention_reason = (
            "Possible fabrication: the AI tailoring may have claimed experience with "
            f"{', '.join(all_unsupported)} that doesn't appear anywhere in your real profile. "
            "Review the tailored resume/cover letter before using them."
        )[:250]
        log_activity(
            db,
            f"FABRICATION WARNING on '{posting.job_title}' at {posting.company_name_raw}: "
            f"unsupported keywords {all_unsupported} -- resume/cover letter need manual review.",
            "WARNING",
        )
    else:
        application.attention_reason = None

    db.commit()

    log_activity(
        db,
        f"Tailored '{posting.job_title}' at {posting.company_name_raw}: "
        f"resume {final_score}%, cover letter {cl_score}%.",
        "INFO",
    )

    # Phase 4: hand off to the confirmation queue -- routes to Needs
    # Review if the fabrication check above (or Phase 2's scam-pattern
    # check) flagged anything, otherwise into a timed Pending
    # Confirmation with a notification. Import here, not at module
    # scope, to keep tailoring_service usable standalone / in tests
    # without pulling in the notification/email stack.
    from .confirmation_service import evaluate_and_enqueue
    evaluate_and_enqueue(db, application.id)
    db.refresh(application)

    return application
