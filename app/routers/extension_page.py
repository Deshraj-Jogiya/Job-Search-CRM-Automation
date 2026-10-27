"""
Extension page (/extension): what the browser extension does, where it has been tested, how to install it, and a
sample application form that "Fill" answers with the same extension_service.resolve_field_answers the extension
calls from a real employer page. The answers come from the chosen application's profile; nothing is submitted.
"""

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import JobApplication
from ..services import extension_service
from ..templating import render

router = APIRouter(prefix="/extension", tags=["extension"])

_MANIFEST = Path(__file__).resolve().parents[2] / "extension" / "manifest.json"

# The ATS platforms the extension was driven against on real postings (see extension/README.md).
TESTED_PLATFORMS = [
    ("Greenhouse", "Samsara, Lyft, Instacart: three embed patterns, react-select menus, file attachment"),
    ("Lever", "Allegiant: question headings with no label element, wrapped radio options"),
    ("Ashby", "OpenAI, Cognition: EEO and yes/no radio groups"),
    ("Recruitee", "Metyis: number fields, 1px custom radios with visible labels"),
    ("Personio", "The Mobility House: hidden file inputs, German labels"),
    ("Workable", "Hack The Box: help-icon text kept out of labels and options"),
    ("SmartRecruiters", "AIR Communities, NielsenIQ: fields inside nested shadow DOM"),
]

# Shaped like the fields the content script sends: radio groups and checkboxes go as selects with their options.
SAMPLE_FIELDS = [
    {"field_id": "first_name", "label": "First name", "type": "text"},
    {"field_id": "last_name", "label": "Last name", "type": "text"},
    {"field_id": "email", "label": "Email", "type": "email"},
    {"field_id": "phone", "label": "Phone", "type": "tel"},
    {"field_id": "city", "label": "City", "type": "text"},
    {"field_id": "linkedin", "label": "LinkedIn profile", "type": "url"},
    {"field_id": "github", "label": "GitHub", "type": "url"},
    {"field_id": "years", "label": "Years of experience in a data engineering role", "type": "select",
     "options": ["Less than 1 year", "1-2 years", "2-3 years", "3-5 years", "5+ years"]},
    {"field_id": "education", "label": "Highest level of education", "type": "select",
     "options": ["High school", "Bachelor's degree", "Master's degree", "Doctorate"]},
    {"field_id": "authorized", "label": "Are you legally authorized to work in the United States?", "type": "radio",
     "options": ["Yes", "No"]},
    {"field_id": "sponsorship", "label": "Will you now or in the future require visa sponsorship?", "type": "radio",
     "options": ["Yes", "No"]},
    {"field_id": "relocate", "label": "Are you willing to relocate?", "type": "radio", "options": ["Yes", "No"]},
    {"field_id": "hear", "label": "How did you hear about this opportunity?", "type": "select",
     "options": ["LinkedIn", "Company website", "Referral", "Job board", "Other"]},
    {"field_id": "why", "label": "Why do you want to work here?", "type": "textarea"},
]


def _version() -> str:
    try:
        return json.loads(_MANIFEST.read_text(encoding="utf-8")).get("version", "")
    except (OSError, ValueError):
        return ""


def _applications(db: Session):
    return (db.query(JobApplication).filter(JobApplication.status.notin_(["Rejected", "Not Selected"]))
            .order_by(JobApplication.id.desc()).limit(30).all())


def _page(request: Request, db: Session, application_id: int | None = None, answers: dict | None = None):
    apps = _applications(db)
    approved = next((a for a in apps if a.status == "Approved"), apps[0] if apps else None)
    return render(request, "extension.html", {
        "version": _version(),
        "platforms": TESTED_PLATFORMS,
        "fields": SAMPLE_FIELDS,
        "applications": apps,
        "selected_id": application_id or (approved.id if approved else None),
        "answers": answers,
    })


@router.get("", response_class=HTMLResponse)
def extension_page(request: Request, db: Session = Depends(get_db)):
    return _page(request, db)


@router.post("/sample-fill", response_class=HTMLResponse)
def sample_fill(request: Request, application_id: int = Form(...), db: Session = Depends(get_db)):
    # The resolver answers radio groups through the select path, exactly as the content script sends them.
    fields = [dict(f, type="select") if f["type"] == "radio" else f for f in SAMPLE_FIELDS]
    answers = extension_service.resolve_field_answers(db, application_id, fields)
    return _page(request, db, application_id, answers)
