"""Showcase-only rules versions of contact discovery, the LinkedIn skills import and story drafts."""
import json

from app.models import BehavioralStory, ProfileVariant, ProfileVersion
from app.services import demo_networking_service, demo_profile_helpers

PROFILE = {
    "name": "Jordan Ellis",
    "skills": {"languages": ["Python", "SQL"], "data_tools": ["Airflow"]},
    "education": [{"degree": "B.S. in Computer Science", "school": "Sample State University"}],
    "experience": [{"role": "Data Engineer", "company": "Acme", "date": "Mar 2022 – Present",
                    "bullets": ["Cut the nightly batch from 4 hours to 45 minutes by partitioning by date",
                                "Wrote clear runbooks for the on-call rotation"]}],
}


def test_discovery_lists_manager_peer_and_recruiter_with_reasons_from_facts():
    contacts = demo_networking_service.discover("Orbital Maps", "Data Engineer", PROFILE)
    assert [c["title"].split(",")[0] for c in contacts] == ["Data Engineering Manager", "Data Engineer",
                                                              "Technical Recruiter"]
    assert "Sample State University" in contacts[1]["reason"]
    assert all(c["linkedin_url"] is None and c["suggested_email"] is None for c in contacts)
    assert contacts == demo_networking_service.discover("Orbital Maps", "Data Engineer", PROFILE)  # stable names


def test_linkedin_import_adds_only_new_skills_from_the_skills_section(db):
    variant = ProfileVariant(name="Default")
    db.add(variant)
    db.commit()
    paste = "About\nI like data\nSkills\nPython\nApache Kafka\n12 endorsements\ndbt\nShow all 20 skills\nEducation\nGo"
    assert demo_profile_helpers.pasted_skills(paste) == ["Python", "Apache Kafka", "dbt"]
    version = demo_profile_helpers.propose_linkedin_skills(db, variant.id, PROFILE, paste)
    assert not version.is_active and "Apache Kafka, dbt" in version.change_summary
    assert json.loads(version.content_json)["skills"]["other"] == ["Apache Kafka", "dbt"]
    assert "other" not in PROFILE["skills"]  # the active profile is untouched until approved


def test_story_drafts_come_only_from_numbered_bullets_and_are_not_repeated(db):
    variant = ProfileVariant(name="Default")
    db.add(variant)
    db.commit()
    stories = demo_profile_helpers.draft_stories(db, variant.id, PROFILE)
    assert len(stories) == 1 and stories[0].status == "draft"
    assert stories[0].action.startswith("Cut the nightly batch") and "needs your words" in stories[0].task
    assert "problem_solving" in json.loads(stories[0].traits_json)
    assert demo_profile_helpers.draft_stories(db, variant.id, PROFILE) == []
    assert db.query(BehavioralStory).count() == 1
