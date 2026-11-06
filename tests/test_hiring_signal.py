"""Hiring signal: freezes, layoffs and job-board activity decide where job-search effort goes."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.ingest.hiring import (_workday_age, add_evidence, apply_hiring_alerts, board_is_falling, count_board, count_linkedin_cards,
                               parse_insights, refresh_own_record)
from app.models import Company
from app.services.company_utils import normalize_company_name
from app.services.hiring_signal import ALERTS_PATH, alert_for_name, assess, for_company, for_name, insights_trend


def _company(db, name, **fields):
    company = Company(name=name, normalized_name=normalize_company_name(name), **fields)
    db.add(company)
    db.commit()
    return company


@pytest.mark.parametrize("args,state", [
    (dict(kind="freeze", scope="company"), "frozen"),
    (dict(kind="freeze", scope="company", board_open=50, data_30=0, data_90=4), "frozen"),
    (dict(kind="freeze", scope="company", board_open=50, data_30=3), "cuts_hiring"),  # the board beats an old report
    (dict(kind="freeze", scope="part", board_open=50, data_30=3), "cuts_hiring"),  # Microsoft: two units paused, engineering openings up
    (dict(kind="freeze", scope="part", board_open=50, data_30=3, evidence="insider"), "part_frozen"),  # someone inside outranks totals
    (dict(kind="freeze", scope="part"), "part_frozen"),
    (dict(kind="tracker", scope="company"), "tracker_frozen"),  # a tracker alone never drops an employer
    (dict(kind="tracker", scope="company", trend="rising"), "cuts_hiring"),
    (dict(trend="rising"), "hiring"),
    (dict(trend="falling"), "postings_falling"),  # Mayo Clinic: openings down 68% in 3 months
    (dict(trend="falling", board_open=40, data_30=2), "hiring"),  # data roles still being published
    (dict(board_open=40, data_30=0, data_90=2, falling_board=True), "postings_falling"),
    (dict(linkedin_30=7), "hiring"),
    (dict(roles_pulled=2), "roles_pulled"),
    (dict(roles_pulled=3, linkedin_30=20), "hiring"),  # Amazon: cancelled three of his roles, still publishing many
    (dict(roles_pulled=1), "unknown"),
    (dict(kind="layoff", scope="company", percent=15, board_open=160, data_30=13), "cuts_hiring"),
    (dict(kind="layoff", scope="company", percent=15), "cuts"),
    (dict(kind="layoff", scope="company", percent=22), "deep_cuts"),
    (dict(kind="layoff", scope="part"), "part_cuts"),  # a small cut in one group of a big employer
    (dict(board_open=300, data_30=11, data_90=16), "hiring"),
    (dict(board_open=30, data_30=0, data_90=2), "slow"),
    (dict(board_open=30, data_30=0, data_90=0), "quiet"),
    (dict(board_open=0, data_30=0, data_90=0), "unknown"),  # an empty board may just have moved
    (dict(), "unknown"),
])
def test_assess(args, state):
    assert assess(**args)["state"] == state


def test_similar_names_do_not_pick_up_another_companys_alert():
    assert alert_for_name("Flock Safety")["kind"] == "layoff"
    assert alert_for_name("Flock Freight") is None
    assert alert_for_name("Tessera Labs") is None


def test_an_alert_stops_counting_after_its_end_date():
    assert alert_for_name("Microchip Technology Inc.", today=date(2026, 11, 6))["kind"] == "freeze"
    assert alert_for_name("Microchip Technology Inc.", today=date(2027, 6, 1)) is None


def test_an_untracked_company_still_gets_its_alert():
    assert for_name("Microchip Technology", None, today=date(2026, 11, 6))["state"] == "part_frozen"
    assert for_name("Rutgers University", None, today=date(2026, 11, 6))["state"] == "frozen"
    assert for_name("Some Unlisted Startup", None)["state"] == "unknown"


def test_alert_list_is_complete_and_names_no_private_person():
    data = json.load(open(ALERTS_PATH, encoding="utf-8"))
    for alert in data["alerts"]:
        assert alert["names"] and alert["kind"] in ("freeze", "tracker", "layoff", "warn") and alert["scope"] in ("company", "part")
        assert alert["evidence"] in ("insider", "confirmed", "reported", "tracker")
        assert date.fromisoformat(alert["date"]) <= date.fromisoformat(alert["until"])
        assert "linkedin.com/in/" not in alert["source"]


def test_apply_flags_tracked_companies_and_clears_old_alerts(db, tmp_path):
    amazon = _company(db, "Amazon")
    freight = _company(db, "Flock Freight")
    stale = _company(db, "Old News Inc", hiring_alert_kind="layoff", hiring_alert="old")
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps({"alerts": [{"names": ["Amazon"], "kind": "layoff", "scope": "part", "note": "cuts", "percent": 5,
                                            "until": "2099-01-01", "source": "https://example.com"}]}), encoding="utf-8")
    result = apply_hiring_alerts(db, path)
    assert result["companies_flagged"] == ["Amazon (layoff)"]
    assert amazon.hiring_alert == "cuts" and freight.hiring_alert is None and stale.hiring_alert is None
    assert for_company(amazon)["state"] == "part_cuts"


def test_board_count_keeps_only_us_data_roles_and_dates_only_count_when_present(db):
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    recent = (now - timedelta(days=5)).isoformat()
    older = (now - timedelta(days=60)).isoformat()
    rows = [
        ("Data Engineer", "Remote - US", recent),
        ("Analytics Engineer", "New York, NY", older),
        ("ML Engineer", "Austin, TX", None),
        ("Data Center Technician", "Phoenix, AZ", recent),
        ("Principal Engineer, Data Converters", "Chandler, AZ", recent),
        ("Account Executive", "Chicago, IL", recent),
        ("Data Engineer", "Berlin, Germany", recent),
    ]
    assert count_board(rows, ["germany"], now) == {"total": 7, "data": 3, "new_30": 1, "new_90": 2, "all_30": 5, "all_31_90": 1}


def test_queue_puts_a_frozen_employer_last_whatever_its_score(db):
    from app.models import JobApplication, JobPosting
    from app.services.queue_service import _sorted_by_score

    apps = []
    for name, total, fields in (("Frozen Co", 95, dict(hiring_alert_kind="freeze", hiring_alert_scope="company", hiring_alert="stopped")),
                                ("Open Co", 60, {})):
        company = _company(db, name, **fields)
        posting = JobPosting(company_id=company.id, company_name_raw=name, job_title="Data Engineer", job_url=f"https://x/{name}", job_description="Build pipelines", source="manual")
        db.add(posting)
        db.commit()
        application = JobApplication(posting_id=posting.id, status="Ingested", score_breakdown={"total": total})
        db.add(application)
        db.commit()
        apps.append(application)
    assert [a.posting.company.name for a in _sorted_by_score(apps)] == ["Open Co", "Frozen Co"]


def test_sponsors_page_shows_the_signal_and_filters_on_new_data_roles(db):
    from fastapi.testclient import TestClient
    from app.main import app as web_app
    from app.routers.sponsors import search_companies

    _company(db, "Busy Co", tier="A", board_open_total=100, board_data_open=9, board_data_new_30d=4, board_data_new_90d=7)
    _company(db, "Still Co", tier="A", board_open_total=100, board_data_open=0, board_data_new_30d=0, board_data_new_90d=0)
    assert [c.name for c in search_companies(db, hiring=True)[0]] == ["Busy Co"]
    page = TestClient(web_app).get("/sponsors").text
    assert "Hiring data roles" in page and "9 US data roles open, 4 new in 30 days" in page and "Quiet: no new data role" in page


@pytest.mark.parametrize("args,trend", [
    (dict(total_3m=-68), "falling"),  # Mayo Clinic
    (dict(total_3m=120, tech_3m=-11, tech_6m=-65), "falling"),  # PayPal: the total hides engineering
    (dict(total_3m=4, tech_3m=11, tech_6m=4), "rising"),  # Microchip
    (dict(total_3m=-10, tech_3m=-4, tech_6m=25), "flat"),  # Twilio
    (dict(), None),
])
def test_insights_trend(args, trend):
    assert insights_trend(**args) == trend


def test_parse_insights_keeps_the_arrow_as_the_sign():
    text = ("Functions\n7,905Q3 2026 Job openings growth 3m 6m Engineering ▲ 79% ▲ 89% Product Management ▲ 57% ▼ 47% "
            "Information Technology ▲ 25% ▲ 18% Total ▼ 42% ▲ 50% Total employee count 68,672")
    assert parse_insights(text) == {"openings": 7905, "total_3m": -42, "total_6m": 50, "tech_3m": 79, "tech_6m": 89}
    assert parse_insights("nothing here") is None


def test_a_falling_board_needs_a_real_drop():
    assert board_is_falling(50, 5, 10, earlier_open=100)
    assert not board_is_falling(90, 5, 10, earlier_open=100)
    assert board_is_falling(50, 1, 20)  # usual month 10, this month 1
    assert not board_is_falling(50, 1, 4)  # too few jobs to judge


def test_workday_ages():
    now = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert (now - datetime.fromisoformat(_workday_age("Posted 9 Days Ago"))).days == 9
    assert (now - datetime.fromisoformat(_workday_age("Posted 30+ Days Ago"))).days == 31
    assert (now - datetime.fromisoformat(_workday_age("Posted Today"))).days == 0
    assert _workday_age(None) is None


def test_linkedin_count_keeps_only_the_exact_employer():
    cards = [("Oracle", "Data Engineer"), ("Oracle", "Sales Director"), ("Oracle Staffing Partners", "Data Engineer")]
    assert count_linkedin_cards(cards, normalize_company_name("Oracle")) == 1


def test_an_official_notice_alerts_a_company_with_no_hand_checked_alert(db, tmp_path):
    quiet = _company(db, "Quiet Co")
    listed = _company(db, "Listed Co")
    add_evidence(db, quiet.name, "warn", date(2026, 10, 1), "WARN notice, AZ: 120 workers", "https://x", quiet)
    add_evidence(db, listed.name, "warn", date(2026, 10, 1), "WARN notice, AZ: 120 workers", "https://x", listed)
    assert add_evidence(db, quiet.name, "warn", date(2026, 10, 1), "WARN notice, AZ: 120 workers", "https://x", quiet) is False
    db.commit()
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps({"alerts": [{"names": ["Listed Co"], "kind": "layoff", "scope": "company", "note": "hand note", "until": "2099-01-01", "source": "https://y"}]}), encoding="utf-8")
    apply_hiring_alerts(db, path)
    assert quiet.hiring_alert_kind == "warn" and quiet.hiring_alert_scope == "part" and quiet.hiring.get("state") in ("part_cuts", "cuts")
    assert listed.hiring_alert == "hand note"


def test_own_record_counts_recent_cancelled_roles_only(db):
    company = _company(db, "Cancels Co")
    today = date(2026, 11, 6)
    for day, note in ((date(2026, 10, 1), "a"), (date(2026, 9, 1), "b"), (date(2026, 1, 1), "old")):
        add_evidence(db, company.name, "cancelled", day, note, None, company)
    db.commit()
    refresh_own_record(db, today)
    assert company.roles_pulled_120d == 2
    assert for_company(company)["state"] == "roles_pulled"


def test_evidence_line_shows_what_was_measured(db):
    company = _company(db, "Measured Co", li_openings=749, li_openings_3m_pct=4, li_tech_openings_3m_pct=11, linkedin_data_new_30d=3)
    line = for_company(company)["board"]
    assert "LinkedIn openings 749 (+4% in 3 months, engineering or IT +11%)" in line and "3 data roles on LinkedIn" in line
