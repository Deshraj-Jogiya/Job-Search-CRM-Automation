"""Keeps each tracked employer's hiring signal current from free public sources: the hand-checked alert list, job boards (Greenhouse,
Lever, Ashby, Workday), LinkedIn job cards, LinkedIn Insights figures, state WARN files, SEC filings, NIH award counts and his own
application record. See services/hiring_signal.py for how they are weighed."""

import csv
import html
import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import Company, CompanyBoardSnapshot, HiringEvidence, JobApplication, JobPosting
from ..services.company_utils import normalize_company_name
from ..services.hiring_signal import ALERTS_PATH
from ..services.sources.keyword_matching import get_active_location_exclusions, location_eligible_for_scoring
from .sponsors import _file_keys_for, _index, log_activity

DATA_TITLE = re.compile(r"\bdata\b(?! cent| convert)|machine learning|\bml\b|\bai\b|analytics|business intelligence", re.IGNORECASE)


def apply_hiring_alerts(db: Session, path=ALERTS_PATH) -> dict:
    """Replaces every company's hiring alert with what the hand-checked list says. Listed employers Career Pilot does not track
    yet are still found by name in job and networking runs."""
    with open(path, encoding="utf-8") as f:
        alerts = json.load(f)["alerts"]
    by_name: dict[str, list[Company]] = {}
    for company in db.query(Company).all():
        company.hiring_alert_kind = company.hiring_alert_scope = company.hiring_alert_percent = None
        company.hiring_alert = company.hiring_alert_until = company.hiring_alert_source = company.hiring_alert_evidence = None
        by_name.setdefault(company.normalized_name, []).append(company)
    index = _index(by_name)
    flagged = []
    for alert in alerts:
        for name in alert["names"]:
            for key in _file_keys_for(normalize_company_name(name), index, {}):
                for company in by_name[key]:
                    company.hiring_alert_kind = alert["kind"]
                    company.hiring_alert_scope = alert["scope"]
                    company.hiring_alert_percent = alert.get("percent")
                    company.hiring_alert = alert["note"]
                    company.hiring_alert_until = date.fromisoformat(alert["until"])
                    company.hiring_alert_source = alert["source"]
                    company.hiring_alert_evidence = alert.get("evidence")
                    flagged.append(f"{company.name} ({alert['kind']})")
    _apply_evidence(db)
    db.commit()
    log_activity(db, f"Hiring alerts: {len(alerts)} on file, applied to {len(set(flagged))} tracked companies.", "INFO")
    return {"alerts_on_file": len(alerts), "companies_flagged": sorted(set(flagged))}


def _parse(value):
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, timezone.utc)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (AttributeError, TypeError, ValueError):
        return None


def _workday_age(text: str | None) -> str | None:
    """Workday says 'Posted 9 Days Ago', 'Posted Today' or 'Posted 30+ Days Ago'. 30+ is stored as 31 days: older than a month,
    exact age unknown."""
    text = (text or "").lower()
    match = re.search(r"(\d+)\+? days? ago", text)
    days = 0 if "today" in text else 1 if "yesterday" in text else (int(match.group(1)) + ("+" in text)) if match else None
    return None if days is None else (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _workday_rows(board: str) -> tuple[list[tuple], int]:
    """(data-search rows, total open jobs). Workday's public job API needs the real tenant, data centre and site name."""
    tenant, data_centre, site = board.split("|")
    url = f"https://{tenant}.{data_centre}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json", "Accept": "application/json"}

    def page(text, offset):
        response = httpx.post(url, json={"limit": 20, "offset": offset, "searchText": text, "appliedFacets": {}}, headers=headers, timeout=20)
        response.raise_for_status()
        return response.json()

    total = page("", 0)["total"]
    rows, offset = [], 0
    while offset < 100:
        jobs = page("data", offset).get("jobPostings", [])
        rows += [(j.get("title"), j.get("locationsText"), _workday_age(j.get("postedOn"))) for j in jobs]
        if len(jobs) < 20:
            break
        offset += 20
    return rows, total


def _board_rows(greenhouse=None, lever=None, ashby=None) -> list[tuple]:
    """(title, location, published) for each open job on the employer's board."""
    if greenhouse:
        jobs = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{greenhouse}/jobs", timeout=20).json()["jobs"]
        return [(j["title"], (j.get("location") or {}).get("name"), j.get("first_published")) for j in jobs]
    if lever:
        jobs = httpx.get(f"https://api.lever.co/v0/postings/{lever}?mode=json", timeout=20).json()
        return [(j["text"], (j.get("categories") or {}).get("location"), j.get("createdAt")) for j in jobs]
    jobs = httpx.get(f"https://api.ashbyhq.com/posting-api/job-board/{ashby}", timeout=20).json()["jobs"]
    return [(j["title"], j.get("location"), j.get("publishedAt")) for j in jobs]


def count_board(rows: list[tuple], deny: list, now: datetime) -> dict:
    """Counts US data roles. A job with no publish date counts as open, never as new."""
    data = [r for r in rows if DATA_TITLE.search(r[0] or "") and location_eligible_for_scoring(r[1] or "", deny) is not False]
    ages = [(now - p).days for p in (_parse(r[2]) for r in data) if p]
    every = [(now - p).days for p in (_parse(r[2]) for r in rows) if p]
    return {"total": len(rows), "data": len(data), "new_30": sum(a <= 30 for a in ages), "new_90": sum(a <= 90 for a in ages),
            "all_30": sum(a <= 30 for a in every), "all_31_90": sum(30 < a <= 90 for a in every)}


def board_is_falling(open_now: int, all_30: int, all_31_90: int, earlier_open: int | None = None) -> bool:
    """Open jobs down 40% against a snapshot at least a week old, or far fewer published this month than the two months before."""
    if earlier_open and earlier_open >= 10 and open_now <= earlier_open * 0.6:
        return True
    usual = all_31_90 / 2
    return usual >= 5 and all_30 < usual / 3


def _earlier_open(db: Session, company_id: int, today: date) -> int | None:
    row = (db.query(CompanyBoardSnapshot).filter(CompanyBoardSnapshot.company_id == company_id, CompanyBoardSnapshot.taken_on <= today - timedelta(days=7))
           .order_by(CompanyBoardSnapshot.taken_on.desc()).first())
    return row.open_total if row else None


def refresh_board_activity(db: Session, max_age_days: int = 0) -> dict:
    """Reads the board of every employer worth checking (not tier X, not Blocked). With max_age_days, a recent read is kept."""
    deny = get_active_location_exclusions()
    now = datetime.now(timezone.utc)
    stale_before = utcnow() - timedelta(days=max_age_days)
    companies = [c for c in db.query(Company).all()
                 if (c.greenhouse_slug or c.lever_slug or c.ashby_slug or c.workday_board) and c.tier != "X" and c.status != "Blocked"
                 and (not max_age_days or c.board_checked_at is None or c.board_checked_at < stale_before)]

    def read(slugs):
        try:
            if slugs[3] and not any(slugs[:3]):
                rows, total = _workday_rows(slugs[3])
                return {**count_board(rows, deny, now), "total": total}
            return count_board(_board_rows(*slugs[:3]), deny, now)
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return None

    with ThreadPoolExecutor(10) as pool:
        counts = list(pool.map(read, [(c.greenhouse_slug, c.lever_slug, c.ashby_slug, c.workday_board) for c in companies]))
    read_ok = 0
    for company, count in zip(companies, counts):
        if count is None:  # a failed read leaves the last good numbers in place
            continue
        read_ok += 1
        company.board_open_total, company.board_data_open = count["total"], count["data"]
        company.board_data_new_30d, company.board_data_new_90d = count["new_30"], count["new_90"]
        company.board_new_30d_all, company.board_new_31_90d_all = count["all_30"], count["all_31_90"]
        company.board_falling = board_is_falling(count["total"], count["all_30"], count["all_31_90"], _earlier_open(db, company.id, now.date()))
        company.board_checked_at = utcnow()
        snapshot = db.query(CompanyBoardSnapshot).filter_by(company_id=company.id, taken_on=now.date()).first()
        if snapshot is None:
            db.add(CompanyBoardSnapshot(company_id=company.id, taken_on=now.date(), open_total=count["total"], data_open=count["data"],
                                        new_30d=count["new_30"], new_90d=count["new_90"]))
    db.commit()
    return {"boards_to_read": len(companies), "read": read_ok, "failed": len(companies) - read_ok}


def _tracked(db: Session) -> dict:
    return {c.normalized_name: c for c in db.query(Company).all() if c.normalized_name}


def add_evidence(db: Session, name: str, kind: str, event_date: date, note: str, source: str | None = None, company=None) -> bool:
    """Saves a dated fact once. Returns True when it is new."""
    note = note[:500]
    if db.query(HiringEvidence).filter_by(company_name=name, kind=kind, event_date=event_date, note=note).first():
        return False
    db.add(HiringEvidence(company_id=company.id if company else None, company_name=name, kind=kind, event_date=event_date, note=note, source=source))
    db.flush()
    return True


def _apply_evidence(db: Session, today: date | None = None) -> None:
    """An employer with no hand-checked alert still gets one from an official notice in the last 180 days: a WARN notice or an SEC
    restructuring filing. Scope is 'part' because a notice covers a site or a plan, not the whole company."""
    since = (today or date.today()) - timedelta(days=180)
    rows = db.query(HiringEvidence).filter(HiringEvidence.kind.in_(("warn", "filing")), HiringEvidence.event_date >= since,
                                           HiringEvidence.company_id.isnot(None)).order_by(HiringEvidence.event_date).all()
    for row in rows:
        company = db.get(Company, row.company_id)
        if company.hiring_alert_kind and company.hiring_alert_evidence != "filing":
            continue
        company.hiring_alert_kind = "warn" if row.kind == "warn" else "layoff"
        company.hiring_alert_scope, company.hiring_alert_evidence = "part", "filing"
        company.hiring_alert, company.hiring_alert_source = row.note, row.source
        company.hiring_alert_until = row.event_date + timedelta(days=180)


LINKEDIN_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"


def linkedin_cards(page: str) -> list[tuple]:
    """(employer, title) from one page of LinkedIn's public job search."""
    cards = []
    for card in page.split("<li>")[1:]:
        title = re.search(r"base-search-card__title[^>]*>\s*(.*?)\s*<", card, re.S)
        employer = re.search(r"hidden-nested-link[^>]*>\s*(.*?)\s*<", card, re.S)
        if title and employer:
            cards.append((html.unescape(employer.group(1)), html.unescape(title.group(1))))
    return cards


def count_linkedin_cards(cards: list[tuple], normalized_name: str) -> int:
    """Only this exact employer: a search for 'Oracle' also returns staffing firms and jobs that merely mention it."""
    return sum(normalize_company_name(employer) == normalized_name and bool(DATA_TITLE.search(title)) for employer, title in cards)


def refresh_linkedin_counts(db: Session, limit: int = 60, max_age_days: int = 7) -> dict:
    """Data roles posted in 30 days on LinkedIn, for employers whose own board cannot be read. Stops at the first refusal."""
    stale_before = utcnow() - timedelta(days=max_age_days)
    companies = [c for c in db.query(Company).filter(Company.tier.in_(("A", "B"))).all()
                 if not c.board_open_total and c.status != "Blocked" and (c.linkedin_checked_at is None or c.linkedin_checked_at < stale_before)][:limit]
    read = 0
    for company in companies:
        response = httpx.get(LINKEDIN_SEARCH, params={"keywords": f'"{company.name}" data', "location": "United States", "f_TPR": "r2592000"},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
        if response.status_code != 200:
            break
        company.linkedin_data_new_30d = count_linkedin_cards(linkedin_cards(response.text), company.normalized_name)
        company.linkedin_checked_at = utcnow()
        read += 1
        time.sleep(1)
    db.commit()
    return {"linkedin_to_read": len(companies), "linkedin_read": read}


_WARN_HF = "https://datasets-server.huggingface.co/filter"
_WARN_DATASET = "APProjects/us-warn-act-layoffs-notices-daily"
_WARN_CA = "https://edd.ca.gov/siteassets/files/jobs_and_training/warn/warn_report1.xlsx"


def _warn_california() -> list[dict]:
    """California's own file. It is the official record, and the fallback when the national copy is down."""
    import openpyxl

    book = openpyxl.load_workbook(io.BytesIO(httpx.get(_WARN_CA, headers={"User-Agent": "Mozilla/5.0"}, timeout=40).content), read_only=True, data_only=True)
    sheet = next(book[s] for s in book.sheetnames if s.lower().startswith("detailed"))
    rows = []
    for row in sheet.iter_rows(min_row=3, values_only=True):
        if isinstance(row[1], datetime) and row[4]:
            rows.append({"company": str(row[4]), "state": "CA", "workers": int(row[6] or 0), "date": row[1].date(), "place": row[7] or "", "source": _WARN_CA})
    return rows


def _warn_national(since: date) -> list[dict]:
    """A daily copy of 40+ states' notices, CC BY 4.0. Temporary errors are normal, so a failure returns what was read."""
    rows, offset = [], 0
    where = f"\"notice_date\">='{since.isoformat()}'"
    while offset < 6000:
        try:
            response = httpx.get(_WARN_HF, params={"dataset": _WARN_DATASET, "config": "default", "split": "train", "where": where, "length": 100, "offset": offset}, timeout=40)
        except httpx.HTTPError:
            break
        if response.status_code != 200:
            break
        batch = [item["row"] for item in response.json().get("rows", [])]
        for row in batch:
            try:
                rows.append({"company": row.get("company_canonical") or row.get("company") or "", "state": row.get("state"), "workers": int(row.get("employees_affected") or 0),
                             "date": date.fromisoformat(str(row["notice_date"])[:10]), "place": row.get("location") or "",
                             "source": f"https://huggingface.co/datasets/{_WARN_DATASET}"})
            except (KeyError, ValueError):
                continue
        if len(batch) < 100:
            break
        offset += 100
    return rows


def refresh_warn(db: Session, days: int = 120) -> dict:
    """Records WARN notices at tracked employers. Exact names only; a site closure of a few workers says nothing about the company."""
    tracked = _tracked(db)
    since = date.today() - timedelta(days=days)
    rows = _warn_national(since)
    national = len(rows)
    try:
        rows += _warn_california()
    except (httpx.HTTPError, StopIteration, ValueError, OSError):
        pass
    added = 0
    for row in rows:
        company = tracked.get(normalize_company_name(row["company"]))
        if company is None or row["date"] < since or row["workers"] < 20:
            continue
        added += add_evidence(db, company.name, "warn", row["date"], f"WARN notice, {row['state']}: {row['workers']} workers", row["source"], company)
    db.commit()
    return {"warn_rows_national": national, "warn_rows_total": len(rows), "warn_added": added}


_SEC_SEARCH = "https://efts.sec.gov/LATEST/search-index"
_SEC_HEADERS = {"User-Agent": "Career Pilot research"}
_SEC_PHRASES = {'"hiring freeze"': "says 'hiring freeze'", '"freeze on hiring"': "says 'freeze on hiring'", '"reduced hiring"': "says 'reduced hiring'",
                '"reduction in force"': "reports a reduction in force", '"workforce reduction"': "reports a workforce reduction",
                '"Item 2.05"': "files Item 2.05, costs of exit or restructuring"}


def _sec_name(display: str) -> str:
    return re.split(r"\s{2,}\(|\s+\(CIK", display)[0].strip()


def apply_filings(db: Session, hits: list[dict], phrase_note: str, tracked: dict) -> int:
    added = 0
    for hit in hits:
        source = hit["_source"]
        company = tracked.get(normalize_company_name(_sec_name(source["display_names"][0])))
        if company is None:
            continue
        accession, _, filename = hit["_id"].partition(":")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(source['ciks'][0])}/{accession.replace('-', '')}/{filename}"
        added += add_evidence(db, company.name, "filing", date.fromisoformat(source["file_date"]), f"{source['form']} {phrase_note}", url, company)
    return added


def refresh_sec_filings(db: Session, days: int = 120) -> dict:
    """Searches recent 10-K, 10-Q and 8-K text for restructuring and hiring language, for tracked employers only. A phrase can be a
    risk statement rather than an event, so the source link is saved beside it."""
    tracked = _tracked(db)
    end = date.today()
    added = 0
    for phrase, note in _SEC_PHRASES.items():
        for start in (0, 100):
            response = httpx.get(_SEC_SEARCH, params={"q": phrase, "forms": "10-Q,10-K,8-K", "dateRange": "custom", "startdt": (end - timedelta(days=days)).isoformat(),
                                                      "enddt": end.isoformat(), "from": start}, headers=_SEC_HEADERS, timeout=30)
            if response.status_code != 200:
                break
            hits = response.json()["hits"]["hits"]
            added += apply_filings(db, hits, note, tracked)
            if len(hits) < 100:
                break
            time.sleep(0.2)
    db.commit()
    return {"filings_added": added}


# RePORTER's organisation name for each cap-exempt employer worth watching. Federal money pays for many research data roles.
NIH_ORGS = {"Johns Hopkins University": "JOHNS HOPKINS UNIVERSITY", "University of Michigan": "UNIVERSITY OF MICHIGAN AT ANN ARBOR", "Duke University": "DUKE UNIVERSITY",
            "Emory University": "EMORY UNIVERSITY", "Washington University in St. Louis": "WASHINGTON UNIVERSITY", "Mayo Clinic": "MAYO CLINIC ROCHESTER",
            "University of Arizona": "UNIVERSITY OF ARIZONA", "Arizona State University": "ARIZONA STATE UNIVERSITY-TEMPE CAMPUS"}


def refresh_nih(db: Session) -> dict:
    """NIH awards in the latest fiscal year against two years earlier. Context beside the signal; it never changes the state."""
    year = date.today().year if date.today().month >= 10 else date.today().year - 1
    tracked, results = _tracked(db), {}
    for name, org in NIH_ORGS.items():
        counts = []
        for fiscal_year in (year - 2, year):
            body = {"criteria": {"org_names_exact_match": [org], "fiscal_years": [fiscal_year]}, "include_fields": ["ProjectNum"], "limit": 1, "offset": 0}
            response = httpx.post("https://api.reporter.nih.gov/v2/projects/search", json=body, timeout=30)
            counts.append(response.json().get("meta", {}).get("total") if response.status_code == 200 else None)
            time.sleep(1)
        if not all(counts):
            continue
        change = (counts[1] - counts[0]) / counts[0] * 100
        results[name] = round(change, 1)
        add_evidence(db, name, "funding", date(year, 9, 30), f"NIH awards {counts[0]} in FY{year - 2}, {counts[1]} in FY{year} ({change:+.0f}%)",
                     "https://reporter.nih.gov/", tracked.get(normalize_company_name(name)))
    db.commit()
    return {"nih_change_pct": results}


def refresh_own_record(db: Session, today: date | None = None) -> dict:
    """His own record: roles cancelled after he applied in 120 days, and applications with no answer after 30."""
    today = today or date.today()
    pulled: dict[int, int] = {}
    for row in db.query(HiringEvidence).filter(HiringEvidence.kind == "cancelled", HiringEvidence.event_date >= today - timedelta(days=120)):
        if row.company_id:
            pulled[row.company_id] = pulled.get(row.company_id, 0) + 1
    silent: dict[int, int] = {}
    cutoff = datetime.combine(today - timedelta(days=30), datetime.min.time())
    applied = db.query(JobApplication).join(JobPosting).filter(JobApplication.status == "Applied", JobApplication.applied_at < cutoff,
                                                               JobApplication.replied_at.is_(None), JobApplication.interviewing_at.is_(None))
    for application in applied:
        if application.posting.company_id:
            silent[application.posting.company_id] = silent.get(application.posting.company_id, 0) + 1
    for company in db.query(Company).all():
        company.roles_pulled_120d = pulled.get(company.id)
        company.own_silent_applied = silent.get(company.id)
    db.commit()
    return {"employers_with_cancelled_roles": len(pulled), "employers_silent": len(silent)}


def discover_workday_boards(db: Session) -> dict:
    """Finds each employer's Workday tenant and site from its own stored posting links, since a guessed name returns 404 or 422."""
    found = 0
    rows = db.query(JobPosting.company_id, JobPosting.job_url).filter(JobPosting.job_url.like("%myworkdayjobs.com%"), JobPosting.company_id.isnot(None))
    for company_id, url in rows:
        match = re.match(r"https://([^.]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?]+)", url or "")
        company = db.get(Company, company_id)
        if match and company and not company.workday_board:
            company.workday_board = "|".join(match.groups())
            found += 1
    db.commit()
    return {"workday_boards_found": found}


def parse_insights(text: str) -> dict | None:
    """Reads the 'Job openings growth' block of a LinkedIn company Insights page. Arrows give the sign: the numbers carry none."""
    openings = re.search(r"Functions\s*([\d,]+)\s*Q\d \d{4}", text)
    start, end = text.find("Job openings growth"), text.find("Total employee count")
    if not openings or start < 0 or end < start:
        return None
    rows = {}
    block = re.sub(r"\s+", " ", text[start:end]).replace("Job openings growth", "").replace("3m 6m", "")
    for label, a1, v1, a2, v2 in re.findall(r"([A-Z][A-Za-z ,&]+?)\s*([▲▼])\s*(\d+)%\s*([▲▼])\s*(\d+)%", block):
        rows[label.strip()] = (int(v1) * (1 if a1 == "▲" else -1), int(v2) * (1 if a2 == "▲" else -1))
    if "Total" not in rows:
        return None
    tech = rows.get("Engineering") or rows.get("Information Technology")
    return {"openings": int(openings.group(1).replace(",", "")), "total_3m": rows["Total"][0], "total_6m": rows["Total"][1],
            "tech_3m": tech[0] if tech else None, "tech_6m": tech[1] if tech else None}


def save_insights(db: Session, figures: dict) -> dict:
    """figures: {employer name: parse_insights result}. The page needs his signed-in browser, so a run reads it and hands it here."""
    tracked, saved, missing = _tracked(db), 0, []
    for name, row in figures.items():
        company = tracked.get(normalize_company_name(name))
        if company is None:
            missing.append(name)
            continue
        company.li_openings, company.li_openings_3m_pct, company.li_openings_6m_pct = row["openings"], row["total_3m"], row["total_6m"]
        company.li_tech_openings_3m_pct, company.li_tech_openings_6m_pct = row.get("tech_3m"), row.get("tech_6m")
        company.li_checked_at = utcnow()
        saved += 1
    db.commit()
    return {"insights_saved": saved, "not_tracked": missing}


def market_pulse() -> dict:
    """Indeed's US posting index (Feb 2020 = 100) for the sectors his roles sit in: the market behind any one employer."""
    url = "https://raw.githubusercontent.com/hiring-lab/job_postings_tracker/master/US/job_postings_by_sector_US.csv"
    rows = list(csv.DictReader(io.StringIO(httpx.get(url, timeout=60, follow_redirects=True).text)))
    result = {}
    for sector in ("Data & Analytics", "Software Development"):
        series = {r["date"]: float(r["indeed_job_postings_index"]) for r in rows if r["display_name"] == sector and r["variable"] == "total postings"}
        latest = max(series)
        year_ago = max(d for d in series if d <= f"{int(latest[:4]) - 1}{latest[4:]}")
        result[sector] = {"date": latest, "index": series[latest], "year_ago": series[year_ago], "change_pct": round((series[latest] / series[year_ago] - 1) * 100, 1)}
    return result


def refresh_all(db: Session, max_age_days: int = 1) -> dict:
    """Every free source in one pass. A source that is down leaves the last good numbers and is reported, never fatal."""
    result: dict = {}
    steps = (discover_workday_boards, lambda d: refresh_board_activity(d, max_age_days), refresh_linkedin_counts, refresh_warn, refresh_sec_filings, refresh_nih, refresh_own_record)
    for step in steps:
        try:
            result.update(step(db))
        except (httpx.HTTPError, KeyError, ValueError) as error:
            db.rollback()
            result.setdefault("failed", []).append(f"{getattr(step, '__name__', 'boards')}: {error}")
    result["alerts"] = apply_hiring_alerts(db)["alerts_on_file"]
    return result
