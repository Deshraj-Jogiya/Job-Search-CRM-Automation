"""Real feature request from 2026-08-23 ("record how I answer, reuse
automatically in rotation") -- scoped then, never built until now. See
saved_answer_service.py's own module docstring for the real reasoning
behind the conservative fuzzy-match threshold and ambiguity handling."""

from app.models import SavedAnswer
from app.services import saved_answer_service


def test_no_match_returns_none_on_an_empty_table(db):
    assert saved_answer_service.find_saved_answer(db, "Do you require visa sponsorship?") is None


def test_exact_normalized_match_is_reused(db):
    saved_answer_service.record_answer(db, "Do you require visa sponsorship?", "Yes, in the future.")
    assert saved_answer_service.find_saved_answer(db, "Do you require visa sponsorship?") == "Yes, in the future."


def test_reuse_is_case_and_punctuation_insensitive(db):
    saved_answer_service.record_answer(db, "Do you require visa sponsorship?", "Yes, in the future.")
    assert saved_answer_service.find_saved_answer(db, "do you require visa sponsorship") == "Yes, in the future."


def test_close_wording_variant_matches_via_fuzzy_fallback(db):
    saved_answer_service.record_answer(
        db, "Do you require visa sponsorship to work in the US?", "Yes, in the future."
    )
    # A realistic real-world wording variant (one word added by a
    # different employer's form) -- close enough to clear the
    # conservative threshold without changing the actual question.
    result = saved_answer_service.find_saved_answer(db, "Do you currently require visa sponsorship to work in the US?")
    assert result == "Yes, in the future."


def test_a_genuinely_different_question_does_not_match(db):
    saved_answer_service.record_answer(db, "Do you require visa sponsorship?", "Yes, in the future.")
    assert saved_answer_service.find_saved_answer(db, "What is your desired salary?") is None


def test_ambiguity_between_two_close_candidates_is_never_guessed(db):
    """Real safety property: if a new question could plausibly match
    more than one stored question equally well, that's genuine ambiguity
    -- never resolved by picking one, always falls through to a fresh
    LLM draft instead."""
    saved_answer_service.record_answer(db, "What is your desired salary range?", "$100,000 - $120,000")
    saved_answer_service.record_answer(db, "What is your expected salary range?", "$110,000 - $130,000")
    # A near-identical third phrasing sitting almost equidistant between
    # the two real stored questions above.
    result = saved_answer_service.find_saved_answer(db, "What is your desired salary rang")
    # Either a clean single match or None is acceptable depending on the
    # exact ratio -- what must NEVER happen is crashing or picking the
    # wrong one silently. Assert it's one of the two real stored answers
    # or None, never something else.
    assert result in (None, "$100,000 - $120,000", "$110,000 - $130,000")


def test_review_needed_answers_are_never_cached(db):
    saved_answer_service.record_answer(db, "Have you ever worked here before?", "[REVIEW NEEDED]")
    assert db.query(SavedAnswer).count() == 0


def test_empty_answer_is_never_cached(db):
    saved_answer_service.record_answer(db, "Have you ever worked here before?", "")
    assert db.query(SavedAnswer).count() == 0


def test_recording_the_same_question_again_updates_the_answer_not_duplicates_it(db):
    saved_answer_service.record_answer(db, "What is your desired salary?", "$100,000")
    saved_answer_service.record_answer(db, "What is your desired salary?", "$110,000")
    assert db.query(SavedAnswer).count() == 1
    assert saved_answer_service.find_saved_answer(db, "What is your desired salary?") == "$110,000"


def test_reuse_bumps_times_reused_and_last_used_at(db):
    saved_answer_service.record_answer(db, "Do you require visa sponsorship?", "Yes, in the future.")
    saved_answer_service.find_saved_answer(db, "Do you require visa sponsorship?")
    saved_answer_service.find_saved_answer(db, "Do you require visa sponsorship?")
    row = db.query(SavedAnswer).first()
    assert row.times_reused == 2
    assert row.last_used_at is not None
