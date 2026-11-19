"""
The general interview-prep content that is NOT the drafted answers: depth cards for every experience, project and
certification, the common-question bank, and the coding practice plan.

Where each part lives, and why:
- The question bank and the coding plan (app/prep_content/*.json) do not depend on anyone's profile, so they ship
  in the repo and every instance (personal, public repo, demo) shows the same content.
- A person's own deep-dive cards and their "my example" lines are personal, so they live in their database, inside
  the active ProfileInterviewPrep row (keys "deep_dives" and "question_examples", carried over by the answer
  refresh). They never go in the repo.
- Every profile item without a written card still gets an outline built by rules from the profile itself (its
  bullets, technologies and numbers, plus the generic follow-up questions for the tools it names), so a new profile,
  a fork, or the demo always has something useful, and nothing is invented: the outline only repeats the profile.
- A written card stores a fingerprint of the profile item it was written from. When the item changes, the card is
  shown as "profile changed since" so it can be reviewed, rather than silently going out of date.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from functools import lru_cache

from . import resume_ats

_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prep_content")


@lru_cache(maxsize=None)
def _load(name: str) -> dict:
    with open(os.path.join(_DIR, name), encoding="utf-8") as f:
        return json.load(f)


def question_bank() -> dict:
    return _load("question_bank.json")


def coding_practice() -> dict:
    return _load("coding_practice.json")


def deep_dive_guide() -> dict:
    return _load("deep_dive_guide.json")


def all_questions() -> list[dict]:
    return [dict(q, section=s["title"], kind=s["kind"]) for s in question_bank()["sections"] for q in s["questions"]]


def question_by_id(qid: str) -> dict | None:
    return next((q for q in all_questions() if q["id"] == qid), None)


def all_problems() -> list[dict]:
    """Every practice problem once, with the pattern(s) it belongs to (a problem can sit under two patterns)."""
    seen: dict[str, dict] = {}
    for p in coding_practice()["patterns"]:
        for prob in p["problems"]:
            entry = seen.setdefault(prob["slug"], dict(prob, patterns=[]))
            entry["patterns"].append({"id": p["id"], "name": p["name"]})
    return list(seen.values())


# --------------------------------------------------------------------------- deep-dive cards

def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60] or "item"


def _fingerprint(item) -> str:
    return hashlib.sha1(json.dumps(item, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def _cert_text(cert) -> str:
    if isinstance(cert, dict):
        return " - ".join(str(cert.get(k)) for k in ("name", "issuer", "date") if cert.get(k))
    return str(cert)


def profile_items(profile: dict) -> list[dict]:
    """Every experience, non-confidential project and certification as {key, kind, title, subtitle, item}."""
    items = []
    for e in profile.get("experience") or []:
        items.append({"key": "exp-" + slug(f"{e.get('company', '')} {e.get('role', '')}"), "kind": "experience",
                      "title": e.get("company", ""), "subtitle": " | ".join(x for x in (e.get("role"), e.get("date")) if x),
                      "item": e})
    for p in profile.get("projects") or []:
        if p.get("confidential"):
            continue  # NDA work never becomes an interview card, same as every other prep entry point
        items.append({"key": "proj-" + slug(p.get("name", "")), "kind": "project", "title": p.get("name", ""),
                      "subtitle": p.get("date", ""), "item": p})
    for c in profile.get("certifications") or []:
        text = _cert_text(c)
        items.append({"key": "cert-" + slug(text), "kind": "certification", "title": text, "subtitle": "", "item": c})
    return items


_NUMBER = re.compile(r"\d")


def _tools_in(text: str, profile: dict, item: dict) -> list[str]:
    tools = list(item.get("technologies") or [])
    known = [t for group in (profile.get("skills") or {}).values() for t in (group or [])]
    for t in known:
        if t not in tools and resume_ats.has_term(text, t):
            tools.append(t)
    return tools


def _tool_follow_ups(tools: list[str]) -> list[dict]:
    guide = deep_dive_guide()["tool_follow_ups"]
    out, seen = [], set()
    for tool in tools:
        for name, questions in guide.items():
            if name.lower() in tool.lower() or tool.lower() in name.lower():
                for q in questions:
                    if q not in seen:
                        seen.add(q)
                        out.append({"q": q, "a": "", "tool": name})
    return out[:12]


def outline_card(profile: dict, entry: dict) -> dict:
    """A card built only from the profile item: nothing here is written by anyone, so nothing can be invented."""
    item = entry["item"]
    if entry["kind"] == "certification":
        text = entry["title"]
        tools = _tools_in(text, profile, {})
        return {"key": entry["key"], "kind": "certification", "title": entry["title"], "subtitle": "", "outline": True,
                "short": "", "covered": [], "concepts": [], "applied": "", "follow_ups": _tool_follow_ups(tools),
                "skills": [{"skill": t, "where": ""} for t in tools], "careful": []}
    bullets = [b if isinstance(b, str) else b.get("text", "") for b in item.get("bullets") or []]
    text = " ".join(bullets + list(item.get("technologies") or []))
    tools = _tools_in(text, profile, item)
    return {
        "key": entry["key"], "kind": entry["kind"], "title": entry["title"], "subtitle": entry["subtitle"], "outline": True,
        "short": bullets[0] if bullets else "",
        "flow": [], "decisions": [], "hardest": "",
        "numbers": [{"claim": b, "how": ""} for b in bullets if _NUMBER.search(b)],
        "my_part": "", "change": "",
        "follow_ups": _tool_follow_ups(tools),
        "skills": [{"skill": t, "where": ""} for t in tools],
        "careful": [], "bullets": bullets,
    }


def build_cards(profile: dict, stored: list[dict] | None) -> list[dict]:
    """One card per profile item: the written card when there is one (flagged if the item changed since), otherwise
    the rule-built outline. Written cards whose item is gone from the profile are returned separately as orphans."""
    by_key = {c["key"]: c for c in stored or []}
    cards = []
    for entry in profile_items(profile):
        written = by_key.pop(entry["key"], None)
        if written:
            card = dict(written, title=entry["title"], subtitle=entry["subtitle"], kind=entry["kind"], outline=False)
            card["changed_since"] = written.get("source_hash") not in (None, _fingerprint(entry["item"]))
            if entry["kind"] != "certification":
                card["bullets"] = [b if isinstance(b, str) else b.get("text", "") for b in entry["item"].get("bullets") or []]
        else:
            card = outline_card(profile, entry)
            card["changed_since"] = False
        cards.append(card)
    return cards


def orphan_cards(profile: dict, stored: list[dict] | None) -> list[dict]:
    keys = {e["key"] for e in profile_items(profile)}
    return [c for c in stored or [] if c.get("key") not in keys]


def fingerprint_for(profile: dict, key: str) -> str | None:
    entry = next((e for e in profile_items(profile) if e["key"] == key), None)
    return _fingerprint(entry["item"]) if entry else None


# Editing: each list field is one line per entry, "question :: answer" where a field has two parts.
CARD_TEXT_FIELDS = ("short", "hardest", "my_part", "change", "applied")
CARD_LINE_FIELDS = ("flow", "careful", "covered", "concepts")
CARD_PAIR_FIELDS = {"decisions": ("q", "a"), "numbers": ("claim", "how"), "follow_ups": ("q", "a"), "skills": ("skill", "where")}


def card_to_form(card: dict) -> dict:
    form = {f: card.get(f) or "" for f in CARD_TEXT_FIELDS}
    for f in CARD_LINE_FIELDS:
        form[f] = "\n".join(card.get(f) or [])
    for f, (a, b) in CARD_PAIR_FIELDS.items():
        form[f] = "\n".join(f"{x.get(a, '')} :: {x.get(b, '')}".rstrip(" :") for x in card.get(f) or [])
    return form


def card_from_form(key: str, form: dict, source_hash: str | None) -> dict:
    card: dict = {"key": key, "source_hash": source_hash}
    for f in CARD_TEXT_FIELDS:
        card[f] = (form.get(f) or "").strip()
    for f in CARD_LINE_FIELDS:
        card[f] = [line.strip() for line in (form.get(f) or "").splitlines() if line.strip()]
    for f, (a, b) in CARD_PAIR_FIELDS.items():
        rows = []
        for line in (form.get(f) or "").splitlines():
            if not line.strip():
                continue
            left, _, right = line.partition("::")
            rows.append({a: left.strip(), b: right.strip()})
        card[f] = rows
    return card


def card_text(card: dict) -> str:
    parts = [card.get(f) or "" for f in CARD_TEXT_FIELDS]
    for f in CARD_LINE_FIELDS:
        parts += card.get(f) or []
    for f, (a, b) in CARD_PAIR_FIELDS.items():
        parts += [f"{x.get(a, '')} {x.get(b, '')}" for x in card.get(f) or []]
    return "\n".join(parts)


# --------------------------------------------------------------------------- matching a posting (tailored prep)

def cards_for_posting(cards: list[dict], job_description: str, limit: int = 6) -> list[dict]:
    """The cards whose content shows the posting's own terms, most matches first. Used by the per-job prep to point
    at the general cards worth reviewing; nothing is copied."""
    terms = resume_ats.posting_terms(job_description or "")
    scored = []
    for card in cards:
        text = card_text(card) + " " + " ".join(card.get("bullets") or [])
        hits = [t for t in terms if resume_ats.has_term(text, t)]
        if hits:
            scored.append({"key": card["key"], "title": card["title"], "subtitle": card.get("subtitle", ""),
                           "outline": card.get("outline", False), "terms": hits})
    scored.sort(key=lambda c: -len(c["terms"]))
    return scored[:limit]


def questions_for_posting(job_description: str, limit: int = 8) -> list[dict]:
    terms = resume_ats.posting_terms(job_description or "")
    out = []
    for q in all_questions():
        text = " ".join(str(v) for k, v in q.items() if k in ("q", "definition", "use_when"))
        text += " " + " ".join(q.get("steps") or [])
        hits = [t for t in terms if resume_ats.has_term(text, t)]
        if hits:
            out.append({"id": q["id"], "q": q["q"], "section": q["section"], "terms": hits})
    out.sort(key=lambda q: -len(q["terms"]))
    return out[:limit]


# --------------------------------------------------------------------------- practice keys

def all_card_keys(profile: dict | None = None) -> list[str]:
    keys = ["qb:" + q["id"] for q in all_questions()]
    keys += ["pat:" + p["id"] for p in coding_practice()["patterns"]]
    keys += ["code:" + p["slug"] for p in all_problems()]
    if profile:
        keys += ["dd:" + e["key"] for e in profile_items(profile)]
    return keys


def describe_key(key: str, profile: dict | None = None) -> dict:
    """What a practice key points at: a label and a link."""
    kind, _, ident = key.partition(":")
    if kind == "qb":
        q = question_by_id(ident)
        return {"label": q["q"] if q else ident, "group": "Question", "href": f"/interview-prep/questions#q-{ident}"}
    if kind == "pat":
        p = next((p for p in coding_practice()["patterns"] if p["id"] == ident), None)
        return {"label": p["name"] if p else ident, "group": "Pattern", "href": f"/interview-prep/coding#pat-{ident}"}
    if kind == "code":
        p = next((p for p in all_problems() if p["slug"] == ident), None)
        return {"label": p["title"] if p else ident, "group": "Problem", "href": p["url"] if p else "/interview-prep/coding",
                "external": bool(p)}
    if kind == "dd":
        entry = next((e for e in profile_items(profile or {}) if e["key"] == ident), None)
        return {"label": entry["title"] if entry else ident, "group": "Deep dive", "href": f"/interview-prep/deep-dives#{ident}"}
    return {"label": key, "group": "", "href": "/interview-prep"}
