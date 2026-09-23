"""
/adaptation -- Part B's audit trail + Tier 2 comparison dashboard.
Read-heavy (every named comparison's current readiness state, every
AdaptationLog entry) plus the mutating actions b2/b3.3 call for:
approve/reject a Tier 2 proposal, revert one change, revert everything
since a given date.

Snooze is deliberately NOT a durable action -- AdaptationLog.status's
own CHECK constraint (see the migration) only allows applied/proposed/
approved/rejected/reverted, and this pass's schema-change budget was
already spent on adaptation_log/adaptive_parameter_values (see
CLAUDE.md's migration-chain discipline: one new migration per pass).
Snoozing just dismisses the current page render; the same comparison
re-evaluates fresh, from real current data, the next time this page
loads -- honest given the constraint, not silently faked as persistent.
"""

from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AdaptationLog
from ..services import adaptation_service, trend_research_service
from ..templating import render

router = APIRouter(prefix="/adaptation", tags=["adaptation"])

_COMPARISON_TYPES = ("resume_variant", "source", "tier", "wage_level")
_COMPARISON_LABELS = {
    "resume_variant": "Resume Variant",
    "source": "Source",
    "tier": "Company Tier",
    "wage_level": "Wage Level",
}


def _redirect(message: str = None, error: str = None) -> RedirectResponse:
    url = "/adaptation"
    if error:
        url += "?error=" + quote(error)
    elif message:
        url += "?message=" + quote(message)
    return RedirectResponse(url=url, status_code=303)


@router.get("", response_class=HTMLResponse)
def adaptation_page(request: Request, db: Session = Depends(get_db)):
    comparisons = [
        {"type": ct, "label": _COMPARISON_LABELS[ct], **adaptation_service.evaluate_comparison(db, ct)}
        for ct in _COMPARISON_TYPES
    ]
    # Real gap found live 2026-09-23, during a full UI audit: this table
    # was already correctly capped at 100 rows (unlike the 3 other
    # unbounded-list bugs found earlier this same audit), but silently --
    # 178 real rows existed, and nothing on the page said so. A page
    # whose whole stated purpose is "nothing here ever applies itself...
    # never a silent change" quietly truncating its own audit trail with
    # no count is the same class of honesty gap, just smaller.
    log_entries_total = db.query(AdaptationLog).count()
    log_entries = db.query(AdaptationLog).order_by(AdaptationLog.created_at.desc()).limit(100).all()
    trend_proposals = [
        {"entry": entry, "label": trend_research_service.TREND_CATEGORIES[entry.parameter].label}
        for entry in trend_research_service.pending_trend_proposals(db)
        if entry.parameter in trend_research_service.TREND_CATEGORIES
    ]
    return render(
        request,
        "adaptation.html",
        {
            "comparisons": comparisons,
            "log_entries": log_entries,
            "log_entries_total": log_entries_total,
            "trend_proposals": trend_proposals,
            "trend_categories": trend_research_service.TREND_CATEGORIES,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/trends/check")
def check_trends_now(db: Session = Depends(get_db)):
    from datetime import date
    created = trend_research_service.run_all_trend_checks(db, date.today().year)
    if not created:
        return _redirect(message="Checked -- nothing new to propose right now (current settings already match research, or Tavily isn't configured).")
    return _redirect(message=f"Found {len(created)} potential update(s) -- review below.")


@router.post("/trends/{log_id}/approve")
def approve_trend(log_id: int, db: Session = Depends(get_db)):
    try:
        entry = trend_research_service.approve_trend_proposal(db, log_id)
        return _redirect(message=f"Applied: {entry.parameter} {entry.old_value} -> {entry.new_value}.")
    except trend_research_service.TrendProposalError as e:
        return _redirect(error=str(e))


@router.post("/trends/{log_id}/reject")
def reject_trend(log_id: int, db: Session = Depends(get_db)):
    try:
        trend_research_service.reject_trend_proposal(db, log_id)
        return _redirect(message="Noted -- proposal rejected.")
    except trend_research_service.TrendProposalError as e:
        return _redirect(error=str(e))


@router.post("/comparisons/{comparison_type}/approve")
def approve_proposal(comparison_type: str, db: Session = Depends(get_db)):
    try:
        entry = adaptation_service.approve_comparison_proposal(db, comparison_type)
        return _redirect(message=f"Applied: {entry.parameter} {entry.old_value} -> {entry.new_value}.")
    except adaptation_service.AdaptationServiceError as e:
        return _redirect(error=str(e))


@router.post("/comparisons/{comparison_type}/reject")
def reject_proposal(comparison_type: str, db: Session = Depends(get_db)):
    adaptation_service.reject_comparison_proposal(db, comparison_type)
    return _redirect(message="Noted -- proposal rejected.")


@router.post("/comparisons/{comparison_type}/snooze")
def snooze_proposal(comparison_type: str):
    return _redirect(message="Snoozed -- it'll show again next time you're on this page.")


@router.post("/{log_id}/revert")
def revert_one(log_id: int, db: Session = Depends(get_db)):
    try:
        adaptation_service.revert_adaptation(db, log_id)
        return _redirect(message="Reverted.")
    except adaptation_service.AdaptationServiceError as e:
        return _redirect(error=str(e))


@router.post("/revert-all-since")
def revert_all_since(since: str = Form(...), db: Session = Depends(get_db)):
    try:
        parsed = datetime.fromisoformat(since)
    except ValueError:
        return _redirect(error=f"'{since}' isn't a valid date.")
    reverted = adaptation_service.revert_all_since(db, parsed)
    return _redirect(message=f"Reverted {len(reverted)} change(s) since {since}.")
