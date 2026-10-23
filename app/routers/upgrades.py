"""
Upgrades page (/upgrades): the monthly upgrade check's latest report, with a button to run it now and the
forms that turn an item into a pending profile version. See app/services/upgrade_service.py.
"""

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import upgrade_service
from ..services.upgrade_service import UpgradeServiceError
from ..templating import render

router = APIRouter(prefix="/upgrades", tags=["upgrades"])


def _back(message: str | None = None, error: str | None = None, anchor: str = "") -> RedirectResponse:
    query = f"?message={quote(message)}" if message else f"?error={quote(error)}" if error else ""
    return RedirectResponse(url=f"/upgrades{query}{anchor}", status_code=303)


@router.get("", response_class=HTMLResponse)
def upgrades_page(request: Request, db: Session = Depends(get_db)):
    row = upgrade_service.latest_report(db)
    return render(request, "upgrades.html", {
        "row": row,
        "report": json.loads(row.report_json) if row else None,
        "count": upgrade_service.action_count(json.loads(row.report_json)) if row else 0,
        "run_every_days": upgrade_service.RUN_EVERY_DAYS,
        "max_answer_chars": upgrade_service.MAX_ANSWER_CHARS,
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
    })


@router.post("/run")
def run_now(db: Session = Depends(get_db)):
    try:
        row = upgrade_service.run_check(db)
    except UpgradeServiceError as e:
        return _back(error=str(e))
    count = upgrade_service.action_count(json.loads(row.report_json))
    return _back(message=f"Check finished: {count} item{'s' if count != 1 else ''} to look at.")


@router.post("/answer")
def answer(target: str = Form(...), answer: str = Form(""), db: Session = Depends(get_db)):
    try:
        upgrade_service.draft_answer(db, target, answer)
    except UpgradeServiceError as e:
        return _back(error=str(e), anchor="#prompts")
    return _back(message="Saved as a pending profile change. Approve or reject it on the Profile page.", anchor="#prompts")


@router.post("/languages")
def languages(project_index: int = Form(...), languages: str = Form(""), db: Session = Depends(get_db)):
    try:
        upgrade_service.draft_languages(db, project_index, [x.strip() for x in languages.split(",")])
    except UpgradeServiceError as e:
        return _back(error=str(e), anchor="#repos")
    return _back(message="Saved as a pending profile change. Approve or reject it on the Profile page.", anchor="#repos")
