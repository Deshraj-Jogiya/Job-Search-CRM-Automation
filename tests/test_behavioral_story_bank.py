"""Behavioral story bank (Phase 3 of the interview-prep platform
feature): reusable STAR-format stories tied to a profile variant,
draft-then-confirm so an LLM-drafted story is never treated as
ready-to-use prep material until a human reviews it. The LLM-calling
draft generation itself isn't exercised here -- same convention as the
rest of this suite (contact_discovery_service, tailoring_service):
external-API/LLM calls aren't unit tested, only the pure logic around
them (CRUD, status transitions, filtering)."""

import json

import pytest

from app import models
from app.services import behavioral_story_service
from app.services.behavioral_story_service import BehavioralStoryServiceError
from tests.conftest import make_variant


def _make_story(db, variant, status="draft", traits=None):
    story = models.BehavioralStory(
        variant_id=variant.id,
        title="Led a migration under a tight deadline",
        situation="Legacy system was failing.",
        task="Own the migration.",
        action="Planned and executed a phased cutover.",
        result="Zero downtime, delivered a week early.",
        traits_json=json.dumps(traits or ["leadership", "ownership"]),
        source_reference="CurioSync project",
        status=status,
    )
    db.add(story)
    db.commit()
    db.refresh(story)
    return story


def test_list_stories_returns_only_this_variant(db):
    variant_a = make_variant(db, name="A")
    variant_b = make_variant(db, name="B", is_default=False)
    _make_story(db, variant_a)
    _make_story(db, variant_b)

    stories = behavioral_story_service.list_stories(db, variant_a.id)

    assert len(stories) == 1
    assert stories[0].variant_id == variant_a.id


def test_list_stories_confirmed_only_filters_drafts(db):
    variant = make_variant(db)
    _make_story(db, variant, status="draft")
    confirmed = _make_story(db, variant, status="confirmed")

    stories = behavioral_story_service.list_stories(db, variant.id, confirmed_only=True)

    assert len(stories) == 1
    assert stories[0].id == confirmed.id


def test_confirm_story_flips_status(db):
    variant = make_variant(db)
    story = _make_story(db, variant, status="draft")

    result = behavioral_story_service.confirm_story(db, story.id)

    assert result.status == "confirmed"
    db.refresh(story)
    assert story.status == "confirmed"


def test_confirm_missing_story_raises(db):
    with pytest.raises(BehavioralStoryServiceError):
        behavioral_story_service.confirm_story(db, 999)


def test_update_story_only_changes_provided_fields(db):
    variant = make_variant(db)
    story = _make_story(db, variant)
    original_situation = story.situation

    behavioral_story_service.update_story(db, story.id, title="New title")

    db.refresh(story)
    assert story.title == "New title"
    assert story.situation == original_situation


def test_update_story_traits_replaces_list(db):
    variant = make_variant(db)
    story = _make_story(db, variant, traits=["leadership"])

    behavioral_story_service.update_story(db, story.id, traits=["conflict", "growth"])

    db.refresh(story)
    assert json.loads(story.traits_json) == ["conflict", "growth"]


def test_delete_story_removes_it(db):
    variant = make_variant(db)
    story = _make_story(db, variant)
    story_id = story.id

    behavioral_story_service.delete_story(db, story_id)

    assert db.query(models.BehavioralStory).filter(models.BehavioralStory.id == story_id).first() is None


def test_generate_story_drafts_raises_for_missing_variant(db):
    with pytest.raises(BehavioralStoryServiceError):
        behavioral_story_service.generate_story_drafts(db, 999)


def test_generate_story_drafts_raises_when_no_active_version(db):
    variant = models.ProfileVariant(name="Empty", is_default=True)
    db.add(variant)
    db.commit()
    db.refresh(variant)

    with pytest.raises(BehavioralStoryServiceError):
        behavioral_story_service.generate_story_drafts(db, variant.id)
