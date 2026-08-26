"""
Mock interview practice: the AI plays interviewer, not a question bank
the candidate browses. A session picks one predicted round (from
interview_prep_service's already-grounded predicted_rounds) and runs a
live back-and-forth -- the opening question is chosen at random from
that round's pool (qa_pairs + other_possible_questions, both already
passed through check_answer_grounding), and every subsequent turn
reacts to what the candidate actually said: a natural follow-up, or a
fresh question pulled from the same vetted pool, never an invented new
topic mid-conversation (a live follow-up reacting to the candidate's
own words carries far less fabrication risk than a freshly-invented
topic question would).

Three tiers (see TIER_DESCRIPTIONS) control how forgiving the session
is. The adaptive layer can flag that a candidate is ready to move up a
tier, but every escalation is candidate-accepted -- suggest_level_up
surfaces a note, it never silently changes the tier itself. This
mirrors interview_prep_service's own escalation-is-explicit posture
(grounding warnings are surfaced, not auto-corrected).

Grading happens once, at end_session, not turn-by-turn -- interrupting
a live conversation to score it breaks the realism the whole feature
exists to provide. The debrief reuses resolve_grounding_profile (same
tailored-resume-preferred source as prep generation) so feedback is
judged against the same material the interview prep itself was built
from.

On-demand and real-LLM-cost per turn, same posture as everywhere else
in this app -- every candidate response is one real LLM call. No
extra guardrail beyond the existing pattern (human-clicked, not
automatic) since that's already this codebase's standard cost control.
"""

import json
import random

from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import InterviewPrep, JobApplication, MockInterviewSession, MockInterviewTurn
from .activity_logger import log_activity
from .interview_prep_service import InterviewPrepServiceError, resolve_grounding_profile
from .llm import get_llm_provider, parse_json_response


class MockInterviewServiceError(Exception):
    """User-facing failure -- callers show the message instead of a 500."""


TIER_DESCRIPTIONS = {
    "warm_up": (
        "Warm-Up",
        "No time pressure. Pause, think out loud, take your time. This mode is for getting "
        "comfortable with the format itself, not being judged on your answers.",
    ),
    "guided": (
        "Guided Practice",
        "A steadier pace with real follow-ups, but still forgiving. Feels like an interview, "
        "with training wheels.",
    ),
    "full_simulation": (
        "Full Simulation",
        "Timed to the round's real length, natural unpredictable follow-ups, no do-overs -- "
        "as close to the actual thing as this can get.",
    ),
}

_MAX_TURNS_BEFORE_NATURAL_WRAP = 10  # roughly how many exchanges a real round this size would hold


def _get_round(db: Session, application_id: int, round_name: str) -> dict:
    prep = db.query(InterviewPrep).filter(InterviewPrep.application_id == application_id).first()
    if not prep or not prep.predicted_rounds_json:
        raise MockInterviewServiceError("Generate interview prep for this application first.")
    rounds = json.loads(prep.predicted_rounds_json).get("rounds", [])
    for r in rounds:
        if r.get("round_name") == round_name:
            return r
    raise MockInterviewServiceError(f"Round '{round_name}' not found in this application's interview prep.")


def _pool_questions(round_data: dict) -> list[str]:
    from_qa = [qa["question"] for qa in round_data.get("qa_pairs", []) if qa.get("question")]
    from_other = [q for q in round_data.get("other_possible_questions", []) if q]
    return from_qa + from_other


def _format_transcript(turns: list[MockInterviewTurn]) -> str:
    lines = []
    for t in turns:
        speaker = "Interviewer" if t.speaker == "interviewer" else "Candidate"
        lines.append(f"{speaker}: {t.content}")
    return "\n".join(lines)


def start_session(db: Session, application_id: int, round_name: str, tier: str) -> MockInterviewSession:
    if tier not in TIER_DESCRIPTIONS:
        raise MockInterviewServiceError(f"Unknown tier '{tier}'.")
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise MockInterviewServiceError(f"Application {application_id} not found.")

    round_data = _get_round(db, application_id, round_name)
    pool = _pool_questions(round_data)
    if not pool:
        raise MockInterviewServiceError(f"Round '{round_name}' has no questions to practice with.")

    opening_question = random.choice(pool)

    session = MockInterviewSession(application_id=application_id, round_name=round_name, tier=tier)
    db.add(session)
    db.flush()  # need session.id for the first turn's FK before commit

    db.add(MockInterviewTurn(session_id=session.id, turn_index=0, speaker="interviewer", content=opening_question))
    db.commit()
    db.refresh(session)

    log_activity(db, f"Started a {TIER_DESCRIPTIONS[tier][0]} mock interview for '{round_name}'.", "INFO")
    return session


def submit_answer(db: Session, session_id: int, candidate_answer: str) -> MockInterviewTurn:
    session = db.query(MockInterviewSession).filter(MockInterviewSession.id == session_id).first()
    if not session:
        raise MockInterviewServiceError(f"Session {session_id} not found.")
    if session.status != "in_progress":
        raise MockInterviewServiceError("This session has already ended.")

    turns = list(session.turns)
    next_index = len(turns)
    candidate_turn = MockInterviewTurn(
        session_id=session.id, turn_index=next_index, speaker="candidate", content=candidate_answer,
    )
    db.add(candidate_turn)
    turns.append(candidate_turn)

    round_data = _get_round(db, session.application_id, session.round_name)
    pool = _pool_questions(round_data)
    asked_already = {t.content for t in turns if t.speaker == "interviewer"}
    remaining_pool = [q for q in pool if q not in asked_already]

    tier_label, tier_description = TIER_DESCRIPTIONS[session.tier]
    turns_so_far = len([t for t in turns if t.speaker == "candidate"])
    should_consider_wrap = turns_so_far >= _MAX_TURNS_BEFORE_NATURAL_WRAP

    llm = get_llm_provider()
    raw = llm.complete_json(
        system=(
            "You are a real, professional interviewer conducting a live interview. You respond only "
            "with the interviewer's next line as raw JSON -- never break character, never explain "
            "yourself to the candidate."
        ),
        prompt=(
            f"Round: {session.round_name} ({round_data.get('what_it_tests', '')}). "
            f"Likely interviewer: {round_data.get('likely_interviewer', 'unknown')}.\n"
            f"Session tier: {tier_label} -- {tier_description}\n\n"
            f"Conversation so far:\n{_format_transcript(turns)}\n\n"
            f"Remaining vetted questions you can still draw from if you choose to move on:\n"
            + "\n".join(f"- {q}" for q in remaining_pool[:15]) + "\n\n"
            "Decide your next line as the interviewer: either a natural follow-up reacting to what the "
            "candidate just said, or a transition to one of the remaining questions above (verbatim or "
            "lightly rephrased to flow naturally) -- never invent a new topic question not in that list. "
            f"{'The conversation has run a realistic length for this round -- if it feels natural, begin '
               'wrapping up rather than opening a new thread.' if should_consider_wrap else ''}\n"
            "Also assess, honestly, whether the candidate is finding this tier comfortably easy based on "
            "their answers so far -- if so, note it; otherwise leave the note empty.\n\n"
            "Respond with EXACTLY this JSON shape:\n"
            "{\n"
            '  "next_line": "...",\n'
            '  "is_followup": true or false,\n'
            '  "suggest_level_up": true or false,\n'
            '  "level_up_note": "one short sentence telling the candidate why, only if suggest_level_up is true, else empty string"\n'
            "}\n"
            "Do not wrap the output in markdown code fences."
        ),
        temperature=0.6,
        max_tokens=600,
    )
    decision = parse_json_response(raw)

    interviewer_turn = MockInterviewTurn(
        session_id=session.id,
        turn_index=next_index + 1,
        speaker="interviewer",
        content=decision.get("next_line", "Can you tell me more about that?"),
        is_followup=bool(decision.get("is_followup")),
        suggest_level_up=bool(decision.get("suggest_level_up")),
        level_up_note=decision.get("level_up_note") or None,
    )
    db.add(interviewer_turn)
    db.commit()
    db.refresh(interviewer_turn)
    return interviewer_turn


def end_session(db: Session, session_id: int) -> MockInterviewSession:
    session = db.query(MockInterviewSession).filter(MockInterviewSession.id == session_id).first()
    if not session:
        raise MockInterviewServiceError(f"Session {session_id} not found.")
    if session.status == "completed":
        return session

    turns = list(session.turns)
    if not any(t.speaker == "candidate" for t in turns):
        raise MockInterviewServiceError("Answer at least one question before ending the session.")

    application = db.query(JobApplication).filter(JobApplication.id == session.application_id).first()
    try:
        profile_content, _, _ = resolve_grounding_profile(db, application)
    except InterviewPrepServiceError as e:
        raise MockInterviewServiceError(str(e)) from e

    llm = get_llm_provider()
    raw = llm.complete_json(
        system="You are an expert interview coach reviewing a completed practice interview. You return only raw JSON.",
        prompt=(
            f"Review this practice interview transcript for the '{session.round_name}' round "
            f"({TIER_DESCRIPTIONS[session.tier][0]} tier).\n\n"
            f"Transcript:\n{_format_transcript(turns)}\n\n"
            f"Candidate's Real Profile (for accuracy-checking their answers):\n{json.dumps(profile_content, indent=2)}\n\n"
            "Respond with EXACTLY this JSON shape:\n"
            "{\n"
            '  "accuracy_notes": ["anything the candidate said that is unsupported by or contradicts their real profile -- empty list if nothing found"],\n'
            '  "strengths": ["what genuinely landed well"],\n'
            '  "areas_to_improve": ["specific, actionable feedback"],\n'
            '  "structure_feedback": "1-2 sentences on clarity/structure (e.g. STAR usage for behavioral answers)",\n'
            '  "overall_summary": "2-3 sentences, direct and honest, like a real post-interview self-review"\n'
            "}\n"
            "Be honest, not just encouraging -- a real interviewer's silence on a weak answer doesn't mean "
            "it was strong. Do not wrap the output in markdown code fences."
        ),
        temperature=0.3,
        max_tokens=2000,
    )
    debrief = parse_json_response(raw)

    session.debrief_json = json.dumps(debrief)
    session.status = "completed"
    session.ended_at = utcnow()
    db.commit()
    db.refresh(session)

    log_activity(db, f"Completed a mock interview session for '{session.round_name}'.", "INFO")
    return session


def list_sessions(db: Session, application_id: int) -> list[MockInterviewSession]:
    return (
        db.query(MockInterviewSession)
        .filter(MockInterviewSession.application_id == application_id)
        .order_by(MockInterviewSession.started_at.desc())
        .all()
    )


def get_available_rounds(db: Session, application_id: int) -> list[dict]:
    prep = db.query(InterviewPrep).filter(InterviewPrep.application_id == application_id).first()
    if not prep or not prep.predicted_rounds_json:
        return []
    rounds = json.loads(prep.predicted_rounds_json).get("rounds", [])
    return [{"round_name": r.get("round_name"), "what_it_tests": r.get("what_it_tests")} for r in rounds]
