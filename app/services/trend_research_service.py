"""Periodic, bounded research into whether specific resume-building rules
still match current real-world practice -- and NEVER auto-applies a
change on its own.

Why this exists: in a real session (2026-09-14), config/resume_rules.yaml
had `max_pages: 1` sitting there as an unverified "resumes must be one
page" folk rule -- nobody had ever actually checked it against current
practice, and it was silently dropping real, backed resume content to
force a page count that turned out not to be required. Real research
found recruiters are now ~2.3x more likely to PREFER two-page resumes
when experience is substantial. That's exactly the class of stale,
never-re-verified assumption this module exists to keep catching before
it goes stale again -- and quietly, not loudly, the way the original one
did.

Every finding becomes a Tier 2 AdaptationLog row (status="proposed"),
reusing the exact same audit-trail table and human-approval convention
adaptation_service.py already established for statistical Tier 2
proposals -- approve/reject is an explicit human click at /adaptation,
never automatic. That's not a stylistic choice: this module's findings
come from web research an LLM summarizes, which can be wrong or find a
low-quality source, and the config it touches includes resume-building
rules a candidate's real job search depends on (see
resume_rules.yaml's work_authorization section for the highest-stakes
example of "this must never silently change itself"). The rest of this
app treats every real-world-consequential action as confirmation-gated;
this is that same rule applied to "the platform edits its own resume
logic."

Scope is a small, explicit catalog (TREND_CATEGORIES), not "monitor
everything." An open-ended, unbounded search-the-web-for-whatever-
trends-exist loop is exactly the unverified-claim risk this feature
exists to guard against, not create. Add a category only when there's a
specific, real config value worth periodically re-checking against
current practice -- the same two that motivated building this at all.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config_loader import CONFIG_DIR
from ..models import AdaptationLog
from .activity_logger import log_activity
from .adaptation_service import log_adaptation
from .contact_discovery_service import is_tavily_configured, tavily_search
from .llm import get_llm_provider, parse_json_response
from . import resume_rules

_SUBSYSTEM = "resume_market_trends"
_RESUME_RULES_PATH: Path = CONFIG_DIR / "resume_rules.yaml"

# How far a fresh recommendation must diverge from the current value
# before it's worth bothering a human with -- a "2 vs 2" or "3 vs 3"
# re-confirmation isn't a proposal, it's a no-op, and creating a
# proposal row for it would just train the user to stop reading these.
_MIN_MEANINGFUL_DELTA = 1


@dataclass
class TrendCategory:
    id: str
    label: str
    search_query: str
    current_value_fn: callable  # () -> int, reads the live config
    yaml_line_pattern: str  # regex matching the exact "key: value" line to replace, against the raw file text
    yaml_replace_fn: callable  # (new_value: int) -> str, the full replacement line


def _current_max_pages() -> int:
    return resume_rules.get_config()["page_fit"]["max_pages"]


def _current_max_projects() -> int:
    return resume_rules.get_config()["projects_by_variant"]["max_projects"]


TREND_CATEGORIES: dict[str, TrendCategory] = {
    "resume_max_pages": TrendCategory(
        id="resume_max_pages",
        label="Resume length (max pages)",
        search_query="resume length one page vs two pages {year} recruiter preference tech industry ATS parsing",
        current_value_fn=_current_max_pages,
        yaml_line_pattern=r"^\s*max_pages:\s*\d+\s*$",
        yaml_replace_fn=lambda v: f"max_pages: {v}",
    ),
    "resume_project_count": TrendCategory(
        id="resume_project_count",
        label="Resume projects section (max project count)",
        search_query="resume projects section optimal number {year} quality vs quantity recruiter attention",
        current_value_fn=_current_max_projects,
        yaml_line_pattern=r"^\s*max_projects:\s*\d+\s*$",
        yaml_replace_fn=lambda v: f"max_projects: {v}",
    ),
}


def _extract_recommendation(query: str, results: list[dict], current_value: int) -> dict | None:
    """Asks the LLM to extract ONE specific integer recommendation from
    real search results, or say it can't -- never asked to invent a
    number when the sources don't clearly support one."""
    if not results:
        return None
    sources_text = "\n\n".join(
        f"[{i+1}] {r.get('title', '')}\n{r.get('url', '')}\n{(r.get('content') or '')[:800]}"
        for i, r in enumerate(results[:5])
    )
    llm = get_llm_provider()
    raw = llm.complete_json(
        system=(
            "You are a careful research analyst. You extract a specific numeric recommendation "
            "from search results ONLY when the sources genuinely, clearly support one. You never "
            "invent a number, round speculatively, or infer past what's actually stated. You return "
            "only raw JSON, no markdown fences."
        ),
        prompt=(
            f"Question: {query}\n\n"
            f"The current configured value is {current_value}.\n\n"
            f"Search results:\n{sources_text}\n\n"
            "Based ONLY on what these sources actually say, is there a clear, specific numeric "
            "recommendation? Respond with exactly this JSON shape:\n"
            '{"has_clear_recommendation": true, "recommended_value": 2, '
            '"confidence": "high", "summary": "one or two sentences citing what the sources actually found", '
            '"source_indices_used": [1, 3]}\n'
            "Set has_clear_recommendation to false (and recommended_value to null) if the sources are "
            "vague, conflicting, or don't clearly support one specific number -- that's a normal, honest "
            "outcome, not a failure."
        ),
        temperature=0.1,
    )
    try:
        parsed = parse_json_response(raw)
    except Exception:
        return None
    if not isinstance(parsed, dict) or not parsed.get("has_clear_recommendation"):
        return None
    value = parsed.get("recommended_value")
    if not isinstance(value, int):
        return None
    return parsed


def _already_has_pending_proposal(db: Session, category_id: str) -> bool:
    existing = db.execute(
        select(AdaptationLog).where(
            AdaptationLog.subsystem == _SUBSYSTEM,
            AdaptationLog.parameter == category_id,
            AdaptationLog.status == "proposed",
        )
    ).scalars().first()
    return existing is not None


def run_trend_check(db: Session, category_id: str, current_year: int) -> AdaptationLog | None:
    """Runs ONE bounded trend check. Returns the new proposed
    AdaptationLog row if a real, meaningfully-different recommendation
    was found, else None -- both are normal outcomes, not errors."""
    category = TREND_CATEGORIES[category_id]

    if _already_has_pending_proposal(db, category_id):
        log_activity(db, f"Trend check '{category.label}' skipped -- a proposal is already pending review.", "INFO")
        return None

    if not is_tavily_configured():
        log_activity(db, f"Trend check '{category.label}' skipped -- Tavily not configured.", "INFO")
        return None

    current_value = category.current_value_fn()
    query = category.search_query.format(year=current_year)

    try:
        results = tavily_search(db, query, max_results=5)
    except Exception as exc:
        log_activity(db, f"Trend check '{category.label}' failed to search: {exc}", "WARNING")
        return None

    recommendation = _extract_recommendation(query, results, current_value)
    if not recommendation:
        log_activity(db, f"Trend check '{category.label}': no clear recommendation found, nothing proposed.", "INFO")
        return None

    new_value = recommendation["recommended_value"]
    if abs(new_value - current_value) < _MIN_MEANINGFUL_DELTA:
        log_activity(db, f"Trend check '{category.label}': current value ({current_value}) still matches research, no proposal needed.", "INFO")
        return None

    used_sources = [
        {"title": results[i - 1].get("title"), "url": results[i - 1].get("url")}
        for i in recommendation.get("source_indices_used", [])
        if 0 < i <= len(results)
    ]

    return log_adaptation(
        db,
        tier="tier2",
        subsystem=_SUBSYSTEM,
        parameter=category_id,
        old_value=current_value,
        new_value=new_value,
        triggering_evidence={
            "summary": recommendation.get("summary"),
            "confidence": recommendation.get("confidence"),
            "sources": used_sources,
            "query": query,
        },
        status="proposed",
    )


def run_all_trend_checks(db: Session, current_year: int) -> list[AdaptationLog]:
    created = []
    for category_id in TREND_CATEGORIES:
        entry = run_trend_check(db, category_id, current_year)
        if entry:
            created.append(entry)
    return created


def pending_trend_proposals(db: Session) -> list[AdaptationLog]:
    return list(
        db.execute(
            select(AdaptationLog)
            .where(AdaptationLog.subsystem == _SUBSYSTEM, AdaptationLog.status == "proposed")
            .order_by(AdaptationLog.created_at.desc())
        ).scalars()
    )


class TrendProposalError(Exception):
    pass


def _apply_yaml_change(category: TrendCategory, new_value: int) -> None:
    """Targeted single-line text replacement, not a full YAML re-dump --
    resume_rules.yaml carries extensive real reasoning in comments
    (including, as of this pass, the note explaining why max_pages
    changed once already); a generic yaml.safe_dump round-trip would
    silently destroy every one of them. HotReloadableYaml re-validates
    and picks this up on its very next .get() call (mtime-based), no
    restart needed."""
    text = _RESUME_RULES_PATH.read_text(encoding="utf-8")
    pattern = re.compile(category.yaml_line_pattern, re.MULTILINE)
    match = pattern.search(text)
    if not match:
        raise TrendProposalError(
            f"Could not find the expected config line for '{category.id}' in resume_rules.yaml "
            f"(pattern: {category.yaml_line_pattern!r}) -- the file may have been restructured "
            f"since this category was defined. Apply the change manually and update this category's "
            f"yaml_line_pattern in trend_research_service.py."
        )
    # Preserve the original line's leading indentation exactly; only the
    # "key: value" portion is regenerated.
    leading_ws = re.match(r"^\s*", match.group(0)).group(0)
    new_text = text[: match.start()] + leading_ws + category.yaml_replace_fn(new_value) + text[match.end():]
    _RESUME_RULES_PATH.write_text(new_text, encoding="utf-8")


def approve_trend_proposal(db: Session, log_id: int) -> AdaptationLog:
    entry = db.get(AdaptationLog, log_id)
    if not entry or entry.subsystem != _SUBSYSTEM:
        raise TrendProposalError(f"No trend proposal with id {log_id}.")
    if entry.status != "proposed":
        raise TrendProposalError(f"Proposal {log_id} is '{entry.status}', not pending -- nothing to approve.")

    category = TREND_CATEGORIES.get(entry.parameter)
    if not category:
        raise TrendProposalError(f"Unknown trend category '{entry.parameter}' -- can't safely apply.")

    _apply_yaml_change(category, entry.new_value)
    entry.status = "approved"
    db.commit()
    db.refresh(entry)
    log_activity(db, f"Trend proposal approved: {category.label} {entry.old_value} -> {entry.new_value}.", "INFO")
    return entry


def reject_trend_proposal(db: Session, log_id: int) -> AdaptationLog:
    entry = db.get(AdaptationLog, log_id)
    if not entry or entry.subsystem != _SUBSYSTEM:
        raise TrendProposalError(f"No trend proposal with id {log_id}.")
    if entry.status != "proposed":
        raise TrendProposalError(f"Proposal {log_id} is '{entry.status}', not pending -- nothing to reject.")
    entry.status = "rejected"
    db.commit()
    db.refresh(entry)
    return entry
