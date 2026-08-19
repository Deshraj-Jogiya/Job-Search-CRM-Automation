"""
Phase 7 route: the outcome-analytics dashboard. Read-only -- no
mutating actions live here, just a single GET rendering everything
analytics_service.py computes.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import analytics_service
from ..templating import render

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("", response_class=HTMLResponse)
def analytics_page(request: Request, db: Session = Depends(get_db)):
    dashboard = analytics_service.build_dashboard(db)
    return render(request, "analytics.html", dashboard)
