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

The debrief itself has three deliberately-scoped inputs beyond the raw
transcript:
- Filler-word counts and response timing, computed mechanically here
  (regex + turn timestamps already on hand) -- the one category actual
  research on this consistently calls reliable, unlike facial/tonal
  inference. No new capture needed; the data already exists.
- Visual metrics, only when camera_enabled: a face-forward ratio and a
  movement count, aggregated entirely client-side (MediaPipe, in the
  browser) and submitted once at end_session as two numbers -- never a
  video frame. Framed to the model as observable facts ("looked at the
  camera N% of the time"), explicitly NOT as emotion/confidence
  inference, which real research (and the EU AI Act's employment-context
  ban on it) treats as unreliable even before the ethical concerns.
- A comparison against the candidate's most recent prior COMPLETED
  session for the SAME round (if any), including its scorecard -- so
  the debrief can call out genuine improvement or decline instead of
  each session existing in isolation. Tier changes between sessions are
  passed through explicitly so the model doesn't score a harder tier as
  "you got worse" just because the bar moved.

On-demand and real-LLM-cost per turn, same posture as everywhere else
in this app -- every candidate response is one real LLM call. No
extra guardrail beyond the existing pattern (human-clicked, not
automatic) since that's already this codebase's standard cost control.
"""

import json
import random
import re

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


_FILLER_WORD_PATTERN = re.compile(r"\b(um+|uh+|erm+|like|you know|sort of|kind of|basically|actually)\b", re.IGNORECASE)


def _delivery_stats(turns: list[MockInterviewTurn]) -> str:
    """Mechanical (non-LLM), computed straight from data already on
    hand -- no new capture. Filler-word counting is the one delivery
    metric research consistently treats as reliable (plain counting,
    no inference); response timing uses the turn timestamps that
    already exist. Returned as a formatted block for the debrief
    prompt, not a hard pass/fail judgment -- the model still interprets
    what a given count/pace actually means in context."""
    lines = []
    for i, t in enumerate(turns):
        if t.speaker != "candidate":
            continue
        fillers = _FILLER_WORD_PATTERN.findall(t.content)
        word_count = len(t.content.split())
        elapsed = ""
        if i > 0:
            delta = (t.created_at - turns[i - 1].created_at).total_seconds()
            if 0 < delta < 3600:  # sanity bound -- a stale/resumed session shouldn't skew this
                elapsed = f", ~{int(delta)}s to respond"
        lines.append(f"- Answer {i}: {word_count} words{elapsed}, {len(fillers)} filler word(s) ({', '.join(fillers) or 'none'})")
    return "\n".join(lines)


def _find_previous_session(db: Session, session: MockInterviewSession) -> MockInterviewSession | None:
    return (
        db.query(MockInterviewSession)
        .filter(
            MockInterviewSession.application_id == session.application_id,
            MockInterviewSession.round_name == session.round_name,
            MockInterviewSession.status == "completed",
            MockInterviewSession.id != session.id,
        )
        .order_by(MockInterviewSession.started_at.desc())
        .first()
    )


def start_session(
    db: Session, application_id: int, round_name: str, tier: str, camera_enabled: bool = False
) -> MockInterviewSession:
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

    session = MockInterviewSession(
        application_id=application_id, round_name=round_name, tier=tier, camera_enabled=camera_enabled,
    )
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
    wrap_instruction = (
        "The conversation has run a realistic length for this round -- if it feels natural, begin "
        "wrapping up rather than opening a new thread."
        if should_consider_wrap else ""
    )

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
            f"{wrap_instruction}\n"
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


def end_session(db: Session, session_id: int, visual_metrics: dict = None) -> MockInterviewSession:
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

    if session.camera_enabled and visual_metrics:
        session.visual_metrics_json = json.dumps(visual_metrics)

    visual_block = ""
    if session.camera_enabled and visual_metrics and visual_metrics.get("frames_analyzed"):
        face_forward_pct = round(
            100 * visual_metrics.get("frames_face_forward", 0) / visual_metrics["frames_analyzed"]
        )
        visual_block = (
            f"\nCamera feedback was on for this session. Observed (client-side, no video retained): "
            f"face oriented toward the camera {face_forward_pct}% of the time; "
            f"{visual_metrics.get('movement_events', 0)} noticeable movement/shift events over the session. "
            "Report these as plain observed facts, never as an inferred emotional state (not \"nervous\" or "
            "\"confident\" -- just what was observed and what real interview guidance suggests about it).\n"
        )
    elif session.camera_enabled:
        visual_block = "\nCamera feedback was enabled but no usable data was captured for this session.\n"

    previous = _find_previous_session(db, session)
    comparison_block = ""
    if previous and previous.debrief_json:
        prev_debrief = json.loads(previous.debrief_json)
        if previous.tier == session.tier:
            tier_change_note = "same tier as now"
        else:
            tier_change_note = (
                "a different tier than now -- account for this, a similar score at a HARDER tier "
                "is real improvement, not a plateau"
            )
        comparison_block = (
            f"\nThe candidate has a previous COMPLETED session for this SAME round, at "
            f"{TIER_DESCRIPTIONS.get(previous.tier, (previous.tier,))[0]} tier ({tier_change_note}). "
            f"Its scorecard was: {json.dumps(prev_debrief.get('scorecard', {}))}. "
            "Compare honestly against it in the comparison field below.\n"
        )

    llm = get_llm_provider()
    raw = llm.complete_json(
        system="You are an expert interview coach reviewing a completed practice interview. You return only raw JSON.",
        prompt=(
            f"Review this practice interview transcript for the '{session.round_name}' round "
            f"({TIER_DESCRIPTIONS[session.tier][0]} tier).\n\n"
            f"Transcript:\n{_format_transcript(turns)}\n\n"
            f"Delivery data (computed directly, not estimated):\n{_delivery_stats(turns)}\n"
            f"{visual_block}{comparison_block}\n"
            f"Candidate's Real Profile (for accuracy-checking their answers):\n{json.dumps(profile_content, indent=2)}\n\n"
            "Respond with EXACTLY this JSON shape:\n"
            "{\n"
            '  "accuracy_notes": ["anything the candidate said that is unsupported by or contradicts their real profile -- empty list if nothing found"],\n'
            '  "strengths": ["what genuinely landed well"],\n'
            '  "areas_to_improve": [\n'
            '    {"issue": "short label", "what_you_said": "the actual moment, quoted or closely paraphrased", "why_it_matters": "...", "example_better_answer": "a concrete rewrite grounded in the real profile above -- not generic advice"}\n'
            "  ],\n"
            '  "delivery_feedback": "1-2 sentences on pace/filler words/response length, grounded in the delivery data above",\n'
            '  "scorecard": {"communication_clarity": 1, "content_accuracy": 1, "structure": 1, "confidence_and_directness": 1},\n'
            '  "comparison": {"has_previous": true or false, "trend": "improved" or "same" or "declined" or "n/a", "note": "specific, honest comparison if has_previous else empty string", "warning": "explicit warning text ONLY if trend is declined on something meaningful, else empty string"},\n'
            '  "overall_summary": "2-3 sentences, direct and honest, like a real post-interview self-review"\n'
            "}\n"
            "Score the scorecard honestly on a real 1-5 scale -- do not default to inflated scores the way a "
            "generic assistant would; a real hiring-caliber interviewer's scoring penalizes vagueness and "
            "rewards concrete, specific answers. Every example_better_answer must be grounded in the real "
            "profile, never inventing an achievement. Be honest throughout, not just encouraging -- a real "
            "interviewer's silence on a weak answer doesn't mean it was strong. Do not wrap the output in "
            "markdown code fences."
        ),
        temperature=0.3,
        max_tokens=3000,
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
