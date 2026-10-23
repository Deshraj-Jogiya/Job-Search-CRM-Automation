"""
Monthly upgrade check: the platform looks at its own data and at the candidate's own work, and says what is
worth updating. Nothing here changes the profile by itself. Every profile change it offers is saved as a
pending version (source "upgrade_check") that goes through the same approve/reject step as a LinkedIn import.

Five checks, all mechanical (no LLM, no paid calls):

1. Market demand vs evidence. Every recent posting for a role type the candidate targets is read by the same
   skill rules as the fit checker (fit_service). For each skill: how often it is required, and how strongly
   the profile shows it (in a role or project, only in the skills list, or not at all). Silent below
   MIN_POSTINGS, because a handful of postings ranks nothing. The advice is always to build evidence, never to
   drop a skill.
2. His own work moved on. For each project linked to a GitHub repo, commits made inside the project's own
   dates since the profile was last updated. A project with an end date is only ever updated with work from
   inside those dates: commits after the end date are not counted, and an ended project is never reopened as
   "Present" (his rule, 2026-10-22). For a current project, languages the repo uses that the profile doesn't
   list can be drafted as a pending version.
3. Accomplishment prompts. Two or three specific questions per current role and current project. His answer
   becomes a bullet in his own words, in a pending version. Accomplishments are asked for, never inferred.
4. Outcome learning, through adaptation_service's existing evidence gates (sent / replies minimums).
5. Interview patterns: weak spots that recur across at least MIN_DEBRIEFS mock-interview debriefs.

Runs monthly (scheduler) and on demand; the first report in each ~quarter also carries a quarterly summary.
No email: the results live on /upgrades and a dashboard card.
"""

import json
import os
import re
from datetime import datetime, timedelta

import requests
from sqlalchemy.orm import Session

from ..models import (
    JobPosting, MockInterviewSession, ProfileVariant, ProfileVersion, SearchKeyword, UpgradeReport, utcnow,
)
from . import fit_service
from .activity_logger import log_activity

MIN_POSTINGS = 30
MARKET_WINDOW_DAYS = 90
MIN_SHARE_TO_REPORT = 20  # percent of target postings that require a skill
MIN_DEBRIEFS = 3
RUN_EVERY_DAYS = 30
QUARTER_DAYS = 85  # a little under 90 so a monthly run that lands a day early still starts the next quarter
MAX_ANSWER_CHARS = 400

UPGRADE_SOURCE = "upgrade_check"


class UpgradeServiceError(Exception):
    """User-facing failure (bad answer, unknown target) -- show the message, don't 500."""


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_MONTH_YEAR = re.compile(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{4})|(\d{4})")


def _point(match, end: bool) -> datetime:
    mon, year, bare_year = match.groups()
    if bare_year:
        return datetime(int(bare_year), 12, 31, 23, 59) if end else datetime(int(bare_year), 1, 1)
    month = _MONTHS.get(mon.lower()[:3])
    if not month:
        raise ValueError(mon)
    if not end:
        return datetime(int(year), month, 1)
    nxt = datetime(int(year) + (month == 12), month % 12 + 1, 1)
    return nxt - timedelta(minutes=1)


def parse_date_range(text: str | None) -> tuple[datetime | None, datetime | None, bool]:
    """'Feb 2026 – Present' -> (2026-02-01, None, True); 'Apr 2025 – Sep 2026' -> (start, end of Sep 2026, False);
    'Sep 2026' -> the whole month. (None, None, False) when there's nothing usable."""
    if not text:
        return None, None, False
    is_current = bool(re.search(r"present|current|now", text, re.IGNORECASE))
    points = list(_MONTH_YEAR.finditer(text))
    try:
        if not points:
            return None, None, is_current
        start = _point(points[0], end=False)
        if is_current:
            return start, None, True
        return start, _point(points[-1], end=True), False
    except ValueError:
        return None, None, is_current


# ---------------------------------------------------------------------------
# Check 1: market demand vs evidence
# ---------------------------------------------------------------------------

def _required_terms(profile: dict, description: str) -> set[str]:
    required_text, _ = fit_service._split_required_preferred(description or "")
    low = required_text.lower()
    vocab = list(dict.fromkeys(fit_service._VOCABULARY + fit_service._profile_vocabulary(profile)))
    found = [t for t in vocab if fit_service._mentions(t, low, required_text)]
    # "SQL" inside "SQL Server" is the same requirement, as in skill_overlap
    return {t for t in found if not any(o != t and t.lower() in o.lower().split() for o in found)}


def evidence_strength(profile: dict, term: str) -> dict:
    """'work' (named in a role or project), 'listed' (only the skills list or a certificate) or 'none',
    plus a comparable tool the profile does show when the term itself isn't there."""
    sources = fit_service._evidence_sources(profile)
    work, listed = sources[:-2], sources[-2:]
    where = fit_service._find_evidence(term, work)
    if where:
        return {"level": "work", "where": where}
    where = fit_service._find_evidence(term, listed)
    if where:
        return {"level": "listed", "where": where}
    group = next((g for g in fit_service._EQUIVALENT_GROUPS if term in g), set())
    via = next((o for o in sorted(group - {term}) if fit_service._find_evidence(o, work)), None)
    return {"level": "none", "via": via}


def market_demand(db: Session, profile: dict, now: datetime | None = None) -> dict:
    now = now or utcnow()
    since = now - timedelta(days=MARKET_WINDOW_DAYS)
    targets = [k.keyword for k in db.query(SearchKeyword).filter(SearchKeyword.is_active == True).all()]  # noqa: E712
    families = fit_service.profile_role_families(profile, targets)
    postings = db.query(JobPosting).filter(JobPosting.first_seen_at >= since).all()
    target_postings = [p for p in postings if fit_service.role_families(p.job_title) & families]

    result = {
        "window_days": MARKET_WINDOW_DAYS,
        "postings_in_window": len(postings),
        "target_postings": len(target_postings),
        "min_postings": MIN_POSTINGS,
        "target_roles": sorted(fit_service.FAMILY_LABELS.get(f, f) for f in families),
        "ready": len(target_postings) >= MIN_POSTINGS,
        "skills": [],
        "gaps": [],
    }
    if not result["ready"]:
        return result

    counts: dict[str, int] = {}
    for posting in target_postings:
        for term in _required_terms(profile, posting.job_description):
            counts[term] = counts.get(term, 0) + 1
    n = len(target_postings)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    for term, count in ranked[:25]:
        share = round(100 * count / n)
        entry = {"term": term, "count": count, "share": share, **evidence_strength(profile, term)}
        result["skills"].append(entry)
        if share >= MIN_SHARE_TO_REPORT and entry["level"] != "work":
            result["gaps"].append(entry)
    return result


# ---------------------------------------------------------------------------
# Check 2: his own work moved on
# ---------------------------------------------------------------------------

_REPO_RE = re.compile(r"github\.com/([^/\s]+)/([^/\s#?]+)")
# Repo languages worth listing on a resume; markup and build files are not skills anyone screens for.
_SKIP_LANGUAGES = {"html", "css", "scss", "shell", "dockerfile", "makefile", "jinja", "procfile", "batchfile",
                   "powershell", "jupyter notebook", "hcl", "mako", "smarty"}
_README_NUMBER = re.compile(
    r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(%|x\b|ms\b|tests?\b|rows\b|records\b|postings\b|files\b|GB\b|MB\b|k\b|M\b)",
    re.IGNORECASE,
)


class GitHubClient:
    """Minimal read-only GitHub REST client; GITHUB_TOKEN raises the rate limit when it's set."""

    def __init__(self, token: str | None = None, timeout: int = 10):
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/vnd.github+json"
        token = token if token is not None else os.getenv("GITHUB_TOKEN")
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.timeout = timeout

    def _get(self, path: str, **params):
        r = self.session.get(f"https://api.github.com{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r

    def commit_count(self, owner: str, repo: str, since: datetime, until: datetime | None) -> int:
        params = {"since": since.isoformat() + "Z", "per_page": 100}
        if until:
            params["until"] = until.isoformat() + "Z"
        total = 0
        for page in range(1, 4):  # 300 is plenty to say "a lot happened"
            batch = self._get(f"/repos/{owner}/{repo}/commits", page=page, **params).json()
            total += len(batch)
            if len(batch) < 100:
                break
        return total

    def languages(self, owner: str, repo: str) -> list[str]:
        return list(self._get(f"/repos/{owner}/{repo}/languages").json().keys())

    def readme(self, owner: str, repo: str) -> str:
        r = self.session.get(f"https://api.github.com/repos/{owner}/{repo}/readme",
                             headers={"Accept": "application/vnd.github.raw"}, timeout=self.timeout)
        return r.text if r.ok else ""


def _new_readme_numbers(readme: str, bullets: list[str]) -> list[str]:
    """Measured numbers in the README that no profile bullet for the project mentions."""
    have = re.sub(r"[,\s]", "", " ".join(bullets)).lower()
    seen, out = set(), []
    for value, unit in _README_NUMBER.findall(readme or ""):
        token = f"{value}{unit}".replace(",", "").replace(" ", "")
        if token.lower() in seen or value.replace(",", "") in ("0", "1"):
            continue
        seen.add(token.lower())
        if token.lower() not in have:
            out.append(f"{value}{'' if unit == '%' else ' '}{unit}")
    return out[:6]


def repo_activity(profile: dict, last_profile_update: datetime, github: GitHubClient | None = None,
                  now: datetime | None = None) -> dict:
    now = now or utcnow()
    github = github or GitHubClient()
    items, errors, skipped_ended = [], [], 0
    for index, project in enumerate(profile.get("projects") or []):
        url = project.get("github_url") or project.get("url") or ""
        match = _REPO_RE.search(url)
        if project.get("confidential") or not match:
            continue
        start, end, is_current = parse_date_range(project.get("date") or project.get("dates"))
        if start is None:
            continue
        since = max(start, last_profile_update)
        until = None if is_current else end
        if until is not None and since >= until:
            skipped_ended += 1  # ended before the profile was last updated: nothing new can belong to it
            continue
        owner, repo = match.group(1), match.group(2).removesuffix(".git")
        try:
            commits = github.commit_count(owner, repo, since, until)
            if not commits:
                continue
            item = {
                "project_index": index, "name": project.get("name"), "repo": f"{owner}/{repo}",
                "date": project.get("date"), "is_current": is_current, "commits": commits,
                "since": since.strftime("%Y-%m-%d"), "until": until.strftime("%Y-%m-%d") if until else None,
                "new_numbers": _new_readme_numbers(github.readme(owner, repo), project.get("bullets") or []),
                "new_languages": [],
            }
            if is_current:
                listed = {t.lower() for t in project.get("technologies") or []}
                item["new_languages"] = [lang for lang in github.languages(owner, repo)
                                         if lang.lower() not in listed and lang.lower() not in _SKIP_LANGUAGES]
            items.append(item)
        except requests.RequestException as e:
            errors.append(f"{owner}/{repo}: {e.__class__.__name__}")
    return {"since_profile_update": last_profile_update.strftime("%Y-%m-%d"), "items": items,
            "ended_projects_not_checked": skipped_ended, "errors": errors}


# ---------------------------------------------------------------------------
# Check 3: accomplishment prompts
# ---------------------------------------------------------------------------

def accomplishment_prompts(profile: dict, last_profile_update: datetime) -> list[dict]:
    month = last_profile_update.strftime("%B %Y")
    prompts = []
    for index, role in enumerate(profile.get("experience") or []):
        _, _, current = parse_date_range(role.get("date") or role.get("dates"))
        if not current:
            continue
        where = role.get("company") or "this role"
        prompts.append({
            "target": f"experience:{index}",
            "label": f"{role.get('role') or role.get('title') or 'Role'} at {where}",
            "questions": [
                f"Any result at {where} since {month} you can put a number on (time saved, accuracy, volume, cost)?",
                "A tool, system or kind of data you started working with there?",
                "Anything you led, shipped, trained someone on, or were recognised for?",
            ],
        })
    for index, project in enumerate(profile.get("projects") or []):
        _, _, current = parse_date_range(project.get("date") or project.get("dates"))
        if not current or project.get("confidential"):
            continue
        prompts.append({
            "target": f"project:{index}",
            "label": f"Project: {project.get('name')}",
            "questions": [
                f"What changed in {project.get('name')} since {month}: a feature, a measured result, or a new tool?",
                "Did anyone use it, or did it replace something slower?",
            ],
        })
    return prompts


# ---------------------------------------------------------------------------
# Check 4: outcomes (gated by the adaptation layer's own evidence rules)
# ---------------------------------------------------------------------------

def outcome_learning(db: Session) -> dict:
    from . import adaptation_service
    comparisons = []
    for comparison_type in adaptation_service._COMPARISON_SEGMENTATION_FNS:
        try:
            r = adaptation_service.evaluate_comparison(db, comparison_type)
        except Exception as e:  # a config problem shouldn't sink the other four checks
            comparisons.append({"comparison_type": comparison_type, "state": "ERROR", "error": str(e)})
            continue
        comparisons.append({k: r.get(k) for k in (
            "comparison_type", "state", "total_sent", "total_replied", "min_samples_required",
            "min_replies_required", "best_segment", "worst_segment", "confidence_interval")})
    reportable = [c for c in comparisons if c["state"] == "REPORTABLE"]
    first = comparisons[0] if comparisons else {}
    return {"ready": bool(reportable), "comparisons": comparisons,
            "sent": first.get("total_sent", 0), "replied": first.get("total_replied", 0),
            "min_sent": min((c.get("min_samples_required") or 0) for c in comparisons) if comparisons else 0,
            "min_replies": first.get("min_replies_required", 0)}


# ---------------------------------------------------------------------------
# Check 5: interview patterns
# ---------------------------------------------------------------------------

_STOP = {"the", "and", "for", "with", "that", "this", "your", "you", "was", "were", "not", "but", "into", "from",
         "answer", "answers", "question", "questions", "more", "less", "when", "about", "could", "would", "should",
         "they", "their", "what", "how", "why", "too", "very", "also", "than", "then", "just", "only"}


def _theme_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in _STOP}


def interview_patterns(db: Session) -> dict:
    sessions = (db.query(MockInterviewSession).filter(MockInterviewSession.debrief_json != None)  # noqa: E711
                .order_by(MockInterviewSession.ended_at).all())
    issues = []  # (session_id, round, issue text)
    for s in sessions:
        try:
            debrief = json.loads(s.debrief_json)
        except (TypeError, ValueError):
            continue
        for area in debrief.get("areas_to_improve") or []:
            text = area.get("issue") if isinstance(area, dict) else str(area)
            if text:
                issues.append((s.id, s.round_name, text))
    result = {"debriefs": len(sessions), "min_debriefs": MIN_DEBRIEFS, "ready": len(sessions) >= MIN_DEBRIEFS,
              "themes": []}
    if not result["ready"]:
        return result
    # Greedy grouping by shared words; a theme only counts when it shows up in two or more sessions.
    groups: list[dict] = []
    for sid, round_name, text in issues:
        words = _theme_words(text)
        home = next((g for g in groups if len(words & g["words"]) / max(1, len(words | g["words"])) >= 0.3), None)
        if home is None:
            home = {"words": set(words), "examples": [], "sessions": set()}
            groups.append(home)
        home["examples"].append(text)
        home["sessions"].add(sid)
    result["themes"] = [{"example": g["examples"][0], "count": len(g["examples"]), "sessions": sorted(g["sessions"])}
                        for g in groups if len(g["sessions"]) >= 2]
    result["themes"].sort(key=lambda t: -len(t["sessions"]))
    return result


# ---------------------------------------------------------------------------
# Running and storing
# ---------------------------------------------------------------------------

def _default_variant(db: Session) -> ProfileVariant | None:
    return db.query(ProfileVariant).filter(ProfileVariant.is_default == True).first()  # noqa: E712


def _active_version(db: Session, variant: ProfileVariant) -> ProfileVersion | None:
    return (db.query(ProfileVersion)
            .filter(ProfileVersion.variant_id == variant.id, ProfileVersion.is_active == True)  # noqa: E712
            .first())


def latest_report(db: Session) -> UpgradeReport | None:
    return db.query(UpgradeReport).order_by(UpgradeReport.created_at.desc()).first()


def _quarter_summary(db: Session, report: dict, now: datetime) -> dict:
    since = now - timedelta(days=90)
    versions = db.query(ProfileVersion).filter(ProfileVersion.created_at >= since).all()
    earlier = (db.query(UpgradeReport).filter(UpgradeReport.created_at <= now - timedelta(days=60))
               .order_by(UpgradeReport.created_at.desc()).first())
    top_now = [s["term"] for s in report["market"]["skills"][:10]]
    top_then = []
    if earlier:
        top_then = [s["term"] for s in json.loads(earlier.report_json).get("market", {}).get("skills", [])[:10]]
    return {
        "profile_versions": len(versions),
        "approved_from_checks": sum(1 for v in versions if v.source == UPGRADE_SOURCE and v.is_active),
        "compared_with": earlier.created_at.strftime("%Y-%m-%d") if earlier else None,
        "rising_skills": [t for t in top_now if top_then and t not in top_then],
        "falling_skills": [t for t in top_then if t not in top_now],
    }


def run_check(db: Session, github: GitHubClient | None = None, now: datetime | None = None) -> UpgradeReport:
    now = now or utcnow()
    variant = _default_variant(db)
    version = _active_version(db, variant) if variant else None
    if not version:
        raise UpgradeServiceError("Set up a profile first: the checks compare everything against it.")
    profile = json.loads(version.content_json)
    last_update = version.created_at or now

    report = {
        "profile_version_id": version.id,
        "market": market_demand(db, profile, now),
        "repos": repo_activity(profile, last_update, github, now),
        "prompts": accomplishment_prompts(profile, last_update),
        "outcomes": outcome_learning(db),
        "interviews": interview_patterns(db),
    }
    last_quarterly = (db.query(UpgradeReport).filter(UpgradeReport.is_quarterly == True)  # noqa: E712
                      .order_by(UpgradeReport.created_at.desc()).first())
    is_quarterly = last_quarterly is None or last_quarterly.created_at <= now - timedelta(days=QUARTER_DAYS)
    if is_quarterly:
        report["quarter"] = _quarter_summary(db, report, now)

    row = UpgradeReport(created_at=now, is_quarterly=is_quarterly, report_json=json.dumps(report))
    db.add(row)
    db.commit()
    db.refresh(row)
    log_activity(db, f"Monthly upgrade check ran: {action_count(report)} item(s) to look at.")
    return row


def run_if_due(db: Session) -> UpgradeReport | None:
    last = latest_report(db)
    if last and last.created_at > utcnow() - timedelta(days=RUN_EVERY_DAYS):
        return None
    if not _default_variant(db):
        return None
    return run_check(db)


def action_count(report: dict) -> int:
    """Things that need him: skill gaps, projects with new work, open prompts, recurring interview themes."""
    return (len(report["market"]["gaps"]) + len(report["repos"]["items"]) + len(report["prompts"])
            + len(report["interviews"]["themes"]) + (1 if report["outcomes"]["ready"] else 0))


def card_summary(db: Session) -> dict | None:
    row = latest_report(db)
    if not row:
        return None
    report = json.loads(row.report_json)
    return {"created_at": row.created_at, "count": action_count(report), "is_quarterly": row.is_quarterly}


# ---------------------------------------------------------------------------
# Turning an item into a pending profile version
# ---------------------------------------------------------------------------

def _pending_version(db: Session, profile: dict, variant_id: int, summary: str) -> ProfileVersion:
    version = ProfileVersion(variant_id=variant_id, content_json=json.dumps(profile, indent=2),
                             source=UPGRADE_SOURCE, change_summary=summary, is_active=False)
    db.add(version)
    db.commit()
    db.refresh(version)
    log_activity(db, f"Monthly check drafted a profile change (pending approval): {summary}")
    return version


def _current_profile(db: Session) -> tuple[dict, int]:
    variant = _default_variant(db)
    version = _active_version(db, variant) if variant else None
    if not version:
        raise UpgradeServiceError("No active profile to update.")
    return json.loads(version.content_json), variant.id


def _clean_bullet(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip()).lstrip("-•* ").strip()
    if not text:
        raise UpgradeServiceError("Write an answer first.")
    if len(text) > MAX_ANSWER_CHARS:
        raise UpgradeServiceError(f"Keep it under {MAX_ANSWER_CHARS} characters: one resume bullet.")
    text = text[0].upper() + text[1:]
    return text if text[-1] in ".!?" else text + "."


def draft_answer(db: Session, target: str, answer: str) -> ProfileVersion:
    """His answer, in his words, added as a bullet to the role or project it answers for."""
    bullet = _clean_bullet(answer)
    profile, variant_id = _current_profile(db)
    kind, _, raw_index = target.partition(":")
    section = {"experience": "experience", "project": "projects"}.get(kind)
    entries = (profile.get(section) or []) if section else []
    if not raw_index.isdigit() or int(raw_index) >= len(entries):
        raise UpgradeServiceError("That role or project is no longer in the profile. Run the check again.")
    entry = entries[int(raw_index)]
    entry.setdefault("bullets", []).append(bullet)
    name = entry.get("company") or entry.get("name") or "entry"
    return _pending_version(db, profile, variant_id, f"Monthly check: new bullet for {name} (your own words): {bullet}")


def draft_languages(db: Session, project_index: int, languages: list[str]) -> ProfileVersion:
    """Adds repo languages to a CURRENT project's technologies. Ended projects are left as they are."""
    profile, variant_id = _current_profile(db)
    projects = profile.get("projects") or []
    if not 0 <= project_index < len(projects):
        raise UpgradeServiceError("That project is no longer in the profile. Run the check again.")
    project = projects[project_index]
    if not parse_date_range(project.get("date") or project.get("dates"))[2]:
        raise UpgradeServiceError("This project has ended; its tools stay as they were during the project.")
    listed = {t.lower() for t in project.get("technologies") or []}
    new = [lang for lang in languages if lang and lang.lower() not in listed]
    if not new:
        raise UpgradeServiceError("Those tools are already listed.")
    project.setdefault("technologies", []).extend(new)
    return _pending_version(db, profile, variant_id,
                            f"Monthly check: added {', '.join(new)} to {project.get('name')} (used in its repo)")
