"""
Look any employer up in the full government sponsorship records, and list the employers that really sponsor data roles.

The Company table only knows companies Career Pilot has already seen, so a networking run that checked sponsorship there found most
new companies "not in Career Pilot" and could say nothing about them. This module reads the raw files instead:

- the USCIS H-1B Employer Data Hub export (data/raw/uscis/h1b_hub_fy2024_2026.csv): initial and continuing approvals per employer
  and fiscal year, every employer in the country;
- a slim extract of the DOL LCA disclosure file (data/raw/dol/lca_data_roles.csv, built once by build_slim_lca from the quarterly
  XLSX): certified H-1B labor condition applications for data, analytics, ML and database work, with the wage level, the worksite
  and DOL's own H-1B-dependent flag.

Two uses: lookup() answers "does this employer sponsor, and for roles like mine?" for any name, and data_sponsors() ranks the
employers that filed the most data-role LCAs (optionally by worksite state), which is where networking should look first.
Cap-exempt employers (universities and their affiliated or nonprofit research organisations) hire on H-1B all year with no lottery;
they are flagged with the same name rules and curated list as cap_exempt.py, plus the LCA's NAICS code for colleges and
universities (6113). H-1B-dependent employers are mostly staffing and IT-consulting firms, which place people at clients rather than
hire for their own teams, so data_sponsors() leaves them out unless asked.
"""

import csv
import json
import re
from collections import defaultdict
from pathlib import Path

from ..services.company_utils import normalize_company_name
from .cap_exempt import classify_cap_exempt

REPO = Path(__file__).resolve().parents[2]
USCIS_PATH = REPO / "data" / "raw" / "uscis" / "h1b_hub_fy2024_2026.csv"
LCA_XLSX = REPO / "data" / "raw" / "dol" / "LCA_Disclosure_Data_FY2026_Q3.xlsx"
LCA_SLIM = REPO / "data" / "raw" / "dol" / "lca_data_roles.csv"
ALERTS_PATH = Path(__file__).parent / "sponsorship_alerts.json"

# SOC codes for data work: data scientists, database architects and administrators, statisticians, operations research analysts,
# software developers, computer systems analysts, computer and information research scientists.
DATA_SOC = ("15-2051", "15-1243", "15-1242", "15-2041", "15-2031", "15-1252", "15-1211", "15-1221")
# Software developers and systems analysts are broad codes; for them the job title must name data work.
BROAD_SOC = ("15-1252", "15-1211")
DATA_TITLE = re.compile(r"\bdata\b|machine learning|\bml\b|\bmlops\b|analytic|\betl\b|\bbi\b|business intelligence|"
                        r"\bai\b|artificial intelligence|warehouse|database|\bnlp\b|deep learning", re.IGNORECASE)
SLIM_COLUMNS = ["employer", "trade_name", "job_title", "soc_code", "wage_level", "worksite_city", "worksite_state", "naics",
                "h1b_dependent", "positions", "received"]


# For-profit schools are not institutions that make an employer cap-exempt (8 CFR 214.2(h)(19)(iii) needs a public or nonprofit
# institution of higher education), though their names match "university".
FOR_PROFIT_SCHOOLS = re.compile(r"university of phoenix|capella|strayer|devry|walden university|american public university|"
                                r"grand canyon education|national american university|aspen university|colorado technical", re.IGNORECASE)
CORPORATE_SUFFIX = re.compile(r"\b(inc|llc|l\.l\.c|corp|corporation|ltd)\b\.?", re.IGNORECASE)


def _cap_exempt(name: str, naics_university: bool) -> tuple[bool, str | None]:
    """Cap-exempt by the curated list and name rules, minus for-profit schools; a name pattern on a corporate name ("..., Inc.")
    is not enough on its own."""
    if FOR_PROFIT_SCHOOLS.search(name or ""):
        return False, "for-profit school: not cap-exempt"
    exempt, reason = classify_cap_exempt(name)
    if exempt and reason and reason.startswith("Name matched") and CORPORATE_SUFFIX.search(name or ""):
        return False, "name looks like a university but is a corporation: check by hand"
    if not exempt and naics_university and not CORPORATE_SUFFIX.search(name or ""):
        return True, "LCA filings list NAICS 6113 (colleges and universities)"
    return exempt, reason


def _key(name: str) -> str:
    name = re.split(r"\b(?:d/?b/?a|doing business as)\b", name or "", maxsplit=1, flags=re.IGNORECASE)[0]
    # "The University of Arizona" and "University of Arizona" are the same employer in the files.
    name = re.sub(r"^\s*the\s+", "", name, flags=re.IGNORECASE)
    return normalize_company_name(name)


# H-1B-dependent alone does not mean staffing: Meta and other large employers declare it too. Staffing and IT-consulting firms
# are the dependent employers in computer systems design and programming services (NAICS 5415) or employment services (5613).
STAFFING_NAICS = ("5415", "5613")


def staffing_share(filings: list[dict]) -> float:
    if not filings:
        return 0.0
    staffing = sum(1 for f in filings if str(f.get("h1b_dependent", "")).upper().startswith("Y")
                   and str(f.get("naics", "")).startswith(STAFFING_NAICS))
    return staffing / len(filings)


def _closest(k: str, keys, limit: int = 3) -> list[str]:
    """Employer keys that contain every word of k, shortest first: shown when there is no exact match, to be checked by eye."""
    words = k.split()
    if not words:
        return []
    found = [x for x in keys if all(w in x.split() for w in words)]
    return sorted(found, key=len)[:limit]


def is_data_role(soc_code: str, job_title: str) -> bool:
    soc = (soc_code or "")[:7]
    if soc not in DATA_SOC:
        return False
    return bool(DATA_TITLE.search(job_title or "")) if soc in BROAD_SOC else True


def build_slim_lca(xlsx_path: Path = LCA_XLSX, out_path: Path = LCA_SLIM) -> int:
    """One pass over the quarterly LCA XLSX (about 250 MB): keep certified H-1B data-role filings, a few columns each."""
    from openpyxl import load_workbook

    wb = load_workbook(xlsx_path, read_only=True)
    rows = wb.worksheets[0].iter_rows(values_only=True)
    header = [str(h) for h in next(rows)]
    col = {name: header.index(name) for name in header}
    kept = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(SLIM_COLUMNS)
        for r in rows:
            if str(r[col["VISA_CLASS"]] or "") != "H-1B" or not str(r[col["CASE_STATUS"]] or "").startswith("Certified"):
                continue
            title, soc = str(r[col["JOB_TITLE"]] or ""), str(r[col["SOC_CODE"]] or "")
            if not is_data_role(soc, title):
                continue
            received = r[col["RECEIVED_DATE"]]
            w.writerow([r[col["EMPLOYER_NAME"]], r[col["TRADE_NAME_DBA"]] or "", title, soc[:7], r[col["PW_WAGE_LEVEL"]] or "",
                        r[col["WORKSITE_CITY"]] or "", r[col["WORKSITE_STATE"]] or "", str(r[col["NAICS_CODE"]] or ""),
                        r[col["H_1B_DEPENDENT"]] or "", r[col["TOTAL_WORKER_POSITIONS"]] or 1,
                        received.date().isoformat() if hasattr(received, "date") else str(received or "")[:10]])
            kept += 1
    return kept


def _int(v) -> int:
    try:
        return int(str(v).replace(",", "").strip() or 0)
    except ValueError:
        return 0


def load_uscis(path: Path = USCIS_PATH) -> dict:
    """{employer key: {"name", "by_year": {fy: {"initial", "continuing", "denials"}}, "states"}}"""
    out: dict = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            k = _key(row.get("Employer", ""))
            if not k:
                continue
            e = out.setdefault(k, {"name": row["Employer"], "by_year": defaultdict(lambda: {"initial": 0, "continuing": 0, "denials": 0}),
                                   "states": set()})
            y = e["by_year"][row.get("Fiscal Year", "")]
            y["initial"] += _int(row.get("Initial Approval"))
            y["continuing"] += _int(row.get("Continuing Approval"))
            y["denials"] += _int(row.get("Initial Denial")) + _int(row.get("Continuing Denial"))
            if row.get("State"):
                e["states"].add(row["State"])
    return out


def load_lca(path: Path = LCA_SLIM) -> dict:
    """{employer key: [filing rows]} from the slim LCA extract."""
    out: dict = defaultdict(list)
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[_key(row["employer"])].append(row)
            if row.get("trade_name"):
                out[_key(row["trade_name"])].append(row)
    return out


def _alerts(path: Path = ALERTS_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = data.get("alerts", data) if isinstance(data, dict) else data
    return {_key(a.get("employer") or a.get("name", "")): a for a in items if isinstance(a, dict)}


def _summary(name: str, uscis: dict, lca: dict, alerts: dict) -> dict:
    k = _key(name)
    u = uscis.get(k)
    filings = lca.get(k, [])
    years = {fy: dict(v) for fy, v in sorted(u["by_year"].items())} if u else {}
    initial = sum(v["initial"] for v in years.values())
    continuing = sum(v["continuing"] for v in years.values())
    levels: dict = defaultdict(int)
    for f in filings:
        levels[str(f.get("wage_level") or "?").upper().replace("LEVEL ", "")] += 1
    naics_university = any(str(f.get("naics", "")).startswith("6113") for f in filings)
    cap_exempt, reason = _cap_exempt(filings[0]["employer"] if filings else (u["name"] if u else name), naics_university)
    dependent = sum(1 for f in filings if str(f.get("h1b_dependent", "")).upper().startswith("Y"))
    if cap_exempt and not filings and not initial:
        # A name that looks cap-exempt with nothing on file is no evidence the employer sponsors.
        cap_exempt, reason = False, f"{reason}; but no H-1B filings on record"
    alert = alerts.get(k)
    if alert:
        verdict = f"alert: {alert.get('kind') or alert.get('type') or 'see sponsorship_alerts.json'}"
    elif cap_exempt:
        verdict = "cap-exempt (no lottery)"
    elif initial >= 5 and len(filings) >= 3:
        verdict = "strong sponsor of data roles"
    elif initial + continuing > 0 or filings:
        verdict = "some sponsorship on record"
    else:
        verdict = "no sponsorship on record"
    return {
        "name": name, "matched": u["name"] if u else None, "verdict": verdict,
        # Always listed: a short name ("Lucid") can match the wrong employer, so the alternatives are shown to be checked by eye.
        "other_matches": [uscis[x]["name"] for x in _closest(k, [y for y in uscis.keys() if y != k])],
        "staffing_share": round(staffing_share(filings), 2) if filings else None,
        "uscis_initial_approvals": initial, "uscis_continuing_approvals": continuing, "uscis_by_year": years,
        "data_role_lcas": len(filings), "wage_levels": dict(levels),
        "az_data_role_lcas": sum(1 for f in filings if f.get("worksite_state") == "AZ"),
        "sample_titles": sorted({f["job_title"] for f in filings})[:6],
        "cap_exempt": cap_exempt, "cap_exempt_reason": reason,
        "h1b_dependent_share": round(dependent / len(filings), 2) if filings else None,
        "data_year": "USCIS FY2024-2026; DOL LCA FY2026 Q1-Q3",
    }


def lookup(names: list[str], uscis: dict | None = None, lca: dict | None = None) -> list[dict]:
    uscis = load_uscis() if uscis is None else uscis
    lca = load_lca() if lca is None else lca
    alerts = _alerts()
    return [_summary(n, uscis, lca, alerts) for n in names]


def data_sponsors(state: str | None = None, limit: int = 50, min_filings: int = 3, cap_exempt_only: bool = False,
                  include_dependent: bool = False, uscis: dict | None = None, lca: dict | None = None) -> list[dict]:
    """Employers ranked by certified data-role LCA filings (in `state` when given), cap-exempt employers first."""
    uscis = load_uscis() if uscis is None else uscis
    lca = load_lca() if lca is None else lca
    alerts = _alerts()
    ranked = []
    seen = set()
    for k, filings in lca.items():
        name = filings[0]["employer"]
        if _key(name) != k or k in seen:
            continue  # count each employer once, under its legal name
        seen.add(k)
        pool = [f for f in filings if not state or f.get("worksite_state") == state]
        if len(pool) < min_filings:
            continue
        if staffing_share(filings) > 0.5 and not include_dependent:
            continue
        s = _summary(name, uscis, lca, alerts)
        if s["verdict"].startswith("alert") or (cap_exempt_only and not s["cap_exempt"]):
            continue
        s["filings_in_scope"] = len(pool)
        ranked.append(s)
    ranked.sort(key=lambda s: (not s["cap_exempt"], -s["filings_in_scope"], -s["uscis_initial_approvals"]))
    return ranked[:limit]
