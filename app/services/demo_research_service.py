"""Answers dashboard questions by rules, for the public showcase instance (no AI provider there).

The research agent normally lets the model choose which tool to call. Here the question's words choose it: a company
name picks search_company, a status word with "how many" picks count_applications (the agent's own two tools), and
two read-only lookups cover the other questions people ask most (who is waiting on a reply, which postings fit
best). The step trail records the tool and what it returned, the same way the agent's does, and the answer only
repeats what the tool returned.
"""
from datetime import timedelta

from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import Company, JobApplication
from .models_access import count_applications_by_status, search_company

_STATUS_WORDS = [("not selected", "Not Selected"), ("needs review", "Needs Review"), ("pending", "Pending Confirmation"),
                 ("interview", "interviewing"), ("offer", "offer"), ("applied", "applied"), ("rejected", "Rejected"),
                 ("approved", "Approved"), ("ingested", "Ingested")]
_ACTIVE = ("Ingested", "Needs Review", "Pending Confirmation", "Approved", "Tailored")

HELP = ("This demo answers by rules, so it understands a few kinds of question: how many applications are in a "
        "status (\"How many are applied?\"), a company by name (\"What's the status of Northwind Logistics?\"), "
        "who is waiting on a reply, and which open postings fit best.")


def _step(action: str, action_input: str, observation: str) -> dict:
    return {"thought": f"The question matches the {action} rule.", "action": action, "action_input": action_input,
            "observation": observation}


def answer(db: Session, question: str) -> tuple[str, list[dict]]:
    q = question.lower()

    for c in sorted(db.query(Company).all(), key=lambda c: -len(c.name)):
        if c.name.lower() in q:
            found = search_company(db, c.name)
            apps = (db.query(JobApplication).join(JobApplication.posting)
                    .filter_by(company_id=c.id).all())
            lines = [f"{a.posting.job_title}: {a.status}" + (f", fit {a.match_score}" if a.match_score is not None else "")
                     for a in apps]
            observation = found + ("; applications: " + "; ".join(lines) if lines else "; no applications")
            text = f"{c.name}: " + ("; ".join(lines) if lines else "no applications tracked yet") + "."
            if c.tier:
                text += f" Sponsorship tier {c.tier}."
            return text, [_step("search_company", c.name, observation)]

    if "how many" in q or "count" in q or "number of" in q:
        for word, status in _STATUS_WORDS:
            if word in q:
                observation = count_applications_by_status(db, status)
                return observation[0].upper() + observation[1:], [_step("count_applications", status, observation)]

    if any(w in q for w in ("waiting", "reply", "replied", "follow up", "follow-up", "heard back", "ghost")):
        cutoff = utcnow() - timedelta(days=7)
        rows = (db.query(JobApplication).filter(JobApplication.status == "Applied",
                                                JobApplication.replied_at.is_(None),
                                                JobApplication.applied_at < cutoff)
                .order_by(JobApplication.applied_at).all())
        lines = [f"{a.posting.company_name_raw} ({a.posting.job_title}, applied "
                 f"{(utcnow() - a.applied_at).days} days ago{', recruiter on file' if a.recruiter_email else ''})"
                 for a in rows]
        observation = "; ".join(lines) or "none"
        text = (f"{len(rows)} applied more than a week ago with no reply: " + "; ".join(lines) + "."
                if rows else "No application has waited more than a week without a reply.")
        return text, [_step("waiting_on_reply", "7 days", observation)]

    if any(w in q for w in ("best fit", "fit best", "highest", "top", "strongest", "best match")):
        rows = (db.query(JobApplication).filter(JobApplication.status.in_(_ACTIVE),
                                                JobApplication.match_score.isnot(None))
                .order_by(JobApplication.match_score.desc()).limit(3).all())
        lines = [f"{a.posting.job_title} at {a.posting.company_name_raw} (fit {a.match_score})" for a in rows]
        observation = "; ".join(lines) or "none"
        text = ("Best fits among open postings: " + "; ".join(lines) + "." if rows
                else "No open posting has a fit score yet.")
        return text, [_step("top_fit", "3", observation)]

    return HELP, []
