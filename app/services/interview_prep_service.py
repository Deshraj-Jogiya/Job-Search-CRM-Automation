"""
Interview prep generation. Independent pieces:

- General prep: likely questions and talking points grounded in the
  candidate's real profile -- the kind of prep that applies to this
  type of role regardless of which company it's at.
- Company-specific prep: questions and talking points shaped by this
  JD and, when Tavily is configured, a few real, current facts about
  the company -- not the LLM's own (possibly stale or invented)
  knowledge of it. Same "discover real info, don't guess" posture as
  contact_discovery_service.py, which is where tavily_search() lives.
- Process research: same posture applied to the interview PROCESS
  itself, not just company facts -- real candidates' reported rounds
  and format (Glassdoor/Blind/etc-style sources, via Tavily), because
  a company like a consulting firm running case interviews + a
  Personal-Experience-Interview needs very different prep than a
  startup running a take-home + one call. Falls back to a clearly-
  labeled generic structure when nothing real is found, same as every
  other optional integration here.
- Predicted rounds: restructures the above into an ordered, round-by-
  round plan instead of one flat list, so prep reads as "here's what
  round 2 actually tests" rather than an undifferentiated question dump.

On-demand (a button on the application detail page), not automatic --
same real-LLM-cost reasoning as scoring/tailoring elsewhere in this
app. Available for any application except Rejected -- prepping for a
job you've already passed on isn't useful, but there's no need for a
stricter gate than that (mirrors the on-demand posture of score/tailor,
not a hard-stop pattern like the confirmation queue).
"""

import json

from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import InterviewPrep, JobApplication
from .activity_logger import log_activity
from .contact_discovery_service import is_tavily_configured, tavily_search
from .llm import get_llm_provider, parse_json_response
from .matching_service import MatchingServiceError, get_profile_content_for_application


class InterviewPrepServiceError(Exception):
    """User-facing failure -- callers show the message instead of a 500."""


def _light_company_research(db: Session, company_name: str) -> str:
    """Best-effort, never raises -- an empty string means the
    company-specific prompt falls back to JD-only grounding, same
    graceful-degrade posture as every other optional integration here."""
    if not is_tavily_configured():
        return ""
    try:
        results = tavily_search(db, f"{company_name} company news mission products", max_results=4)
    except Exception:
        return ""
    snippets = [r.get("content", "")[:400] for r in results if r.get("content")]
    return "\n\n".join(snippets)


def _research_interview_process(db: Session, company_name: str, job_title: str) -> dict:
    """Best-effort, never raises. Searches for real, reported interview
    rounds/format for this company+role -- distinct from
    _light_company_research, which looks for company facts, not process.
    Returns {"summary": str, "sources": [url, ...]}; an empty summary
    means the round-prediction prompt below falls back to a generic,
    clearly-labeled structure instead of guessing at this company's
    actual process."""
    if not is_tavily_configured():
        return {"summary": "", "sources": []}
    try:
        results = tavily_search(
            db,
            f"{company_name} {job_title} interview process rounds format questions candidate experience",
            max_results=5,
        )
    except Exception:
        return {"summary": "", "sources": []}
    snippets = [r.get("content", "")[:500] for r in results if r.get("content")]
    sources = [r.get("url") for r in results if r.get("url")]
    return {"summary": "\n\n".join(snippets), "sources": sources}


def _generate_predicted_rounds(job_title: str, company_name: str, jd_text: str, process_research: dict) -> dict:
    llm = get_llm_provider()
    if process_research.get("summary"):
        research_block = (
            "Real, reported information found about this company's actual interview process:\n"
            f"{process_research['summary']}\n\n"
        )
    else:
        research_block = (
            "No real information about this company's actual interview process was found -- use a "
            "reasonable generic structure (e.g. screen, technical, behavioral) and set "
            "grounded_in_real_research to false. Do not invent specific claims about this company's "
            "real process.\n\n"
        )
    raw = llm.complete_json(
        system="You are an expert interview coach. You return only raw JSON.",
        prompt=(
            f"Predict the realistic round-by-round interview structure a candidate should prepare for, "
            f"for {job_title} at {company_name}.\n\n{research_block}"
            f"Job Description:\n{jd_text}\n\n"
            "Respond with EXACTLY this JSON shape:\n"
            "{\n"
            '  "rounds": [\n'
            '    {"round_name": "...", "what_it_tests": "...", "prep_focus": ["...", "..."]}\n'
            "  ],\n"
            '  "grounded_in_real_research": true or false\n'
            "}\n"
            "3-6 rounds, ordered as they would realistically occur. 2-4 items per prep_focus list. "
            "Do not wrap the output in markdown code fences."
        ),
        temperature=0.4,
    )
    return parse_json_response(raw)


def _generate_general_prep(profile_content: dict, jd_text: str) -> dict:
    llm = get_llm_provider()
    raw = llm.complete_json(
        system="You are an expert interview coach. You return only raw JSON.",
        prompt=(
            "Based on this candidate's real background and the target role, generate interview prep that "
            "does NOT depend on which specific company this is -- the kind of questions and talking points "
            "that apply to this type of role generally.\n\n"
            f"Candidate Profile:\n{json.dumps(profile_content, indent=2)}\n\n"
            f"Target Role (job description):\n{jd_text}\n\n"
            "Respond with EXACTLY this JSON shape:\n"
            "{\n"
            '  "likely_questions": ["...", "..."],\n'
            '  "talking_points": ["...", "..."],\n'
            '  "strengths_to_emphasize": ["...", "..."],\n'
            '  "potential_gaps_to_address": ["...", "..."]\n'
            "}\n"
            "Ground every talking point/strength in something that genuinely appears in the candidate's "
            "profile -- do not invent experience. 5-8 items per list. Do not wrap the output in markdown "
            "code fences."
        ),
        temperature=0.4,
    )
    return parse_json_response(raw)


def _generate_company_prep(company_name: str, job_title: str, jd_text: str, research: str) -> dict:
    llm = get_llm_provider()
    research_block = (
        f"Recent real information found about the company:\n{research}\n\n"
        if research
        else "No external research available -- base this only on the job description below.\n\n"
    )
    raw = llm.complete_json(
        system="You are an expert interview coach. You return only raw JSON.",
        prompt=(
            f"Generate company-specific interview prep for a candidate interviewing for {job_title} at "
            f"{company_name}.\n\n"
            f"{research_block}"
            f"Job Description:\n{jd_text}\n\n"
            "Respond with EXACTLY this JSON shape:\n"
            "{\n"
            '  "company_context": "2-3 sentences on what this company does and what seems to matter to them right now",\n'
            '  "company_specific_questions": ["...", "..."],\n'
            '  "why_this_company_talking_points": ["...", "..."],\n'
            '  "questions_to_ask_them": ["...", "..."]\n'
            "}\n"
            "If no real research was provided, keep company_context general and clearly grounded in the JD "
            "only -- do not fabricate specific facts (funding, headcount, recent news) you were not given. "
            "4-6 items per list. Do not wrap the output in markdown code fences."
        ),
        temperature=0.4,
    )
    return parse_json_response(raw)


def generate_interview_prep(db: Session, application_id: int) -> JobApplication:
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise InterviewPrepServiceError(f"Application {application_id} not found.")
    if application.status == "Rejected":
        raise InterviewPrepServiceError("Can't generate interview prep for a Rejected application.")

    try:
        profile_content, _ = get_profile_content_for_application(db, application)
    except MatchingServiceError as e:
        raise InterviewPrepServiceError(str(e)) from e

    posting = application.posting
    jd_text = posting.job_description

    try:
        general = _generate_general_prep(profile_content, jd_text)
        research = _light_company_research(db, posting.company_name_raw)
        company = _generate_company_prep(posting.company_name_raw, posting.job_title, jd_text, research)
        process_research = _research_interview_process(db, posting.company_name_raw, posting.job_title)
        predicted_rounds = _generate_predicted_rounds(
            posting.job_title, posting.company_name_raw, jd_text, process_research
        )
    except Exception as e:
        raise InterviewPrepServiceError(f"Interview prep generation failed: {e}") from e

    prep = db.query(InterviewPrep).filter(InterviewPrep.application_id == application.id).first()
    if not prep:
        prep = InterviewPrep(application_id=application.id)
        db.add(prep)

    prep.general_prep_json = json.dumps(general)
    prep.company_prep_json = json.dumps(company)
    prep.process_research_json = json.dumps(process_research)
    prep.predicted_rounds_json = json.dumps(predicted_rounds)
    prep.generated_at = utcnow()
    db.commit()

    log_activity(
        db,
        f"Generated interview prep for '{posting.job_title}' at {posting.company_name_raw}"
        + (" (with live company + process research)." if research or process_research.get("summary")
           else " (JD-only -- no research configured)."),
        "INFO",
    )

    db.refresh(application)
    return application
