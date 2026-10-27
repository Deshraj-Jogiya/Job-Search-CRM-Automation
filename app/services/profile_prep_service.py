"""
Profile-level interview prep -- the "whole profile" prep for a recruiter call
or a direct interview, NOT tailored to any job, company, or job description.

interview_prep_service.py prepares for one specific application (its company,
its predicted rounds, its JD). This module is the other thing: what you need
at hand before any call, whatever the job. Two parts, kept separate on purpose:

- Reference sections, built mechanically from the profile on every page load
  with no LLM and no stored copy, so they can never drift from the profile
  and cost nothing: work timeline with employment type, a walkthrough card per
  project, skills by area, education and certifications, the application
  preferences answers (work authorization, sponsorship, relocation, salary,
  notice period), the confirmed behavioral stories, and a fixed list of
  questions worth asking any interviewer.
- Drafted answers, generated on demand by one LLM call: a 30-second and a
  90-second "tell me about yourself", and first-person drafts for the
  questions nearly every interview asks, plus honest answers for the real
  gaps in the profile. Grounded only in the base profile and the confirmed
  stories. Every metric in a draft is checked against the profile with the
  same mechanical check interview_prep_service uses; anything not found is
  surfaced as a warning for the candidate to verify, never silently kept.

Confidential (NDA) projects are removed from everything here, since this
material is meant to be said out loud.
"""

import json
import re
import os
import threading
import time

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from ..app_mode import is_showcase_mode
from ..database import SessionLocal, utcnow
from ..models import ProfileInterviewPrep, ProfileVariant, ProfileVersion, get_or_create_settings
from . import behavioral_story_service, resume_rules
from .activity_logger import log_activity
from .interview_prep_service import check_answer_grounding
from .llm import get_llm_provider, llm_available, parse_json_response
from .profile_service import strip_confidential_projects


class ProfilePrepServiceError(Exception):
    """User-facing failure -- callers show the message instead of a 500."""


# Fixed on purpose: the point of this prep is to cover the questions nearly
# every interview asks. Work authorization, sponsorship, salary, relocation and
# notice period are NOT in this list -- they are answered mechanically from the
# stored application preferences, never drafted.
COMMON_QUESTIONS = (
    "Tell me about yourself.",
    "Walk me through your resume.",
    "Why did you choose data engineering and AI as your field?",
    "What are you looking for in your next role?",
    "Tell me about the project you are proudest of.",
    "What is your strongest technical skill, and where did you use it?",
    "Tell me about a time something you built failed or you made a mistake. What did you do?",
    "Tell me about a time you disagreed with someone or had to work with a difficult stakeholder.",
    "How do you handle a task with unclear requirements or a tight deadline?",
    "What is your biggest weakness?",
    "Several of your roles were contract, part-time, or short. Why?",
    "Where do you see yourself in three to five years?",
)

QUESTIONS_TO_ASK_ANYONE = (
    "What does success look like in the first 90 days, and how will it be measured?",
    "What does a typical week look like for someone in this role?",
    "What is the team working on right now that you are most excited about?",
    "How does the team handle a failing pipeline, a bad deploy, or a data-quality incident?",
    "How do engineers and the business side decide what gets built next?",
    "What would you want the person in this role to have done differently from the last person?",
    "What are the biggest open challenges the team has not solved yet?",
    "What are the next steps, and when should I expect to hear back?",
)

_PREFERENCE_LABELS = (
    ("work_authorization", "Work authorization"),
    ("visa_sponsorship", "Visa sponsorship needed"),
    ("willing_to_relocate", "Willing to relocate"),
    ("salary_minimum", "Salary minimum"),
    ("salary_negotiable", "Salary negotiable"),
    ("notice_period", "Notice period"),
    ("currently_employed", "Currently employed"),
)


def _default_variant(db: Session) -> ProfileVariant:
    variant = db.query(ProfileVariant).filter(ProfileVariant.is_default == True).first()  # noqa: E712
    if not variant:
        raise ProfilePrepServiceError(
            "No profile is set up yet. Create one and seed its content on the Profile page first."
        )
    return variant


def _active_version(db: Session, variant_id: int) -> ProfileVersion:
    version = (
        db.query(ProfileVersion)
        .filter(ProfileVersion.variant_id == variant_id, ProfileVersion.is_active == True)  # noqa: E712
        .order_by(ProfileVersion.id.desc())
        .first()
    )
    if not version:
        raise ProfilePrepServiceError("Your profile has no active content yet -- seed it on the Profile page first.")
    return version


def load_profile(db: Session) -> tuple[dict, ProfileVariant]:
    variant = _default_variant(db)
    version = _active_version(db, variant.id)
    return strip_confidential_projects(json.loads(version.content_json)), variant



# Skill-group keys in the profile are snake_case; the page shows them as names a person would use.
_SKILL_GROUP_NAMES = {
    "generative_ai_llms_agentic_frameworks": "Generative AI, LLMs and agents",
    "machine_learning_deep_learning": "Machine learning and deep learning",
    "data_engineering_cloud_platforms": "Data engineering and cloud",
    "full_stack_software_engineering": "Software engineering",
    "devops_tooling_analytics": "DevOps, tooling and analytics",
}
FEATURED_PROJECTS = 6
_LABEL_RE = re.compile(r"^([A-Z][A-Za-z0-9&/+.\- ]{2,45}):\s+(?=\S)")


def skill_group_name(key: str) -> str:
    return _SKILL_GROUP_NAMES.get(key) or key.replace("_", " ").capitalize()


_FILLER_RE = re.compile(r"\b(?:real|genuine|actual)\b,? (?!-?time|world|estate)", re.IGNORECASE)


def split_bullet(text: str) -> dict:
    """'Label: rest' bullets show the label in bold; others are plain. Filler words ("a real", "genuine") are dropped, as on the portfolio."""
    text = re.sub(r"\s{2,}", " ", re.sub(r"\bactually\s+", "", _FILLER_RE.sub("", text or ""))).strip()
    m = _LABEL_RE.match(text or "")
    return {"label": m.group(1), "text": text[m.end():]} if m else {"label": "", "text": text or ""}


def _end_key(date: str) -> tuple:
    if re.search(r"present", date or "", re.I):
        return (9999, 12)
    months = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
    found = re.findall(r"([A-Za-z]{3})[a-z]*\.?\s+((?:19|20)\d\d)", date or "")
    if found:
        mon, year = found[-1]
        return (int(year), months.get(mon.lower(), 0))
    years = re.findall(r"(?:19|20)\d\d", date or "")
    return (int(years[-1]), 0) if years else (0, 0)


def friendly_error(error: str | None) -> str | None:
    """A short sentence for the page; the raw provider error stays in the activity log."""
    if not error:
        return None
    low = error.lower()
    if "credit balance" in low or "billing" in low:
        return "The AI account has no credits left, so the drafted answers cannot refresh until credits are added."
    if "rate" in low and "limit" in low or "429" in low:
        return "The AI service was busy (rate limited). It will try again on its own later."
    if "spend cap" in low:
        return "Today's AI spend cap is reached. It will try again tomorrow."
    return "The last refresh failed. Details are in the activity log."


def build_reference(db: Session, profile: dict, variant_id: int) -> dict:
    """Everything that can be shown straight from the profile, no LLM."""
    prefs = profile.get("application_preferences") or {}
    preferences = []
    for key, label in _PREFERENCE_LABELS:
        if key in prefs and prefs[key] not in (None, ""):
            value = prefs[key]
            preferences.append({"label": label, "value": "Yes" if value is True else "No" if value is False else str(value)})
    timeline = [
        {
            "company": e.get("company", ""), "employment_type": e.get("employment_type", ""),
            "role": e.get("role", ""), "date": e.get("date", ""), "location": e.get("location", ""),
            "bullets": e.get("bullets", []),
        }
        for e in profile.get("experience", [])
    ]
    projects = sorted(
        (
            {"name": p.get("name", ""), "date": p.get("date", ""), "technologies": p.get("technologies", []),
             "bullets": [split_bullet(b) for b in p.get("bullets", [])]}
            for p in profile.get("projects", [])
        ),
        key=lambda p: _end_key(p["date"]),
        reverse=True,
    )
    stories = behavioral_story_service.list_stories(db, variant_id, confirmed_only=True)
    return {
        "name": profile.get("name", ""),
        "summary": profile.get("summary", ""),
        "preferences": preferences,
        "timeline": timeline,
        "projects": projects[:FEATURED_PROJECTS],
        "more_projects": projects[FEATURED_PROJECTS:],
        "skills": [{"name": skill_group_name(k), "tools": v} for k, v in (profile.get("skills") or {}).items() if v],
        "education": profile.get("education", []),
        "certifications": [resume_rules.certification_display(c) for c in profile.get("certifications", [])],
        "stories": stories,
        "questions_to_ask": list(QUESTIONS_TO_ASK_ANYONE),
    }


def _format_stories(stories: list) -> str:
    if not stories:
        return "(none confirmed yet)"
    return "\n".join(
        f"- {s.title}: situation: {s.situation} | task: {s.task} | action: {s.action} | result: {s.result}"
        for s in stories
    )


def _draft_answers(profile: dict, stories: list) -> dict:
    llm = get_llm_provider()
    raw = llm.complete_json(
        system="You are an expert interview coach. You return only raw JSON.",
        prompt=(
            "Draft general interview prep for this candidate. It is NOT for any specific job or company: it is "
            "the prep for any recruiter call or interview.\n\n"
            f"Candidate profile:\n{json.dumps(profile, indent=2)}\n\n"
            f"Confirmed behavioral stories (use these for the story-style questions):\n{_format_stories(stories)}\n\n"
            "Respond with EXACTLY this JSON shape:\n"
            "{\n"
            '  "pitch_30_seconds": "first-person, about 70 words",\n'
            '  "pitch_90_seconds": "first-person, about 200 words",\n'
            '  "answers": [{"question": "...", "draft_answer": "first person, ready to say aloud", '
            '"quick_reference": "one short cue line"}],\n'
            '  "strengths_to_steer_toward": ["..."],\n'
            '  "honest_gaps": [{"gap": "a real gap or limit in the profile", "honest_answer": "how to say it truthfully"}]\n'
            "}\n"
            "Write one entry in answers for EACH of these questions, in this order, using the question text "
            "exactly:\n" + "\n".join(f"{i + 1}. {q}" for i, q in enumerate(COMMON_QUESTIONS)) + "\n\n"
            "Rules: ground every statement in something that appears in the profile or the confirmed stories. "
            "Do not invent a project, employer, title, tool, date, or number; reuse only figures that already "
            "appear in the profile. Keep each employment type as the profile states it (contract, part-time, "
            "apprenticeship, full-time) and never present a contract or part-time role as full-time. For the "
            "question about short or contract roles, answer from the facts in the profile and do not invent a "
            "reason that is not in it. Do not state any work-authorization, visa, salary, or relocation answer; "
            "those are answered elsewhere. honest_gaps must be real gaps visible in the profile (for example "
            "limited years of professional experience, tools listed only from personal projects), stated "
            "truthfully, never softened into something false. Plain language, no filler words such as "
            "leveraged, spearheaded, utilized, seamless, or synergy. Do not wrap the output in markdown code "
            "fences."
        ),
        temperature=0.4,
        max_tokens=6000,
    )
    return parse_json_response(raw)


def generate_profile_prep(db: Session, trigger: str = "manual") -> ProfileInterviewPrep:
    profile, variant = load_profile(db)
    version_id = _active_version(db, variant.id).id
    stories = behavioral_story_service.list_stories(db, variant.id, confirmed_only=True)
    try:
        if llm_available():
            drafted = _draft_answers(profile, stories)
        else:
            from .demo_prep_service import draft_profile_answers

            drafted = draft_profile_answers(profile, stories)
    except Exception as e:
        raise ProfilePrepServiceError(f"Profile interview prep generation failed: {e}") from e

    answers = drafted.get("answers") or []
    pitches = {"round_name": "Pitch", "qa_pairs": [
        {"question": "30-second pitch", "draft_answer": drafted.get("pitch_30_seconds", "")},
        {"question": "90-second pitch", "draft_answer": drafted.get("pitch_90_seconds", "")},
    ]}
    drafted["grounding_warnings"] = check_answer_grounding(
        profile, {"rounds": [pitches, {"round_name": "Common questions", "qa_pairs": answers}]}
    )
    # honest-gap answers are spoken too, so they get the same figure check
    drafted["grounding_warnings"] += check_answer_grounding(
        profile, {"rounds": [{"round_name": "Honest gaps", "qa_pairs": [
            {"question": g.get("gap", ""), "draft_answer": g.get("honest_answer", "")} for g in drafted.get("honest_gaps") or []
        ]}]}
    )
    asked = {a.get("question", "").strip() for a in answers}
    drafted["missing_questions"] = [q for q in COMMON_QUESTIONS if q not in asked]

    db.query(ProfileInterviewPrep).filter(
        ProfileInterviewPrep.variant_id == variant.id, ProfileInterviewPrep.is_active == True  # noqa: E712
    ).update({"is_active": False})
    prep = ProfileInterviewPrep(
        variant_id=variant.id, content_json=json.dumps(drafted), is_active=True, generated_at=utcnow(),
        profile_version_id=version_id, trigger=trigger,
    )
    db.add(prep)
    db.commit()
    db.refresh(prep)
    log_activity(
        db,
        f"Generated profile interview prep ({trigger}, from profile version {version_id}): {len(answers)} drafted answers, "
        f"{len(drafted['grounding_warnings'])} grounding warning(s), "
        f"{len(drafted['missing_questions'])} question(s) the model skipped.",
        "INFO",
    )
    return prep


def get_active_prep(db: Session, variant_id: int) -> tuple[ProfileInterviewPrep | None, dict | None]:
    prep = (
        db.query(ProfileInterviewPrep)
        .filter(ProfileInterviewPrep.variant_id == variant_id, ProfileInterviewPrep.is_active == True)  # noqa: E712
        .order_by(ProfileInterviewPrep.generated_at.desc())
        .first()
    )
    return prep, (json.loads(prep.content_json) if prep else None)


# ---------------------------------------------------------------------------
# Keeping the drafted answers in step with the profile
#
# Real gap found 2026-10-10: the first version of this page only had a manual
# button, so it showed nothing until clicked and silently went stale the moment
# the profile changed. Three independent paths now keep it current, all through
# run_generation() so two of them can never run at once:
#   1. a profile version becoming active (any route that saves, syncs, approves
#      or restores one) schedules a refresh after a short debounce, so a burst of
#      edits costs one call;
#   2. an hourly scheduler catch-up (and the page load) covers "never generated"
#      (the initial prep for a profile that already existed) and anything path 1
#      missed (a restart, a failed attempt), with a cooldown after a failure so a
#      credit problem does not retry every hour;
#   3. a Refresh button on the page, which ignores the cooldown.
# One setting (GlobalSettings.profile_prep_auto_refresh) switches paths 1 and 2
# off; the button always works. Nothing here is gated by automation_enabled: that
# is the job-hunting kill switch, and this is one call per profile change.
# ---------------------------------------------------------------------------

DEBOUNCE_SECONDS = 90
FAILURE_COOLDOWN_SECONDS = 30 * 60

_state_lock = threading.Lock()
_state = {"generating": False, "last_error": None, "last_failure_at": 0.0, "timer": None}


def _env_allows_auto() -> bool:
    # Never in the public showcase instance (it has no real API keys, so every attempt would only fail
    # and log), and switchable off by environment for tests.
    return os.getenv("PROFILE_PREP_AUTO_REFRESH", "1") != "0" and not is_showcase_mode()


def auto_refresh_enabled(db: Session) -> bool:
    return _env_allows_auto() and bool(get_or_create_settings(db).profile_prep_auto_refresh)


def status(db: Session) -> dict:
    """What the page shows: none | current | stale, plus whether a run is in progress."""
    variant = _default_variant(db)
    current = _active_version(db, variant.id)
    prep, _ = get_active_prep(db, variant.id)
    if prep is None:
        state = "none"
    elif prep.profile_version_id == current.id:
        state = "current"
    else:
        state = "stale"
    return {
        "state": state,
        "generating": _state["generating"],
        "last_error": _state["last_error"],
        "last_error_text": friendly_error(_state["last_error"]),
        "current_version_id": current.id,
        "current_version_at": current.created_at,
        "built_from_version_id": prep.profile_version_id if prep else None,
        "trigger": prep.trigger if prep else None,
        "auto_refresh": auto_refresh_enabled(db),
    }


def run_generation(trigger: str) -> bool:
    """Runs one generation in the calling thread. Returns False without doing anything when one is
    already running. Failures are recorded for the page and the activity log, never raised."""
    with _state_lock:
        if _state["generating"]:
            return False
        _state["generating"] = True
    db = SessionLocal()
    try:
        generate_profile_prep(db, trigger=trigger)
        _state["last_error"] = None
        _state["last_failure_at"] = 0.0
    except Exception as e:
        _state["last_error"] = str(e)
        _state["last_failure_at"] = time.time()
        try:
            log_activity(db, f"Profile interview prep refresh failed ({trigger}): {e}", "ERROR")
        except Exception:
            pass
    finally:
        db.close()
        _state["generating"] = False
    return True


def start_background_generation(trigger: str) -> bool:
    """Non-blocking: starts the run on a thread. False when one is already running."""
    if _state["generating"]:
        return False
    threading.Thread(target=run_generation, args=(trigger,), daemon=True).start()
    return True


def catch_up(db: Session) -> str:
    """The hourly scheduler path (also used by the page load): generate when there is no prep for the
    current profile version, unless auto refresh is off, a run is in progress, or a recent failure is
    still cooling down. Returns what it did, for the log and tests."""
    if not auto_refresh_enabled(db):
        return "auto refresh off"
    try:
        st = status(db)
    except ProfilePrepServiceError:
        return "no profile yet"
    if st["state"] == "current":
        return "up to date"
    if st["generating"]:
        return "already generating"
    if time.time() - _state["last_failure_at"] < FAILURE_COOLDOWN_SECONDS:
        return "cooling down after a failed attempt"
    run_generation("initial" if st["state"] == "none" else "profile_changed")
    return "generated"


def catch_up_in_background() -> None:
    """catch_up() on a thread with its own session, so a page visit never blocks on it."""
    def _go():
        db = SessionLocal()
        try:
            catch_up(db)
        finally:
            db.close()

    if not _state["generating"]:
        threading.Thread(target=_go, daemon=True).start()


def schedule_refresh_after_profile_change() -> None:
    """Debounced: every profile change resets the timer, so a burst of saves produces one refresh."""
    if not _env_allows_auto():
        return

    def _fire():
        db = SessionLocal()
        try:
            if auto_refresh_enabled(db):
                run_generation("profile_changed")
        finally:
            db.close()

    with _state_lock:
        if _state["timer"] is not None:
            _state["timer"].cancel()
        timer = threading.Timer(DEBOUNCE_SECONDS, _fire)
        timer.daemon = True
        _state["timer"] = timer
        timer.start()


def _on_version_inserted(mapper, connection, target):
    if target.is_active:
        schedule_refresh_after_profile_change()


def _on_version_updated(mapper, connection, target):
    history = inspect(target).attrs.is_active.history
    if target.is_active and history.has_changes():
        schedule_refresh_after_profile_change()


_listeners_registered = False


def register_auto_refresh() -> None:
    """Called once at app startup. Hooks every route that makes a profile version active (manual save,
    portfolio sync, approving a LinkedIn diff, restoring an older version) without editing any of them."""
    global _listeners_registered
    if _listeners_registered:
        return
    event.listen(ProfileVersion, "after_insert", _on_version_inserted)
    event.listen(ProfileVersion, "after_update", _on_version_updated)
    _listeners_registered = True
