"""
Cap-exempt employer flagging. Universities and affiliated/nonprofit or
governmental research organizations are exempt from the annual H-1B
lottery (8 CFR 214.2(h)(8)(ii)(F)) -- a real positive signal distinct
from "no USCIS approval history yet" for a company that may not need
to win the cap lottery to sponsor. No government API or dataset
labels this directly, so this is a best-effort, name-based heuristic:
a small set of narrow regex patterns for "obviously a university"
names, plus a curated list of well-known national labs and nonprofit
research institutions (cap_exempt_seeds.json). False negatives are
expected and fine -- most cap-exempt employers outside this list
simply won't get flagged. A false positive is the real risk to avoid,
so patterns are kept narrow (word-boundary "university"/"college",
not a broad substring) rather than broad.
"""

import json
import re
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Company
from ..services.activity_logger import log_activity

_SEEDS_PATH = Path(__file__).parent / "cap_exempt_seeds.json"


def _load_seeds() -> dict:
    with open(_SEEDS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _classify(raw_name: str, seeds: dict) -> tuple[bool, str | None]:
    if not raw_name:
        return False, None
    lowered = raw_name.lower()

    for entry in seeds.get("curated_employers", []):
        if entry["name"].lower() == lowered:
            return True, f"Known {entry['category'].replace('_', ' ')}: {entry['name']}"

    for pattern in seeds.get("name_patterns", []):
        if re.search(pattern, lowered):
            return True, f"Name matched cap-exempt pattern: '{pattern}'"

    return False, None


def classify_cap_exempt(raw_name: str) -> tuple[bool, str | None]:
    """Pure function, no DB access -- (is_cap_exempt, reason) for one
    company name. Reloads the seed file each call; fine for one-off
    use, apply_cap_exempt_flags() below loads it once for a full sweep."""
    return _classify(raw_name, _load_seeds())


def apply_cap_exempt_flags(db: Session) -> dict:
    """Sweeps every tracked Company and flags cap-exempt matches.
    Idempotent -- always recomputes from the current seed list instead
    of only ever adding flags, so editing/removing a bad seed later
    correctly un-flags a company on the next run too."""
    seeds = _load_seeds()
    companies = db.query(Company).all()
    flagged = 0
    for company in companies:
        is_exempt, reason = _classify(company.name, seeds)
        company.is_cap_exempt = is_exempt
        company.cap_exempt_reason = reason
        if is_exempt:
            flagged += 1
    db.commit()
    log_activity(db, f"Cap-exempt sweep: {flagged}/{len(companies)} tracked companies flagged.", "INFO")
    return {"companies_total": len(companies), "companies_flagged": flagged}
