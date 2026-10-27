"""Contact discovery for the public showcase instance, by rules.

A real instance finds contacts with a web search (contact_discovery_service, Tavily + Hunter). The showcase has no
search keys and its companies are fictional, so it lists the three kinds of people that search looks for at any
company: the manager who would own the role, a peer on the team, and the recruiter. Names are fictional and fixed per
company; every reason is built only from the posting and the demo profile; nothing has a LinkedIn URL or email, so a
draft goes to LinkedIn as a connection note the visitor would send by hand.
"""
import hashlib
import re

_FIRST = ["Avery", "Morgan", "Riley", "Casey", "Jamie", "Rowan", "Quinn", "Dana", "Hayden", "Reese", "Skyler", "Emerson"]
_LAST = ["Nakamura", "Okonkwo", "Lindqvist", "Ferreira", "Haddad", "Kowalski", "Mendoza", "Chandler", "Sato", "Abara",
         "Whitfield", "Novak"]
SOURCE = "sample contact (demo, by rules)"


def _name(company: str, slot: int) -> str:
    digest = hashlib.sha256(f"{company}|{slot}".encode()).digest()
    return f"{_FIRST[digest[0] % len(_FIRST)]} {_LAST[digest[1] % len(_LAST)]}"


def _team(job_title: str) -> str:
    title = (job_title or "").lower()
    if "analytics" in title:
        return "Analytics Engineering"
    if "machine learning" in title or re.search(r"\bml\b", title):
        return "ML Platform"
    return "Data Engineering"


def discover(company: str, job_title: str, profile: dict | None) -> list[dict]:
    profile = profile or {}
    team = _team(job_title)
    school = next((e.get("school") for e in profile.get("education") or [] if e.get("school")), None)
    current = next((e for e in profile.get("experience") or []), {})
    peer_reason = f"Works as a {job_title} on the team this role joins"
    if current.get("role"):
        peer_reason += f"; same kind of work as your current {current['role']} role"
    if school:
        peer_reason += f". Also attended {school}"
    contact = lambda slot, title, reason: {
        "name": _name(company, slot), "title": f"{title}, {company}", "linkedin_url": None, "suggested_email": None,
        "email_confidence": None, "source": SOURCE, "reason": reason,
    }
    return [
        contact(0, f"{team} Manager", f"Manages the {team} team the {job_title} role reports into: the person who decides"),
        contact(1, job_title, peer_reason),
        contact(2, "Technical Recruiter", f"Recruits for {team} roles at {company}: can confirm where the application stands"),
    ]
