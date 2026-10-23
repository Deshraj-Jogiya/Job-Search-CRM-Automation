"""
USCIS H-1B Employer Data Hub + DOL OFLC LCA Disclosure loaders.

Both files are manually dropped in by the user (see SETUP.md) --
there is no live download here. Both loaders only UPDATE Company rows
that already exist (matched via the same normalize_company_name()
exact-match every other intake path in this app uses -- see
intake_service.py's _get_or_create_company), never create new ones:
USCIS's Employer Data Hub alone covers 100k+ employers historically,
almost none of them relevant to this job search, so this is
deliberately a signal layered onto companies already being tracked,
not a new company-discovery source (that's a separate, existing
feature -- see ats_dataset_discovery.py and friends).

Idempotent by design: both loaders recompute each matched company's
totals from scratch on every run rather than incrementing, so
re-running against the same file (or a newer one covering the same
employers) never double-counts.
"""

import math

import json
import os

import pandas as pd
from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import Company
from ..services.activity_logger import log_activity
from ..services.company_utils import normalize_company_name
from .column_utils import resolve_column_learned

_USCIS_EMPLOYER_ALIASES = ["Employer", "Petitioner Name", "Employer (Petitioner) Name"]
_USCIS_FY_ALIASES = ["Fiscal Year", "FY"]
_USCIS_INITIAL_APPROVAL_ALIASES = ["Initial Approval", "Initial Approvals"]
_USCIS_INITIAL_DENIAL_ALIASES = ["Initial Denial", "Initial Denials"]
_USCIS_CONTINUING_APPROVAL_ALIASES = ["Continuing Approval", "Continuing Approvals"]
_USCIS_CONTINUING_DENIAL_ALIASES = ["Continuing Denial", "Continuing Denials"]

_LCA_EMPLOYER_ALIASES = ["EMPLOYER_NAME", "Employer Name"]
_LCA_STATUS_ALIASES = ["CASE_STATUS", "Case Status"]
_LCA_VISA_CLASS_ALIASES = ["VISA_CLASS", "Visa Class"]
_LCA_SOC_CODE_ALIASES = ["SOC_CODE", "SOC Code"]
_LCA_WAGE_LEVEL_ALIASES = ["PW_WAGE_LEVEL", "PW Wage Level", "Wage Level"]

# Computer/Mathematical Occupations major group -- see company_tier.py's
# wage_level_fit score component, which reads Company.max_wage_level_15xx.
_WAGE_LEVEL_SOC_PREFIX = "15-"
_WAGE_LEVEL_RANK = {"I": 1, "II": 2, "III": 3, "IV": 4}


_ALIASES_PATH = os.path.join(os.path.dirname(__file__), "sponsor_name_aliases.json")


def _load_aliases() -> dict[str, list[str]]:
    """Hand-checked links from a tracked company's normalized name to the employer names the government files use
    (a brand vs its legal entity, e.g. QuantumBlack files as McKinsey). Keys and values are normalized names."""
    try:
        with open(_ALIASES_PATH, encoding="utf-8") as f:
            return {normalize_company_name(k): [normalize_company_name(v) for v in vs] for k, vs in json.load(f).items()}
    except FileNotFoundError:
        return {}


def _file_keys_for(name: str, file_keys: dict, aliases: dict[str, list[str]]) -> list[str]:
    """The file's employer keys that belong to one tracked company, most certain rule first: the exact normalized name;
    hand-checked aliases; the same name with spaces ignored ("JPMorganChase" vs "JPMorgan Chase"); and a legal entity
    filing "doing business as" the tracked name ("Nielsen Consumer dba NielsenIQ"). The last two only count when
    exactly one employer fits, so an ambiguous name is left unmatched rather than guessed."""
    if not name:
        return []
    if name in file_keys:
        return [name]
    if name in aliases:
        found = [k for k in aliases[name] if k in file_keys]
        if found:
            return found
    compact = name.replace(" ", "")
    if len(compact) >= 6:
        same = [k for k in file_keys.get("__compact__", {}).get(compact, [])]
        if len(same) == 1:
            return same
    dba = [k for k in file_keys.get("__dba__", {}).get(name, [])]
    return dba if len(dba) == 1 else []


def _index(keys) -> dict:
    """Lookup structure for _file_keys_for: every key, plus space-free and "dba" indexes built once per file."""
    index: dict = {k: True for k in keys}
    compact: dict[str, list[str]] = {}
    dba: dict[str, list[str]] = {}
    for k in keys:
        compact.setdefault(k.replace(" ", ""), []).append(k)
        if " dba " in k:
            dba.setdefault(k.split(" dba ", 1)[1].strip(), []).append(k)
    index["__compact__"], index["__dba__"] = compact, dba
    return index


def _to_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _sponsorship_tier(approvals: int) -> str:
    if approvals <= 0:
        return "None"
    if approvals < 5:
        return "Rare"
    if approvals < 50:
        return "Occasional"
    return "Frequent"


def _aggregate_uscis_rows(df: pd.DataFrame, columns: dict) -> dict[str, dict]:
    aggregated: dict[str, dict] = {}
    for _, r in df.iterrows():
        key = normalize_company_name(str(r[columns["employer"]] or ""))
        if not key:
            continue
        bucket = aggregated.setdefault(key, {"approvals": 0, "denials": 0, "last_fiscal_year": None})
        bucket["approvals"] += (_to_int(r[columns["initial_approval"]]) or 0) + (
            _to_int(r[columns["continuing_approval"]]) or 0
        )
        bucket["denials"] += (_to_int(r[columns["initial_denial"]]) or 0) + (
            _to_int(r[columns["continuing_denial"]]) or 0
        )
        fy = _to_int(r[columns["fiscal_year"]])
        if fy is not None and (bucket["last_fiscal_year"] is None or fy > bucket["last_fiscal_year"]):
            bucket["last_fiscal_year"] = fy
    return aggregated


def load_uscis_h1b_data(db: Session, csv_path: str) -> dict:
    """Aggregates approvals/denials per employer across every fiscal
    year present in the file, then updates every tracked Company whose
    normalized name matches an employer in the file. Returns counts
    for the caller to report/log."""
    df = pd.read_csv(csv_path, dtype=str)
    _S = "uscis_h1b"
    columns = {
        "employer": resolve_column_learned(db, df.columns, _USCIS_EMPLOYER_ALIASES, _S, "employer"),
        "fiscal_year": resolve_column_learned(db, df.columns, _USCIS_FY_ALIASES, _S, "fiscal_year"),
        "initial_approval": resolve_column_learned(db, df.columns, _USCIS_INITIAL_APPROVAL_ALIASES, _S, "initial_approval"),
        "initial_denial": resolve_column_learned(db, df.columns, _USCIS_INITIAL_DENIAL_ALIASES, _S, "initial_denial"),
        "continuing_approval": resolve_column_learned(db, df.columns, _USCIS_CONTINUING_APPROVAL_ALIASES, _S, "continuing_approval"),
        "continuing_denial": resolve_column_learned(db, df.columns, _USCIS_CONTINUING_DENIAL_ALIASES, _S, "continuing_denial"),
    }
    aggregated = _aggregate_uscis_rows(df, columns)

    companies = db.query(Company).all()
    now = utcnow()
    matched = 0
    index, aliases = _index(aggregated), _load_aliases()
    for company in companies:
        keys = _file_keys_for(company.normalized_name, index, aliases)
        if not keys:
            continue
        parts = [aggregated[k] for k in keys]
        years = [b["last_fiscal_year"] for b in parts if b["last_fiscal_year"] is not None]
        bucket = {"approvals": sum(b["approvals"] for b in parts), "denials": sum(b["denials"] for b in parts),
                  "last_fiscal_year": max(years) if years else None}
        company.h1b_approvals_total = bucket["approvals"]
        company.h1b_denials_total = bucket["denials"]
        company.h1b_last_fiscal_year = bucket["last_fiscal_year"]
        company.sponsorship_tier = _sponsorship_tier(bucket["approvals"])
        company.h1b_data_updated_at = now
        matched += 1
    db.commit()

    result = {
        "employers_in_file": len(aggregated),
        "companies_matched": matched,
        "companies_total": len(companies),
    }
    log_activity(
        db,
        f"USCIS H-1B ingest: matched {matched}/{len(companies)} tracked companies "
        f"against {len(aggregated)} employers in the file.",
        "INFO",
    )
    return result


def _normalize_wage_level(raw) -> str | None:
    """DOL's own PW_WAGE_LEVEL column has been spelled inconsistently
    across quarterly releases -- bare roman numerals, "Level I", or a
    plain digit have all been seen in the wild. Normalizes any of
    those to the canonical "I"/"II"/"III"/"IV", or None if unrecognized
    (never guessed)."""
    text = str(raw or "").strip().upper()
    text = text.replace("LEVEL", "").strip()
    digit_map = {"1": "I", "2": "II", "3": "III", "4": "IV"}
    if text in digit_map:
        return digit_map[text]
    if text in _WAGE_LEVEL_RANK:
        return text
    return None


def load_dol_lca_data(db: Session, xlsx_path: str, fiscal_quarter: str) -> dict:
    """One DOL LCA Disclosure file covers one fiscal quarter, so
    fiscal_quarter (e.g. "FY2024Q1") is supplied by the caller rather
    than parsed from the filename -- naming conventions for these
    files aren't reliable enough to parse. Only counts CERTIFIED H-1B
    LCAs (the disclosure file also includes withdrawn/denied filings
    and other visa classes like H-1B1/E-3).

    Also tracks each employer's highest prevailing-wage level among
    Computer/Mathematical (SOC 15-*) filings -- Company.max_wage_level_15xx,
    the wage_level_fit score component's input (see company_tier.py and
    WEIGHTING.md). SOC_CODE/PW_WAGE_LEVEL are optional columns (older
    releases or a stripped-down export might not carry them) -- when
    missing, filing counts are still updated normally, just without a
    wage-level update, logged separately below rather than failing the
    whole load."""
    df = pd.read_excel(xlsx_path, dtype=str)
    _S = "dol_lca"
    employer_col = resolve_column_learned(db, df.columns, _LCA_EMPLOYER_ALIASES, _S, "employer")
    status_col = resolve_column_learned(db, df.columns, _LCA_STATUS_ALIASES, _S, "status", required=False)
    visa_col = resolve_column_learned(db, df.columns, _LCA_VISA_CLASS_ALIASES, _S, "visa_class", required=False)
    soc_col = resolve_column_learned(db, df.columns, _LCA_SOC_CODE_ALIASES, _S, "soc_code", required=False)
    wage_level_col = resolve_column_learned(db, df.columns, _LCA_WAGE_LEVEL_ALIASES, _S, "wage_level", required=False)

    if status_col:
        df = df[df[status_col].astype(str).str.strip().str.upper() == "CERTIFIED"]
    if visa_col:
        df = df[df[visa_col].astype(str).str.strip().str.upper().str.contains("H-1B", na=False)]

    counts: dict[str, int] = {}
    max_wage_level: dict[str, str] = {}
    for _, r in df.iterrows():
        key = normalize_company_name(str(r[employer_col] or ""))
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1

        if not (soc_col and wage_level_col):
            continue
        soc_code = str(r[soc_col] or "").strip()
        if not soc_code.startswith(_WAGE_LEVEL_SOC_PREFIX):
            continue
        level = _normalize_wage_level(r[wage_level_col])
        if level is None:
            continue
        current = max_wage_level.get(key)
        if current is None or _WAGE_LEVEL_RANK[level] > _WAGE_LEVEL_RANK[current]:
            max_wage_level[key] = level

    companies = db.query(Company).all()
    now = utcnow()
    matched = 0
    wage_level_matched = 0
    index, aliases = _index(counts), _load_aliases()
    for company in companies:
        keys = _file_keys_for(company.normalized_name, index, aliases)
        if not keys:
            continue
        filings = sum(counts[k] for k in keys)
        company.lca_filings_total = filings
        company.lca_last_fiscal_quarter = fiscal_quarter
        company.lca_data_updated_at = now
        matched += 1

        levels = [max_wage_level[k] for k in keys if k in max_wage_level]
        level = max(levels, key=lambda lv: _WAGE_LEVEL_RANK[lv]) if levels else None
        if level is not None:
            company.max_wage_level_15xx = level
            wage_level_matched += 1
    db.commit()

    result = {
        "employers_in_file": len(counts),
        "companies_matched": matched,
        "companies_total": len(companies),
        "wage_level_matched": wage_level_matched,
    }
    log_activity(
        db,
        f"DOL LCA ingest ({fiscal_quarter}): matched {matched}/{len(companies)} tracked companies "
        f"against {len(counts)} employers with certified H-1B LCAs in the file "
        f"({wage_level_matched} with a SOC 15-* wage level update).",
        "INFO",
    )
    return result
