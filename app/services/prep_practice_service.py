"""
Review schedule for the general interview prep: questions, deep-dive cards, coding patterns and problems.

Why this shape: retrieval practice spread over time (spacing) is among the most reliable findings in learning
research, and the research that shaped the coding plan says a problem only counts as learned when it is solved cold
on a later day. So every item gets a box: "could not" sends it back to tomorrow, "shaky" repeats the current gap,
"solid" moves it to a longer gap (1, 3, 7, 21, 45 days). Showcase mode never writes here; the demo page keeps
practice in each visitor's browser with the same intervals (static/js/prep_practice.js).
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from ..models import PrepPractice, utcnow

INTERVAL_DAYS = (1, 3, 7, 21, 45)
RATINGS = {1: "Could not", 2: "Shaky", 3: "Solid"}


class PrepPracticeError(Exception):
    pass


def next_box(box: int, rating: int) -> int:
    if rating == 1:
        return 0
    if rating == 3:
        return min(box + 1, len(INTERVAL_DAYS) - 1)
    return box


def rate(db: Session, variant_id: int, card_key: str, rating: int) -> PrepPractice:
    if rating not in RATINGS:
        raise PrepPracticeError("Rating must be 1, 2 or 3.")
    if not card_key or len(card_key) > 120 or ":" not in card_key:
        raise PrepPracticeError("Unknown item.")
    row = db.query(PrepPractice).filter_by(variant_id=variant_id, card_key=card_key).first()
    if row is None:
        row = PrepPractice(variant_id=variant_id, card_key=card_key, box=0, reviews=0)
        db.add(row)
    now = utcnow()
    row.box = next_box(row.box or 0, rating)
    row.last_rating = rating
    row.reviews = (row.reviews or 0) + 1
    row.last_reviewed_at = now
    row.next_due_at = now + timedelta(days=INTERVAL_DAYS[row.box])
    db.commit()
    return row


def progress(db: Session, variant_id: int) -> dict[str, PrepPractice]:
    return {r.card_key: r for r in db.query(PrepPractice).filter_by(variant_id=variant_id).all()}


def due(db: Session, variant_id: int, valid_keys: set[str]) -> list[PrepPractice]:
    """Items practised before whose review date has come, weakest first. Keys that no longer exist are skipped."""
    now = utcnow()
    rows = (db.query(PrepPractice)
            .filter(PrepPractice.variant_id == variant_id, PrepPractice.next_due_at <= now)
            .order_by(PrepPractice.last_rating.asc(), PrepPractice.next_due_at.asc())
            .all())
    return [r for r in rows if r.card_key in valid_keys]


def summary(rows: dict[str, PrepPractice], keys: list[str]) -> dict:
    """Counts for one group of keys: how many practised, how many solid (box 3 or more), how many due now."""
    now = utcnow()
    practised = [rows[k] for k in keys if k in rows]
    return {
        "total": len(keys),
        "practised": len(practised),
        "solid": sum(1 for r in practised if (r.box or 0) >= 3),
        "due": sum(1 for r in practised if r.next_due_at and r.next_due_at <= now),
    }
