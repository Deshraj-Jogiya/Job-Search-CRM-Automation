"""Is this employer really hiring for data roles? Effort should go where it can pay off, so the evidence is ranked by what it measures:

1. What the employer is doing now: data roles on its own job board (Greenhouse, Lever, Ashby, Workday), LinkedIn cards for employers
   with no readable board, and LinkedIn's openings trend by function.
2. What happened to his own applications: roles cancelled after he applied, and applications left unanswered.
3. Official notices: state WARN filings and SEC restructuring disclosures.
4. Reports: hand-checked news, and public trackers such as ShouldApply's (ingest/hiring_alerts.json). Trackers go stale: ShouldApply
   listed Canva and HubSpot as active in March and both cut staff by November, so a tracker alone never drops an employer.

Live openings beat reports. Pinterest and Coinbase cut staff in 2026 and kept publishing data roles; Microsoft's reported freeze
covers two units while its engineering openings rose. The one exception is a contact inside the company, who can know that a team's
roles are on hold even while the company's total openings look normal. An empty board proves nothing, because the employer may have
moved systems. Free: no LLM, public pages only.
"""

import json
from datetime import date
from functools import lru_cache
from pathlib import Path

from .company_utils import normalize_company_name

ALERTS_PATH = Path(__file__).resolve().parent.parent / "ingest" / "hiring_alerts.json"
DEEP_CUT_PERCENT = 20
ROLES_PULLED_LIMIT = 2  # two cancelled roles in 120 days is a pattern; one can be a single team's change of plan

# state: (label, rank). A lower rank gets the effort first; "frozen" is dropped from job lists and networking.
STATES = {
    "hiring": ("Hiring data roles", 0),
    "cuts_hiring": ("Cuts or freeze reported, still hiring", 1),
    "slow": ("Slow: no new data role in 30 days", 2),
    "unknown": ("No signal", 2),
    "part_cuts": ("Cuts in part of the company", 2),
    "cuts": ("Cut staff", 3),
    "part_frozen": ("Part of the company frozen", 3),
    "tracker_frozen": ("Listed as frozen by a tracker, not confirmed", 3),
    "postings_falling": ("Openings falling fast", 3),
    "roles_pulled": ("Cancelling roles after applications", 4),
    "quiet": ("Quiet: no new data role in 90 days", 4),
    "deep_cuts": ("Deep cuts", 5),
    "frozen": ("Hiring frozen", 9),
}


@lru_cache(maxsize=1)
def _alerts_by_name() -> dict:
    with open(ALERTS_PATH, encoding="utf-8") as f:
        alerts = json.load(f)["alerts"]
    return {normalize_company_name(name): alert for alert in alerts for name in alert["names"]}


def alert_for_name(name: str, today: date | None = None) -> dict | None:
    """The live alert for a company not yet tracked in the Company table, matched on the exact normalized name."""
    alert = _alerts_by_name().get(normalize_company_name(name or ""))
    if alert and alert.get("until") and date.fromisoformat(alert["until"]) < (today or date.today()):
        return None
    return alert


def insights_trend(total_3m=None, tech_3m=None, tech_6m=None) -> str | None:
    """LinkedIn openings trend. Engineering or IT counts for more than the total: PayPal's total rose 120% while engineering
    openings fell 65% in six months."""
    if total_3m is None:
        return None
    if total_3m <= -20 or (tech_3m is not None and tech_3m <= -20) or (tech_6m is not None and tech_6m <= -40 and (tech_3m or 0) < 0):
        return "falling"
    if total_3m >= 0 and (tech_3m is None or tech_3m >= -10):
        return "rising"
    return "flat"


def assess(kind=None, scope=None, percent=None, board_open=None, data_30=None, data_90=None, linkedin_30=None, trend=None,
           roles_pulled=0, falling_board=False, evidence=None) -> dict:
    """Pure decision from one employer's alert fields and measured activity."""
    board_read = bool(board_open)  # 0 or None: not read, or an empty board that may simply have moved
    new_data = (board_read and (data_30 or 0) > 0) or (linkedin_30 or 0) > 0
    live = new_data or trend == "rising"
    if kind == "freeze" and scope == "company":
        state = "cuts_hiring" if live else "frozen"
    elif kind == "freeze":
        state = "cuts_hiring" if live and evidence != "insider" else "part_frozen"
    elif kind == "tracker":
        state = "cuts_hiring" if live else "tracker_frozen"
    elif kind in ("layoff", "warn"):
        if live:
            state = "cuts_hiring"
        else:
            state = "deep_cuts" if (percent or 0) >= DEEP_CUT_PERCENT else ("cuts" if scope == "company" else "part_cuts")
    elif new_data or trend == "rising":
        state = "hiring"
    elif board_read and (data_90 or 0) > 0:
        state = "slow"
    elif board_read:
        state = "quiet"
    else:
        state = "unknown"
    if state != "frozen":
        if roles_pulled >= ROLES_PULLED_LIMIT and not live:
            state = "roles_pulled"
        elif (trend == "falling" or falling_board) and not new_data and STATES[state][1] < STATES["postings_falling"][1]:
            state = "postings_falling"
    label, rank = STATES[state]
    return {"state": state, "label": label, "rank": rank}


def _pct(value) -> str:
    return f"{value:+.0f}%"


def evidence_text(company) -> str | None:
    """One line of what was measured, so a label can be checked instead of trusted."""
    parts = []
    if company.board_open_total:
        parts.append(f"{company.board_data_open or 0} US data roles open, {company.board_data_new_30d or 0} new in 30 days, "
                     f"{company.board_data_new_90d or 0} in 90")
    if company.linkedin_data_new_30d is not None:
        parts.append(f"{company.linkedin_data_new_30d} data roles on LinkedIn in 30 days")
    if company.li_openings is not None:
        text = f"LinkedIn openings {company.li_openings:,} ({_pct(company.li_openings_3m_pct)} in 3 months"
        if company.li_tech_openings_3m_pct is not None:
            text += f", engineering or IT {_pct(company.li_tech_openings_3m_pct)}"
        parts.append(text + ")")
    if company.board_falling:
        parts.append("board shrinking or publishing far fewer jobs than usual")
    if company.roles_pulled_120d:
        parts.append(f"{company.roles_pulled_120d} of his roles cancelled in 120 days")
    if company.own_silent_applied:
        parts.append(f"{company.own_silent_applied} applications unanswered after 30 days")
    return "; ".join(parts) or None


def for_company(company, today: date | None = None) -> dict:
    """The signal for a Company row, plus the alert text and measured evidence to show beside it."""
    live = company.hiring_alert_kind and not (company.hiring_alert_until and company.hiring_alert_until < (today or date.today()))
    result = assess(company.hiring_alert_kind if live else None, company.hiring_alert_scope if live else None,
                    company.hiring_alert_percent if live else None, company.board_open_total,
                    company.board_data_new_30d, company.board_data_new_90d, company.linkedin_data_new_30d,
                    insights_trend(company.li_openings_3m_pct, company.li_tech_openings_3m_pct, company.li_tech_openings_6m_pct),
                    company.roles_pulled_120d or 0, bool(company.board_falling), company.hiring_alert_evidence if live else None)
    result["note"] = company.hiring_alert if live else None
    result["source"] = company.hiring_alert_source if live else None
    result["board"] = evidence_text(company)
    return result


def for_name(name: str, company=None, today: date | None = None) -> dict:
    """For job and networking runs: a tracked company's signal, or the alert list alone for one Career Pilot has not seen."""
    if company is not None:
        return for_company(company, today)
    alert = alert_for_name(name, today) or {}
    result = assess(alert.get("kind"), alert.get("scope"), alert.get("percent"), evidence=alert.get("evidence"))
    result["note"], result["source"], result["board"] = alert.get("note"), alert.get("source"), None
    return result
