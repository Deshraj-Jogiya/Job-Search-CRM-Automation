"""The /sponsors lookup page: search, filters and ordering over the H-1B data already on Company rows."""
from app.models import Company
from app.routers.sponsors import search_companies


def _company(db, name, tier, approvals=None, cap_exempt=False, alert=None):
    from app.services.company_utils import normalize_company_name

    c = Company(name=name, normalized_name=normalize_company_name(name), tier=tier, h1b_approvals_total=approvals,
                is_cap_exempt=cap_exempt, sponsorship_alert=alert,
                sponsorship_alert_kind="debarred" if alert else None)
    db.add(c)
    db.commit()
    return c


def test_strongest_sponsor_comes_first_and_filters_narrow_the_list(db):
    _company(db, "Small Shop", "C", 1)
    _company(db, "Big Sponsor Inc", "A", 900)
    _company(db, "Mid Sponsor", "B", 40)
    _company(db, "State University", "C", None, cap_exempt=True)
    _company(db, "Banned Corp", "X", 12, alert="Debarred by DOL WHD")

    rows, total = search_companies(db)
    assert total == 5 and [c.name for c in rows][:2] == ["Big Sponsor Inc", "Mid Sponsor"]
    assert [c.name for c in search_companies(db, cap_exempt=True)[0]] == ["State University"]
    assert [c.name for c in search_companies(db, alerts=True)[0]] == ["Banned Corp"]
    assert [c.name for c in search_companies(db, tier="C")[0]] == ["Small Shop", "State University"]
    assert [c.name for c in search_companies(db, q="big sponsor")[0]] == ["Big Sponsor Inc"]


def test_page_renders_records_and_the_no_record_note(db):
    from fastapi.testclient import TestClient
    from app.main import app as web_app

    _company(db, "Banned Corp", "X", 12, alert="Debarred by DOL WHD")
    page = TestClient(web_app).get("/sponsors?q=banned").text
    assert "Banned Corp" in page and "debarred" in page and "Debarred by DOL WHD" in page
    assert "1 company" in page
    assert "No company matches" in TestClient(web_app).get("/sponsors?q=nothing-like-this").text
