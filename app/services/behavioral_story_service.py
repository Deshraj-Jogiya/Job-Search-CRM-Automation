"""
Reusable STAR-format behavioral story bank, tied to a profile variant
rather than a single application. The same handful of real stories
(leadership, ownership, conflict, failure-and-growth, etc.) get reused
across every behavioral/PEI-style interview round for any job, instead
of being regenerated from scratch each time an application asks for
interview prep.

Draft-then-confirm, same safeguard posture as tailoring
(profile_service.detect_profile_regressions / the tailoring
fabrication guard): an LLM-drafted story is never treated as ready-to-
use prep material until a human reviews and confirms it. Every draft
must cite which real experience or project entry it came from, and the
prompt is explicit about not inventing or combining unrelated facts
into a new claim.
"""

import json

from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import BehavioralStory, ProfileVariant
from .llm import get_llm_provider, parse_json_response
from .profile_service import get_active_version


class BehavioralStoryServiceError(Exception):
    """User-facing failure -- callers show the message instead of a 500."""


def generate_story_drafts(db: Session, variant_id: int) -> list[BehavioralStory]:
    variant = db.query(ProfileVariant).filter(ProfileVariant.id == variant_id).first()
    if not variant:
        raise BehavioralStoryServiceError(f"Profile variant {variant_id} not found.")

    version = get_active_version(db, variant_id)
    if not version:
        raise BehavioralStoryServiceError("This profile variant has no active version yet.")

    content = json.loads(version.content_json)

    llm = get_llm_provider()
    raw = llm.complete_json(
        system="You are an expert interview coach. You return only raw JSON.",
        prompt=(
            "Draft 4-6 STAR-format (Situation, Task, Action, Result) behavioral interview stories "
            "using ONLY real achievements that appear in this candidate's profile below. Do not invent "
            "anything, do not combine unrelated facts into a new claim, and do not embellish numbers "
            "beyond what is stated. Each story must be traceable to one specific real experience entry "
            "or project in the profile.\n\n"
            f"Candidate Profile:\n{json.dumps(content, indent=2)}\n\n"
            "Respond with EXACTLY this JSON shape:\n"
            "{\n"
            '  "stories": [\n'
            "    {\n"
            '      "title": "...",\n'
            '      "situation": "...",\n'
            '      "task": "...",\n'
            '      "action": "...",\n'
            '      "result": "...",\n'
            '      "traits": ["leadership", "..."],\n'
            '      "source_reference": "which real experience entry or project this is drawn from"\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "Pick traits from: leadership, ownership, problem_solving, conflict, failure_and_growth, "
            "collaboration, client_facing. Ground every claim in the profile above -- if there isn't "
            "enough real material for a given trait, skip it rather than inventing one. Do not wrap the "
            "output in markdown code fences."
        ),
        temperature=0.3,
        max_tokens=4000,
    )
    parsed = parse_json_response(raw)

    stories = []
    for s in parsed.get("stories", []):
        story = BehavioralStory(
            variant_id=variant_id,
            title=s.get("title") or "Untitled story",
            situation=s.get("situation", ""),
            task=s.get("task", ""),
            action=s.get("action", ""),
            result=s.get("result", ""),
            traits_json=json.dumps(s.get("traits", [])),
            source_reference=s.get("source_reference"),
            status="draft",
        )
        db.add(story)
        stories.append(story)
    db.commit()
    for s in stories:
        db.refresh(s)
    return stories


def list_stories(db: Session, variant_id: int, confirmed_only: bool = False) -> list[BehavioralStory]:
    q = db.query(BehavioralStory).filter(BehavioralStory.variant_id == variant_id)
    if confirmed_only:
        q = q.filter(BehavioralStory.status == "confirmed")
    return q.order_by(BehavioralStory.created_at).all()


def _get_or_raise(db: Session, story_id: int) -> BehavioralStory:
    story = db.query(BehavioralStory).filter(BehavioralStory.id == story_id).first()
    if not story:
        raise BehavioralStoryServiceError(f"Story {story_id} not found.")
    return story


def confirm_story(db: Session, story_id: int) -> BehavioralStory:
    story = _get_or_raise(db, story_id)
    story.status = "confirmed"
    story.updated_at = utcnow()
    db.commit()
    db.refresh(story)
    return story


def update_story(
    db: Session,
    story_id: int,
    title: str = None,
    situation: str = None,
    task: str = None,
    action: str = None,
    result: str = None,
    traits: list = None,
    source_reference: str = None,
) -> BehavioralStory:
    story = _get_or_raise(db, story_id)
    if title is not None:
        story.title = title
    if situation is not None:
        story.situation = situation
    if task is not None:
        story.task = task
    if action is not None:
        story.action = action
    if result is not None:
        story.result = result
    if traits is not None:
        story.traits_json = json.dumps(traits)
    if source_reference is not None:
        story.source_reference = source_reference
    story.updated_at = utcnow()
    db.commit()
    db.refresh(story)
    return story


def delete_story(db: Session, story_id: int) -> None:
    story = _get_or_raise(db, story_id)
    db.delete(story)
    db.commit()
