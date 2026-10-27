"""Interview prep built by rules, for the public showcase instance (no AI provider there).

Everything in it comes from two places: the posting's own text and the candidate's profile. Strengths are the
posting's requirements the fit rules found in the profile, each with the profile bullet that shows it; gaps are the
requirements they did not find; technical questions ask about those same requirements and answer with that bullet;
behavioral questions use the candidate's confirmed STAR stories. Nothing is invented about the company, so the
company section repeats only what the posting says. Output has the same shape as interview_prep_service's.
"""
import json
import re

from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import BehavioralStory, InterviewPrep, JobApplication, SearchKeyword
from . import demo_usage_service, fit_service
from .activity_logger import log_activity
from .matching_service import get_profile_content_for_application

_TRAIT_QUESTIONS = {
    "reliability": "Tell me about a time something you owned broke in production.",
    "ownership": "Tell me about a problem you fixed that nobody asked you to fix.",
    "collaboration": "Tell me about a time teams disagreed on the numbers. What did you do?",
    "communication": "Tell me about explaining a technical problem to people who are not engineers.",
    "leadership": "Tell me about a time you got others to change how they work.",
}


def _section(jd: str, heading: str) -> list[str]:
    """Lines under a heading in the posting ("What you'll do", ...), without their bullet marks."""
    m = re.search(rf"{re.escape(heading)}\s*\n(.*?)(?:\n\s*\n|\Z)", jd, re.S | re.I)
    if not m:
        return []
    return [re.sub(r"^[-*•]\s*", "", ln).strip() for ln in m.group(1).splitlines() if ln.strip()]


def _bullet_for(term: str, profile: dict) -> tuple[str, str] | None:
    """The first experience or project bullet that names `term`, with where it comes from."""
    pattern = re.compile(rf"(?<![\w.+#]){re.escape(term)}(?![\w+#])", re.I)
    for e in profile.get("experience", []):
        for b in e.get("bullets", []):
            if pattern.search(b):
                return b, f"At {e.get('company')}"
    for p in profile.get("projects", []):
        for b in p.get("bullets", []) + [p.get("description") or ""]:
            if b and pattern.search(b):
                return b, f"In my {p.get('name', 'project')} project"
    return None


def _numbers(text: str) -> str:
    found = re.findall(r"\d[\d,.]*\s?(?:%|GB|TB|M|K|hours?|minutes?|x)?(?:/day)?", text)
    return ", ".join(dict.fromkeys(f.strip() for f in found))[:80]


def build_rule_prep(db: Session, application: JobApplication) -> dict:
    profile, variant_id = get_profile_content_for_application(db, application)
    posting = application.posting
    jd = posting.job_description or ""
    targets = [k.keyword for k in db.query(SearchKeyword).filter(SearchKeyword.is_active == True)]  # noqa: E712
    rules = fit_service.rules_fit(profile, posting.job_title, jd, targets)
    skills = rules["skills"]

    shown = []
    for m in skills["matched"]:
        hit = _bullet_for(m["term"], profile)
        if hit:
            shown.append((m["term"], hit[0], hit[1]))
    strengths = [f"{t}: {b}" for t, b, _ in shown[:4]] or [
        "The fit rules found the posting's skills only in the skills list; bring a concrete example for each."]
    gaps = [f"{m['term']} is asked for and the profile shows {m['via']} instead. Say how close it is, plainly."
            for m in skills["transferable"]]
    gaps += [f"{m['term']} is asked for and is not in the profile. Do not claim it; say how you would learn it."
             for m in skills["missing"]]
    years = rules["years"]
    if years.get("required_years") and years.get("profile_years") is not None \
            and years["profile_years"] < years["required_years"]:
        gaps.append(f"The posting asks for {years['required_years']}+ years; the profile shows "
                    f"{years['profile_years']:.1f}. Lead with scope and results rather than tenure.")

    team = _section(jd, "About the team")
    duties = _section(jd, "What you'll do")
    first = (profile.get("experience") or [{}])[0]
    intro_bullets = first.get("bullets", [])[:2]

    technical = []
    for term, bullet, src in shown[:3]:
        technical.append({
            "question": f"How have you used {term} in production?",
            "category": "technical",
            "draft_answer": f"{src}: {bullet}",
            "quick_reference": _numbers(bullet) or term,
            "possible_follow_ups": [f"What went wrong with {term} and how did you fix it?",
                                    "What would you do differently now?"],
        })
    if duties:
        technical.append({
            "question": f"How would you design the pipeline behind this part of the role: \"{duties[0]}\"",
            "category": "system design",
            "draft_answer": "Start from who reads the output and how fresh it must be, then: raw landing zone, "
                            "idempotent incremental loads, tests and freshness checks on the models, and alerts that "
                            "reach the owner before the readers notice. Tie each choice to a pipeline you have run.",
            "quick_reference": "readers + freshness, raw zone, incremental, tests, alerts",
            "possible_follow_ups": ["How do you backfill a week of bad data?", "How do you handle late data?"],
        })

    stories = db.query(BehavioralStory).filter(BehavioralStory.variant_id == variant_id,
                                               BehavioralStory.status == "confirmed").all() if variant_id else []
    behavioral = []
    for s in stories[:3]:
        traits = json.loads(s.traits_json or "[]")
        question = next((_TRAIT_QUESTIONS[t] for t in traits if t in _TRAIT_QUESTIONS),
                        f"Tell me about this: {s.title.lower()}.")
        behavioral.append({
            "question": question,
            "category": "behavioral",
            "draft_answer": f"{s.situation} {s.task} {s.action} {s.result}",
            "quick_reference": s.title,
            "possible_follow_ups": ["What did you do yourself, not the team?", "How did you measure the result?"],
        })

    company = posting.company_name_raw
    rounds = [
        {"round_name": "Recruiter screen",
         "what_it_tests": "Motivation, role fit, salary range and work authorization.",
         "likely_interviewer": "Recruiter",
         "prep_focus": ["A 60-second story of your current role", f"Why {company}, from the posting's own words"],
         "qa_pairs": [{
             "question": "Walk me through your current role.",
             "category": "background",
             "draft_answer": f"I'm a {first.get('role', '')} at {first.get('company', '')}. " + " ".join(intro_bullets),
             "quick_reference": _numbers(" ".join(intro_bullets)),
             "possible_follow_ups": ["Why are you looking now?", "What do you want more of in your next role?"],
         }],
         "other_possible_questions": ["What salary range are you looking for?"],
         "questions_to_ask_them": ["What does success look like in the first 90 days?"]},
        {"round_name": "Technical: pipeline design",
         "what_it_tests": "The posting's own requirements, and designing a reliable pipeline end to end.",
         "likely_interviewer": "Senior engineer on the team",
         "prep_focus": [t for t, _, _ in shown[:4]] + ["Failure handling", "Backfills"],
         "qa_pairs": technical,
         "other_possible_questions": [f"How would you get up to speed on {m['term']}?" for m in skills["missing"][:2]],
         "questions_to_ask_them": ["Which pipeline pages the team most often today, and why?"]},
        {"round_name": "Behavioral",
         "what_it_tests": "Ownership, working with other teams, and how you handle things going wrong.",
         "likely_interviewer": "Hiring manager",
         "prep_focus": ["One concrete situation per answer", "Your own actions", "A number in the result"],
         "qa_pairs": behavioral,
         "other_possible_questions": ["Tell me about a decision you would make differently now."],
         "questions_to_ask_them": ["What would you want this hire to fix first?"]},
    ]
    return {
        "general_prep_json": {"strengths_to_emphasize": strengths, "potential_gaps_to_address": gaps},
        "company_prep_json": {
            "company_context": " ".join(team) or f"The posting says little about {company}'s team.",
            "recent_publications": [],
            "why_this_company_talking_points": [f"From the posting: {d}" for d in duties[:2]],
            "questions_to_ask_them": ["How do people who use the data report a problem, and how fast is it fixed?",
                                      "What does on-call look like for this team?"],
            "grounded_in_real_research": False,
        },
        "process_research_json": {
            "summary": "Built by rules in this demo from the posting and the profile. A real instance also researches "
                       "the company and its interview process.",
            "sources": [],
        },
        "predicted_rounds_json": {
            "rounds": rounds,
            "answering_method_guidance": "Use STAR for behavioral questions: one concrete situation, your own "
                                         "actions, and a number in the result.",
            "grounded_in_real_research": False,
            "grounding_warnings": [],
        },
    }


def generate_rule_prep(db: Session, application_id: int, generated_at=None) -> InterviewPrep:
    """Save a rules-built prep as the application's active version (older versions are kept)."""
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    content = build_rule_prep(db, application)
    db.query(InterviewPrep).filter(InterviewPrep.application_id == application_id,
                                   InterviewPrep.is_active == True).update({"is_active": False})  # noqa: E712
    prep = InterviewPrep(application_id=application_id, is_active=True, generated_at=generated_at or utcnow(),
                         used_tailored_resume=False, **{k: json.dumps(v) for k, v in content.items()})
    db.add(prep)
    if generated_at is None:
        demo_usage_service.record_simulated_run(db, "interview_prep")
        log_activity(db, f"Built interview prep by rules for '{application.posting.job_title}' at "
                         f"{application.posting.company_name_raw}.", "INFO")
    db.commit()
    return prep
