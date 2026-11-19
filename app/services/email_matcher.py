"""
Finds which application a job email is about (email_sync_service.py), by rules only.

1. The person: a sender already contacted through Outreach, or saved as an application's recruiter, points straight
   at that application.
2. The company: the name the email gives ("Ibotta", "Whatnot Hiring Team") and the names its address carries (a
   Workday or iCIMS tenant such as "micron@myworkday.com", or the company's own domain) are compared with every
   company that has an application: its normalized name, the name without spaces, its website domain, its Workday
   tenant and its job-board slugs. Never a bare "the name appears somewhere in the email" check; that is how the
   old monitor matched bank alerts.
3. The application, among that company's: a job ID in the email that appears in the posting's ID or URL decides it;
   otherwise the role is compared word by word with the posting's title, and the application's status counts a
   little (a rejection is about an application already sent, a confirmation about one not yet marked Applied).

Result: "matched" (one clear application), "choose" (several close ones, the inbox asks), or "unmatched" (none; the
inbox offers to add it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, joinedload

from ..models import Company, JobApplication, JobPosting, OutreachMessage
from .company_utils import normalize_company_name, normalize_title
from .email_classifier import Classification
from .email_reader_service import RawEmail

PRE_APPLIED = ("Ingested", "Tailored", "Needs Review", "Pending Confirmation", "Approved")
SENT = ("Applied", "Interviewing")
CLOSED = ("Offer", "Not Selected", "Rejected")
_STOP = {"the", "a", "an", "of", "and", "for", "to", "in", "at", "with", "on", "i", "ii", "iii", "iv", "1", "2", "3",
         "sr", "senior", "jr", "junior", "remote", "hybrid", "onsite", "us", "usa", "team", "role", "position", "job",
         "new", "grad", "level", "-", "&", "or"}
_GENERIC_DOMAIN_LABELS = {"gmail", "outlook", "yahoo", "hotmail", "icloud", "mail", "email", "careers", "jobs"}


@dataclass
class MatchResult:
    state: str  # matched | choose | unmatched
    application_id: int | None = None
    candidate_ids: list[int] = field(default_factory=list)
    how: str = ""


# Words too common in company names to identify one by themselves.
_GENERIC_NAME_WORDS = {"health", "healthcare", "services", "service", "bank", "university", "college", "school", "medical",
                       "medicine", "center", "centre", "system", "systems", "data", "ai", "labs", "lab", "global", "group",
                       "the", "of", "and", "at", "for", "capital", "financial", "partners", "energy", "media", "network",
                       "networks", "software", "digital", "consulting", "us", "usa", "america", "american", "national",
                       "international", "company", "corporation", "inc", "llc", "hospital", "institute", "foundation",
                       "cloud", "careers", "jobs", "team", "talent", "hr", "venture", "ventures", "joint"}


def _name_words(value: str | None) -> set[str]:
    words = set(re.findall(r"[a-z0-9]+", normalize_company_name(value or "")))
    return {w for w in words if w not in _GENERIC_NAME_WORDS and len(w) >= 3}


def compact(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize_company_name(value or ""))


def _title_words(value: str | None) -> set[str]:
    words = re.findall(r"[a-z0-9+#]+", normalize_title(value or ""))
    return {w for w in words if w not in _STOP}


def title_similarity(a: str | None, b: str | None) -> float:
    wa, wb = _title_words(a), _title_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


class CompanyIndex:
    """Companies that have applications, keyed every way an email might name them. Built once per sync."""

    def __init__(self, db: Session):
        self.keys: dict[str, set[int]] = {}  # key -> company ids
        self.domains: dict[str, set[int]] = {}
        self.apps_by_company: dict[int, list[JobApplication]] = {}
        self.raw_names: dict[str, set[int]] = {}  # posting name compact -> application ids (postings with no company)
        apps = (db.query(JobApplication).join(JobPosting)
                .options(joinedload(JobApplication.posting).joinedload(JobPosting.company)).all())
        for app in apps:
            company = app.posting.company
            if company is None:
                self.raw_names.setdefault(compact(app.posting.company_name_raw), set()).add(app.id)
                continue
            self.apps_by_company.setdefault(company.id, []).append(app)
        for company in db.query(Company).filter(Company.id.in_(list(self.apps_by_company))).all() if self.apps_by_company else []:
            names = {company.name, company.normalized_name}
            names |= {a.posting.company_name_raw for a in self.apps_by_company[company.id]}
            for name in names:
                key = compact(name)
                if len(key) >= 2:
                    self.keys.setdefault(key, set()).add(company.id)
            for slug in (company.greenhouse_slug, company.lever_slug, company.ashby_slug, company.workable_slug,
                         company.smartrecruiters_slug, (company.workday_board or "").split("|")[0]):
                key = re.sub(r"[^a-z0-9]", "", (slug or "").lower())
                if len(key) >= 3:
                    self.keys.setdefault(key, set()).add(company.id)
            domain = (company.website_domain or "").lower().removeprefix("www.")
            if domain:
                self.domains.setdefault(domain, set()).add(company.id)
                label = domain.split(".")[0]
                if len(label) >= 3:
                    self.keys.setdefault(label, set()).add(company.id)
        self.words = {}
        for company in db.query(Company).filter(Company.id.in_(list(self.apps_by_company))).all() if self.apps_by_company else []:
            for name in {company.name} | {a.posting.company_name_raw for a in self.apps_by_company[company.id]}:
                w = _name_words(name)
                if w:
                    self.words.setdefault(company.id, set()).update(w)
        self.app_by_id = {a.id: a for apps_ in self.apps_by_company.values() for a in apps_}
        for a in apps:
            self.app_by_id.setdefault(a.id, a)

    def companies_for_domain(self, domain: str) -> set[int]:
        domain = (domain or "").lower()
        out = set()
        for known, ids in self.domains.items():
            if domain == known or domain.endswith("." + known):
                out |= ids
        return out

    def lookup(self, name: str | None) -> tuple[set[int], str]:
        """Company ids for a name: the same name ignoring spaces and suffixes first; then one name's distinctive words
        all inside the other's ("Wolverine" ~ "Wolverine Trading", "Duke" ~ "Duke University Health System"); then a
        tenant code that starts the full name ("wolve" ~ "wolverinetrading", "firstcitizens" ~ "firstcitizensbank").
        Words such as "health", "services" or "bank" never match on their own."""
        key = compact(name)
        if len(key) < 2:
            return set(), ""
        if key in self.keys:
            return set(self.keys[key]), "exact"
        words = _name_words(name)
        close = set()
        if words:
            for cid, cwords in self.words.items():
                if words <= cwords or (cwords and cwords <= words):
                    close.add(cid)
        if not close and len(key) >= 5:
            for known, ids in self.keys.items():
                if len(known) >= 5 and (known.startswith(key) or key.startswith(known)):
                    close |= ids
        return close, "close" if close else ""

    def raw_lookup(self, name: str | None) -> set[int]:
        key = compact(name)
        return set(self.raw_names.get(key, set())) if len(key) >= 3 else set()


def known_people(db: Session) -> dict[str, set[int | None]]:
    """Email address -> applications it is tied to: people written to through Outreach, and saved recruiters."""
    people: dict[str, set[int | None]] = {}
    for address, app_id in (db.query(OutreachMessage.recipient_address, OutreachMessage.application_id)
                            .filter(OutreachMessage.recipient_address.like("%@%")).all()):
        people.setdefault(address.strip().lower(), set()).add(app_id)
    for address, app_id in db.query(JobApplication.recruiter_email, JobApplication.id).filter(
            JobApplication.recruiter_email.isnot(None)).all():
        if "@" in (address or ""):
            people.setdefault(address.strip().lower(), set()).add(app_id)
    return people


def _ref_in_posting(job_ref: str | None, posting: JobPosting) -> bool:
    if not job_ref or len(job_ref) < 4:
        return False
    ref = job_ref.lower()
    hay = " ".join(filter(None, [posting.external_id, posting.job_url, posting.job_title])).lower()
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(ref)}(?![0-9])", hay))


def _status_weight(kind: str, status: str) -> int:
    """How likely an email of this kind is about an application in this status. An application already sent is the
    likeliest for every kind: the CRM often holds a duplicate, never-sent copy of the same posting from another source."""
    if status in SENT:
        return 10
    if status == "Approved":
        return 7
    if status in PRE_APPLIED:
        return 5
    return 2 if status == "Not Selected" and kind == "rejection" else 0


def rank(apps: list[JobApplication], cls: Classification) -> list[tuple[float, JobApplication, bool]]:
    scored = []
    for app in apps:
        by_ref = _ref_in_posting(cls.job_ref, app.posting)
        score = (100 if by_ref else 0) + 60 * title_similarity(cls.role, app.posting.job_title) + _status_weight(cls.kind, app.status)
        scored.append((score, app, by_ref))
    scored.sort(key=lambda t: (-t[0], -(t[1].id)))
    return scored


def match(index: CompanyIndex, people: dict[str, set[int | None]], raw: RawEmail, cls: Classification) -> MatchResult:
    # 1. someone already contacted or saved as the recruiter
    linked = {a for a in people.get(raw.sender_email, set()) if a and a in index.app_by_id}
    if len(linked) == 1:
        return MatchResult("matched", next(iter(linked)), [next(iter(linked))], "sender is someone you contacted for it")

    # 2. the company
    company_ids, how = set(), ""
    if cls.company:
        company_ids, how = index.lookup(cls.company)
        how = f"company '{cls.company}' ({how})" if company_ids else ""
    if not company_ids:
        by_domain = index.companies_for_domain(raw.sender_domain)
        if by_domain:
            company_ids, how = by_domain, f"sender domain {raw.sender_domain}"
    if not company_ids:
        for hint in cls.hints:
            ids, kind = index.lookup(hint)
            if ids:
                company_ids, how = ids, f"sender address '{hint}' ({kind})"
                break
    apps = [a for cid in company_ids for a in index.apps_by_company.get(cid, [])]
    if not apps:
        raw_ids = index.raw_lookup(cls.company) or next((index.raw_lookup(h) for h in cls.hints if index.raw_lookup(h)), set())
        apps = [index.app_by_id[i] for i in raw_ids if i in index.app_by_id]
        how = how or (f"posting company '{cls.company}'" if apps else "")
    if linked:
        apps = [index.app_by_id[i] for i in linked] + [a for a in apps if a.id not in linked]
    if not apps:
        return MatchResult("unmatched", how="no tracked company matches")

    # 3. the application
    ranked = rank(apps, cls)
    best_score, best, by_ref = ranked[0]
    ids = [a.id for _, a, _ in ranked[:6]]
    best_sim = title_similarity(cls.role, best.posting.job_title)
    if by_ref and (len(ranked) == 1 or not ranked[1][2]):
        return MatchResult("matched", best.id, ids, f"{how}; job ID {cls.job_ref}")
    if cls.role and best_sim < 0.3:
        # The email names a role none of this company's applications has (Torc "ML Engineer I" against a saved
        # "ML Engineer II - 3D"): likely a role that never made it into the CRM, so ask rather than guess.
        return MatchResult("choose", None, ids, f"{how}; role '{cls.role}' is not one of this company's applications")
    if len(ranked) == 1:
        return MatchResult("matched", best.id, ids, how)
    second_sim = title_similarity(cls.role, ranked[1][1].posting.job_title)
    if best_sim >= 0.6 and (best_sim - second_sim >= 0.3 or
                            _status_weight(cls.kind, best.status) > _status_weight(cls.kind, ranked[1][1].status)):
        return MatchResult("matched", best.id, ids, f"{how}; role '{cls.role}'")
    return MatchResult("choose", None, ids, f"{how}; {len(ranked)} applications at this company")
