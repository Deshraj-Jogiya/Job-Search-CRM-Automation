"""The Profile page's LinkedIn import and story drafting, by rules, for the public showcase instance.

A real instance asks the AI provider to merge a pasted LinkedIn profile and to draft STAR stories. The showcase has
no provider, so both run on rules that only move facts the visitor already gave: the import adds skills listed in the
paste's own Skills section that the profile lacks (as a pending version to approve), and story drafts are skeletons
built from experience bullets that carry a number, with the parts only the candidate can say left for them.
"""
import json
import re

from sqlalchemy.orm import Session

from ..models import BehavioralStory, ProfileVersion
from .activity_logger import log_activity

# Section headings in a LinkedIn profile's text, as the page shows them.
_HEADINGS = {"about", "experience", "education", "licenses & certifications", "licenses and certifications", "projects",
             "skills", "recommendations", "interests", "languages", "honors & awards", "courses", "volunteering",
             "publications", "activity", "featured"}
_NOT_A_SKILL = re.compile(r"\d|endorse|show all|·|\bat\b|experience|https?://", re.I)
_NUMBER = re.compile(r"\d[\d,.]*\s*(%|percent|x|hours?|minutes?|days?|ms|gb|tb|k\b|m\b|million|sources|tables|users)?",
                     re.I)
_TRAITS = [
    (re.compile(r"\blead|mentor|trained|onboard", re.I), "leadership"),
    (re.compile(r"\bcut|reduc|fix|debug|resolv|faster|lower", re.I), "problem_solving"),
    (re.compile(r"\bbuilt|owned|designed|launched|migrat", re.I), "ownership"),
    (re.compile(r"\bteam|partner|cross-functional|collaborat", re.I), "collaboration"),
    (re.compile(r"client|stakeholder|customer|business user", re.I), "client_facing"),
]
MAX_STORIES = 5


def _profile_skills(content: dict) -> set[str]:
    skills = content.get("skills") or {}
    values = skills.values() if isinstance(skills, dict) else [skills]
    return {s.strip().lower() for group in values for s in (group if isinstance(group, list) else [group])
            if isinstance(s, str)}


def pasted_skills(pasted_text: str) -> list[str]:
    """The lines under the paste's Skills heading, up to the next section heading."""
    found, in_skills = [], False
    for line in (l.strip() for l in pasted_text.splitlines()):
        if line.lower() in _HEADINGS:
            in_skills = line.lower() == "skills"
            continue
        if in_skills and line and len(line.split()) <= 5 and not _NOT_A_SKILL.search(line) and line not in found:
            found.append(line)
    return found


def propose_linkedin_skills(db: Session, variant_id: int, content: dict, pasted_text: str) -> ProfileVersion:
    have = _profile_skills(content)
    new = [s for s in pasted_skills(pasted_text) if s.lower() not in have]
    updated = json.loads(json.dumps(content))
    if new:
        skills = updated.setdefault("skills", {})
        if isinstance(skills, dict):
            skills.setdefault("other", [])
            skills["other"] = list(skills["other"]) + new
    summary = (f"Added {len(new)} skill{'s' if len(new) != 1 else ''} from the paste's Skills section: {', '.join(new)}."
               if new else "No skill in the paste's Skills section is missing from the profile.")
    summary += " Built by rules (this demo has no AI provider): other sections are not merged."
    version = ProfileVersion(variant_id=variant_id, content_json=json.dumps(updated, indent=2), source="linkedin_diff",
                             change_summary=summary, is_active=False)
    db.add(version)
    db.commit()
    db.refresh(version)
    log_activity(db, "Proposed a LinkedIn skills import by rules (pending approval).")
    return version


def _result_phrase(bullet: str) -> str:
    clauses = [c.strip() for c in re.split(r"[,;]| and | by | to ", bullet) if _NUMBER.search(c)]
    return clauses[-1] if clauses else bullet


def draft_stories(db: Session, variant_id: int, content: dict) -> list[BehavioralStory]:
    used = {s.source_reference for s in db.query(BehavioralStory).filter(BehavioralStory.variant_id == variant_id)}
    stories = []
    for entry in content.get("experience") or []:
        role, company = entry.get("role") or "", entry.get("company") or ""
        for bullet in entry.get("bullets") or []:
            ref = f"experience: {role} at {company}: {bullet[:80]}"
            if len(stories) >= MAX_STORIES or ref in used or not re.search(r"\d", bullet):
                continue
            words = bullet.rstrip(".").split()
            story = BehavioralStory(
                variant_id=variant_id, title=" ".join(words[:9]) + ("..." if len(words) > 9 else ""),
                situation=f"{role} at {company} ({entry.get('date') or 'dates on the profile'}).",
                task="Draft, needs your words: what had to change, and why it mattered then.",
                action=bullet, result=_result_phrase(bullet),
                traits_json=json.dumps([t for pattern, t in _TRAITS if pattern.search(bullet)][:3]),
                source_reference=ref, status="draft")
            db.add(story)
            stories.append(story)
    db.commit()
    for s in stories:
        db.refresh(s)
    return stories
