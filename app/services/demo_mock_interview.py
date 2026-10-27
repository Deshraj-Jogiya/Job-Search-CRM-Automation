"""Mock interview by rules, for the public showcase instance (no AI provider there).

The interviewer's next line and the debrief normally come from the LLM (mock_interview_service). Here they come from
plain checks on the typed answer. Follow-ups ask for the part a strong answer would have and this one lacks (detail,
a measured result, the candidate's own actions). The debrief counts what the answers contain, checks every number
against the profile, and offers the prep's own drafted answer (itself quoted from the profile) as the better version.
Every check is listed in the debrief so a visitor can see why a score is what it is.
"""
import json
import re

_NUMBER = re.compile(r"\d[\d,.]*\s?(?:%|gb|tb|m\b|k\b|x\b)?", re.I)
_FIRST_PERSON = re.compile(r"\b(i|i'm|i've|i'd|my|me)\b", re.I)
_TEAM_ONLY = re.compile(r"\b(we|our|the team)\b", re.I)
_HEDGES = re.compile(r"\b(i think|maybe|probably|i guess|sort of|kind of|hopefully)\b", re.I)
_FILLERS = re.compile(r"\b(um+|uh+|erm+|like|you know|basically|actually)\b", re.I)
_MAX_ANSWERS = 5

FOLLOW_UPS = {
    "short": "Can you walk me through that in more detail? What was the situation, and what did you do?",
    "no_number": "What was the result, and how did you measure it?",
    "team_only": "What did you do yourself, as opposed to the team?",
}


def _gap(answer: str) -> str | None:
    if len(answer.split()) < 40:
        return "short"
    if not _NUMBER.search(answer):
        return "no_number"
    if _TEAM_ONLY.search(answer) and not _FIRST_PERSON.search(answer):
        return "team_only"
    return None


def next_line(answer: str, last_was_followup: bool, answers_so_far: int, remaining: list[str]) -> dict:
    """The interviewer's next turn: one follow-up per question at most, then the next prepared question."""
    gap = None if last_was_followup else _gap(answer)
    if gap:
        return {"next_line": FOLLOW_UPS[gap], "is_followup": True, "suggest_level_up": False, "level_up_note": ""}
    if answers_so_far >= _MAX_ANSWERS or not remaining:
        line = "That covers what I wanted to ask. End the session for your debrief."
    else:
        line = remaining[0]
    return {"next_line": line, "is_followup": False, "suggest_level_up": False, "level_up_note": ""}


def _score(value: float, cuts: tuple) -> int:
    """1-5 from four ascending cut points."""
    return 1 + sum(value >= c for c in cuts)


def debrief(turns: list, profile: dict, prep_round: dict, previous: dict | None) -> dict:
    profile_text = json.dumps(profile).lower()
    drafted = {qa["question"]: qa.get("draft_answer") for qa in prep_round.get("qa_pairs", [])}
    pairs, question = [], None
    for t in turns:
        if t.speaker == "interviewer" and not t.is_followup:  # a follow-up's answer belongs to the main question
            question = t.content
        elif question:
            pairs.append((question, t.content, t))

    unsupported, strengths, improve = [], [], []
    hedges = fillers = words = with_number = with_first_person = 0
    for q, a, _ in pairs:
        n_words = len(a.split())
        words += n_words
        hedges += len(_HEDGES.findall(a))
        fillers += len(_FILLERS.findall(a))
        numbers = [n.strip().lower() for n in _NUMBER.findall(a)]
        with_number += bool(numbers)
        with_first_person += bool(_FIRST_PERSON.search(a))
        for n in numbers:
            digits = re.sub(r"[^\d.]", "", n).strip(".")
            if digits and digits not in profile_text:
                unsupported.append(f"You said \"{n}\"; that number is not in the profile. Use only figures you can back up.")
        gap = _gap(a)
        if gap is None and n_words <= 250:
            strengths.append(f"\"{q}\": a concrete answer with your own actions and a number ({n_words} words).")
        elif q in drafted or gap:
            issue = {"short": "Too short to judge", "no_number": "No measured result",
                     "team_only": "Only what the team did", None: "Ran long"}[gap if gap else None]
            improve.append({
                "issue": issue,
                "what_you_said": a[:160] + ("..." if len(a) > 160 else ""),
                "why_it_matters": {"Too short to judge": "Interviewers score evidence; a short answer gives none.",
                                   "No measured result": "A number is what makes the result believable.",
                                   "Only what the team did": "They are hiring you, so they need your part.",
                                   "Ran long": "Past about 250 words the point gets lost."}[issue],
                "example_better_answer": drafted.get(q) or "Situation in one line, what you did, and the result "
                                                           "with a number from your own work.",
            })

    n = max(1, len(pairs))
    avg_words = words / n
    scorecard = {
        "communication_clarity": max(1, _score(avg_words, (25, 45, 70, 100)) - (fillers > 2 * n)),
        "content_accuracy": max(1, 5 - len(unsupported)),
        "structure": _score((with_number + with_first_person) / (2 * n), (0.25, 0.5, 0.75, 1.0)),
        "confidence_and_directness": max(1, 5 - hedges),
    }
    total = sum(scorecard.values())
    comparison = {"has_previous": False, "trend": "n/a", "note": "", "warning": ""}
    if previous and previous.get("scorecard"):
        before = sum(previous["scorecard"].values())
        trend = "improved" if total > before else "declined" if total < before else "same"
        comparison = {"has_previous": True, "trend": trend,
                      "note": f"Scorecard total {before} last time, {total} now.",
                      "warning": "Lower than last time; read the areas to improve below." if trend == "declined" else ""}
    return {
        "accuracy_notes": unsupported,
        "strengths": strengths,
        "areas_to_improve": improve,
        "delivery_feedback": f"{len(pairs)} answer(s), {avg_words:.0f} words on average, {fillers} filler word(s), "
                             f"{hedges} hedge(s) like \"I think\" or \"maybe\".",
        "scorecard": scorecard,
        "comparison": comparison,
        "overall_summary": "Scored by rules in this demo: clarity from answer length and filler words, accuracy from "
                           "checking every number against the profile, structure from whether answers carry a "
                           "number and your own actions, directness from hedges. "
                           f"{with_number} of {len(pairs)} answer(s) had a measured result.",
        "scored_by_rules": True,
    }
