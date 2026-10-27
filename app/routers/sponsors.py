"""
Sponsors page (/sponsors): look up an employer's H-1B record from the data already loaded onto Company rows
(USCIS approvals/denials, DOL data-role LCA filings and their highest wage level, cap-exempt status, tier and any
current government alert). The same page runs on both instances; on the showcase the companies are the demo's
fictional ones, so their records are sample data and the page says so.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Company
from ..services.company_utils import normalize_company_name
from ..templating import render

router = APIRouter(prefix="/sponsors", tags=["sponsors"])

PAGE_SIZE = 50
TIERS = ("A", "B", "C", "X")
_TIER_ORDER = case({"A": 0, "B": 1, "C": 2, "X": 3}, value=Company.tier, else_=4)


def search_companies(db: Session, q: str = "", tier: str = "", cap_exempt: bool = False, alerts: bool = False):
    """Matching companies, strongest sponsor first, and the total number that matched."""
    query = db.query(Company)
    q = (q or "").strip()
    if q:
        normalized = normalize_company_name(q)
        conditions = [Company.name.ilike(f"%{q}%")]
        if normalized:
            conditions.append(Company.normalized_name.like(f"%{normalized}%"))
        query = query.filter(or_(*conditions))
    if tier in TIERS:
        query = query.filter(Company.tier == tier)
    if cap_exempt:
        query = query.filter(Company.is_cap_exempt.is_(True))
    if alerts:
        query = query.filter(Company.sponsorship_alert.isnot(None))
    total = query.count()
    rows = (query.order_by(_TIER_ORDER, func.coalesce(Company.h1b_approvals_total, 0).desc(),
                           func.coalesce(Company.lca_filings_total, 0).desc(), Company.name)
            .limit(PAGE_SIZE).all())
    return rows, total


def tier_counts(db: Session) -> dict:
    counts = dict(db.query(Company.tier, func.count(Company.id)).group_by(Company.tier).all())
    return {t: counts.get(t, 0) for t in TIERS} | {"unknown": counts.get(None, 0)}


@router.get("", response_class=HTMLResponse)
def sponsors_page(request: Request, q: str = "", tier: str = "", cap_exempt: str = "", alerts: str = "",
                  db: Session = Depends(get_db)):
    rows, total = search_companies(db, q, tier, bool(cap_exempt), bool(alerts))
    return render(request, "sponsors.html", {
        "rows": rows,
        "total": total,
        "page_size": PAGE_SIZE,
        "q": q.strip(),
        "tier": tier if tier in TIERS else "",
        "cap_exempt": bool(cap_exempt),
        "alerts": bool(alerts),
        "tiers": TIERS,
        "counts": tier_counts(db),
    })
