"""Real feature request from 2026-08-23 ("record how I answer, reuse
automatically in rotation") -- scoped then, never built. Most "custom
questions" a per-JD LLM call drafts across different employers' forms
are the same handful of recurring questions (visa sponsorship, work
authorization, desired salary, notice period) worded differently, not
genuinely novel each time. Checked BEFORE the LLM draft call in every
ATS autofill module (greenhouse/lever/ashby_autofill.py's
_draft_custom_answers) -- an already-answered question costs nothing
the second time it's seen.

Deliberately conservative, same posture as extension_service.py's
_best_option_match: this is answering real application questions on the
candidate's behalf, so a wrong reuse (answering the WRONG question with
a cached answer) is worse than just asking the LLM again. Exact
normalized match is always safe. The fuzzy fallback only fires above a
high similarity threshold, and only when exactly one stored question
clears it -- real ambiguity (two different stored questions both
plausibly matching) is treated as no match at all, never a guess."""

import re
from difflib import SequenceMatcher

from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import SavedAnswer

_WHITESPACE_RE = re.compile(r"\s+")
_PUNCTUATION_RE = re.compile(r"[^\w\s]")

# Conservative on purpose -- see module docstring. 0.90 was chosen to
# safely catch real wording variants of the same question (e.g. "Are you
# legally authorized to work in the US?" vs "Are you legally authorized
# to work in the United States?") while staying well clear of two
# genuinely different questions about similar topics (e.g. work
# authorization vs visa sponsorship timeline) ever being conflated.
_FUZZY_MATCH_THRESHOLD = 0.90

# Never worth caching -- the LLM's own explicit "I can't answer this
# from the profile" signal, not a real answer.
_REVIEW_NEEDED_MARKER = "[REVIEW NEEDED]"


def _normalize(question_text: str) -> str:
    text = (question_text or "").strip().lower()
    text = _PUNCTUATION_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def find_saved_answer(db: Session, question_text: str) -> str | None:
    normalized = _normalize(question_text)
    if not normalized:
        return None

    exact = db.query(SavedAnswer).filter(SavedAnswer.question_normalized == normalized).first()
    if exact:
        exact.times_reused = (exact.times_reused or 0) + 1
        exact.last_used_at = utcnow()
        db.commit()
        return exact.answer_text

    candidates = db.query(SavedAnswer).all()
    if not candidates:
        return None

    scored = [
        (SequenceMatcher(None, normalized, c.question_normalized).ratio(), c)
        for c in candidates
    ]
    above_threshold = [(score, c) for score, c in scored if score >= _FUZZY_MATCH_THRESHOLD]
    if len(above_threshold) != 1:
        # Zero matches, or real ambiguity (more than one candidate
        # plausibly the same question) -- never guess between them.
        return None

    _, match = above_threshold[0]
    match.times_reused = (match.times_reused or 0) + 1
    match.last_used_at = utcnow()
    db.commit()
    return match.answer_text


def record_answer(db: Session, question_text: str, answer_text: str) -> None:
    if not answer_text or answer_text.strip().upper() == _REVIEW_NEEDED_MARKER.upper():
        return
    normalized = _normalize(question_text)
    if not normalized:
        return

    existing = db.query(SavedAnswer).filter(SavedAnswer.question_normalized == normalized).first()
    if existing:
        existing.answer_text = answer_text  # the real, current answer wins over a stale one
        db.commit()
        return

    db.add(SavedAnswer(question_text=question_text, question_normalized=normalized, answer_text=answer_text))
    db.commit()
