"""
DOL OFLC OEWS wage-level reference data. Loads the SOC-code x area
wage table (see models.OewsWage) and exposes wage_level_for() to
classify an offered annual salary against it. Levels I-IV follow
OFLC's own prevailing-wage convention (roughly the 17th/34th/50th/
67th wage percentiles for that occupation+area) -- this module only
stores and looks up the numbers DOL published, it doesn't compute
percentiles itself. File is manually dropped in by the user (see
SETUP.md), same as sponsors.py's two loaders.
"""

import math

import pandas as pd
from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import OewsWage
from ..services.activity_logger import log_activity
from .column_utils import resolve_column_learned

_SOC_CODE_ALIASES = ["OCC_CODE", "SOC_CODE", "SOC Code", "Occupation Code"]
_SOC_TITLE_ALIASES = ["OCC_TITLE", "SOC_TITLE", "Occupation Title"]
_AREA_TITLE_ALIASES = ["AREA_TITLE", "Area Title", "Area Name"]
_LEVEL_1_ALIASES = ["LEVEL1", "Level I", "WAGE_LEVEL_1", "Level 1"]
_LEVEL_2_ALIASES = ["LEVEL2", "Level II", "WAGE_LEVEL_2", "Level 2"]
_LEVEL_3_ALIASES = ["LEVEL3", "Level III", "WAGE_LEVEL_3", "Level 3"]
_LEVEL_4_ALIASES = ["LEVEL4", "Level IV", "WAGE_LEVEL_4", "Level 4"]


def _clean_str(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def _to_float(value) -> float | None:
    text = _clean_str(value).replace(",", "").replace("$", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def load_oews_wage_data(db: Session, file_path: str, source_year: int) -> dict:
    if file_path.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(file_path, dtype=str)
    else:
        df = pd.read_csv(file_path, dtype=str)

    _S = "oews_wages"
    soc_col = resolve_column_learned(db, df.columns, _SOC_CODE_ALIASES, _S, "soc_code")
    soc_title_col = resolve_column_learned(db, df.columns, _SOC_TITLE_ALIASES, _S, "soc_title", required=False)
    area_col = resolve_column_learned(db, df.columns, _AREA_TITLE_ALIASES, _S, "area_title")
    level1_col = resolve_column_learned(db, df.columns, _LEVEL_1_ALIASES, _S, "level_1")
    level2_col = resolve_column_learned(db, df.columns, _LEVEL_2_ALIASES, _S, "level_2")
    level3_col = resolve_column_learned(db, df.columns, _LEVEL_3_ALIASES, _S, "level_3")
    level4_col = resolve_column_learned(db, df.columns, _LEVEL_4_ALIASES, _S, "level_4")

    now = utcnow()
    upserted = 0
    for _, r in df.iterrows():
        soc_code = _clean_str(r[soc_col])
        area_title = _clean_str(r[area_col])
        if not soc_code or not area_title:
            continue

        existing = (
            db.query(OewsWage)
            .filter(
                OewsWage.soc_code == soc_code,
                OewsWage.area_title == area_title,
                OewsWage.source_year == source_year,
            )
            .first()
        )
        row = existing or OewsWage(soc_code=soc_code, area_title=area_title, source_year=source_year)
        row.soc_title = _clean_str(r[soc_title_col]) if soc_title_col else row.soc_title
        row.wage_level_1 = _to_float(r[level1_col])
        row.wage_level_2 = _to_float(r[level2_col])
        row.wage_level_3 = _to_float(r[level3_col])
        row.wage_level_4 = _to_float(r[level4_col])
        row.updated_at = now
        if not existing:
            db.add(row)
        upserted += 1

    db.commit()
    log_activity(db, f"OEWS wage ingest ({source_year}): upserted {upserted} SOC/area wage rows.", "INFO")
    return {"rows_upserted": upserted}


def wage_level_for(db: Session, soc_code: str, area_title: str, offered_annual_wage: float) -> str | None:
    """Classifies an offered salary against DOL's own wage levels for
    this SOC code + area, using the most recent source_year loaded for
    that pair if more than one is present. Returns 'I'/'II'/'III'/'IV',
    'Below Level I' (offered wage undercuts even the lowest DOL level
    -- a real prevailing-wage red flag), or None if no wage data is
    loaded for this SOC+area combination at all."""
    row = (
        db.query(OewsWage)
        .filter(OewsWage.soc_code == soc_code, OewsWage.area_title == area_title)
        .order_by(OewsWage.source_year.desc())
        .first()
    )
    if row is None or row.wage_level_1 is None:
        return None
    if offered_annual_wage < row.wage_level_1:
        return "Below Level I"
    if row.wage_level_2 is not None and offered_annual_wage < row.wage_level_2:
        return "I"
    if row.wage_level_3 is not None and offered_annual_wage < row.wage_level_3:
        return "II"
    if row.wage_level_4 is not None and offered_annual_wage < row.wage_level_4:
        return "III"
    return "IV"
