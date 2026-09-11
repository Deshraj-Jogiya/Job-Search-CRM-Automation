"""
Weekly-rollup metrics dashboard (/metrics). Read-only, same shape as
analytics.py -- one GET rendering whatever metrics_service.py computes.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import metrics_service
from ..templating import render

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("", response_class=HTMLResponse)
def metrics_page(request: Request, db: Session = Depends(get_db)):
    dashboard = metrics_service.build_dashboard(db)
    return render(request, "metrics.html", dashboard)
