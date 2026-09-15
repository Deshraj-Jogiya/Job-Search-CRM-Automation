"""
Part C: declarative resume-generation rules, consumed by the EXISTING
tailoring pipeline (tailoring_service.py) and the existing profile-
variant system (profile_service.py) -- this module has no generator of
its own. Every threshold/list here comes from config/resume_rules.yaml
(app/config_loader.py), validated on load, hot-reloadable, never
hardcoded.

C1: real total-experience-months math (UNION of date ranges, overlaps
counted once), never a bare "N+ years" derived from elapsed calendar
time.
C2: classifies each experience entry as EXPERIENCE / EARLIER /
CREDENTIAL based on config thresholds, and flags concurrent overlaps.
C3: drops any skill that doesn't appear in an actual bullet/summary
(unsupported-skill filtering, same "only claim what's backed by real
evidence" posture as tailoring_service.py's existing fabrication
safeguard).
C4: hedges any percentage/multiplier not in the config's verified
allowlist ("roughly"/"approximately"/"about"/"~"); exact scope figures
(counts, volumes, uptime) are never hedged.
C6: picks the right project set per profile variant from config.
C9: mechanically rewrites generic AI-sounding filler words ("leveraged",
"spearheaded", "utilized", "seamless", "synergy", ...) to the plainest
accurate synonym -- a style fix, never flagged for manual review.
C10: flags a bullet opening with a weak, passive phrase ("Responsible
for", "Worked on", "Helped with", "Duties included", ...) instead of a
real action verb -- a soft note, not auto-rewritten (unlike C9, there's
no mechanically safe rewrite: doing so would mean inventing what the
real action verb should have been).
C8: work-authorization text is never generated or inferred -- config
says whether to include a line at all, and supplies the exact text
verbatim if so.
"""

import re
from datetime import date

from ..config_loader import ConfigValidationError, HotReloadableYaml, require

_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_CREDENTIAL_KEYWORDS = ("fellowship", "bootcamp", "boot camp", "certificate program", "training program")

_YEARS_CLAIM_RE = re.compile(r"\b(\d+)\+?\s*years?\b", re.IGNORECASE)
# Leading +/- is part of the match (not just \b\d+) so a real signed
# figure like "(-35% silent drift)" hedges as "roughly -35%", not
# "-roughly 35%" -- found via a real bullet in the golden fixture pull
# (deshraj_source_profile.json), see git log for this line.
_PERCENT_RE = re.compile(r"(?<!\w)[-+]?\d+(?:\.\d+)?\s*%|(?<!\w)[-+]?\d+(?:\.\d+)?x\b", re.IGNORECASE)


def _validate(data: dict) -> None:
    ec = require(data, "experience_classification", dict)
    require(ec, "experience_min_months", int, min=0)
    require(ec, "experience_recency_months", int, min=1)
    require(ec, "concurrent_overlap_days", int, min=0)

    sk = require(data, "skills", dict)
    require(sk, "max_skill_items_total", int, min=1)
    require(sk, "max_skill_lines", int, min=1)
    require(sk, "groups", list)

    m = require(data, "metrics", dict)
    require(m, "max_bare_percentages_per_page", int, min=0)
    require(m, "verified_metrics", list)

    pv = require(data, "projects_by_variant", dict)
    require(pv, "max_projects", int, min=0)
    require(pv, "bullets_per_project_min", int, min=0)
    require(pv, "bullets_per_project_max", int, min=0)
    require(pv, "variants", dict)

    wa = require(data, "work_authorization", dict)
    require(wa, "include_work_auth_line", bool)
    require(wa, "work_auth_text", str, required=False)

    cc = require(data, "certifications", dict)
    require(cc, "max_shown", int, min=1, required=False)
    require(cc, "priority", list, required=False)

    pf = require(data, "page_fit", dict)
    require(pf, "min_body_font_pt", float, min=1.0)
    require(pf, "min_margin_in", float, min=0.0)
    require(pf, "max_pages", int, min=1)
    require(pf, "line_spacing_step_pt", float, min=0.01)
    require(pf, "margin_step_in", float, min=0.01)
    require(pf, "font_step_pt", float, min=0.01)
    require(pf, "summary_shorten_max_chars", int, min=20)
    require(pf, "reduction_catalog", list)
    for entry in pf["reduction_catalog"]:
        if not isinstance(entry, dict):
            raise ConfigValidationError(f"page_fit.reduction_catalog entries must be objects, got {entry!r}.")
        require(entry, "id", str)
        require(entry, "content_value_weight", int, min=1)


_STORE = HotReloadableYaml("resume_rules.yaml", validate_fn=_validate)


def get_config() -> dict:
    return _STORE.get()


# ---------------------------------------------------------------------------
# C1 -- real experience-months math
# ---------------------------------------------------------------------------

def _parse_single_date(text: str) -> date | None:
    text = text.strip().rstrip(".")
    match = re.match(r"^([A-Za-z]{3,9})\.?\s+(\d{4})$", text)
    if match:
        month = _MONTH_NAMES.get(match.group(1)[:3].lower())
        if month:
            return date(int(match.group(2)), month, 1)
        return None
    match = re.match(r"^(\d{4})$", text)
    if match:
        return date(int(match.group(1)), 1, 1)
    return None


def parse_date_range(date_str: str, now: date | None = None) -> tuple[date, date] | None:
    """Parses "Jun 2020 - Dec 2021", "2022 - Present", "2022 - 2023" --
    each endpoint to the first of its month. Returns None (never a
    guessed fallback) for anything else, so callers can tell "genuinely
    no experience" apart from "couldn't parse this one" and handle the
    two differently -- see total_experience_months."""
    now = now or date.today()
    if not date_str:
        return None
    parts = re.split(r"\s*[-–—]\s*", date_str.strip())
    if len(parts) != 2:
        return None
    start = _parse_single_date(parts[0])
    end_raw = parts[1].strip()
    end = now.replace(day=1) if end_raw.lower() == "present" else _parse_single_date(end_raw)
    if start is None or end is None:
        return None
    return (start, end)


def display_date_range(entry: dict) -> str:
    """The entry's date range plus its employment_type ("Contract",
    "Part-Time"), if set, as one display string ("May 2026 - Present
    - Part-Time") -- kept separate from the raw "date" field itself so
    parse_date_range/total_experience_months/classify_role keep
    getting a clean, parseable string. Appending free text directly
    into "date" (an earlier, real mistake here) silently broke
    parsing for every entry it touched -- total_experience_months
    excluded them entirely rather than erroring, so the years-claim
    math went quietly wrong with no visible failure."""
    date_str = entry.get("date", "")
    employment_type = entry.get("employment_type")
    if not employment_type:
        return date_str
    return f"{date_str} · {employment_type}"


def _month_index(d: date) -> int:
    return d.year * 12 + (d.month - 1)


def total_experience_months(experience: list[dict], now: date | None = None) -> int:
    """UNION of employment date ranges -- overlapping/concurrent roles
    counted once, never summed. An entry whose date field can't be
    parsed is EXCLUDED from the total, not assumed to be zero-length
    or estimated any other way -- see parse_date_range.

    A stated end month counts as fully worked (resume convention: "Jan
    2020 - Dec 2022" reads as 3 full years/36 months, not 35) -- each
    interval is treated as half-open [start, end+1) in month-index
    space so the merge/sum below comes out inclusive of the end month."""
    now = now or date.today()
    intervals = []
    for entry in experience:
        parsed = parse_date_range(entry.get("date", ""), now=now)
        if parsed:
            intervals.append((_month_index(parsed[0]), _month_index(parsed[1]) + 1))
    if not intervals:
        return 0

    intervals.sort()
    merged = [list(intervals[0])]
    for start, end in intervals[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return sum(end - start for start, end in merged)


def check_years_claim(summary_text: str, total_months: int) -> list[str]:
    """Returns the list of years-figures in summary_text that exceed
    round(total_months/12) -- empty if none. Pure detection, no
    rewriting: tailoring_service.py's existing attention_reason/Needs-
    Review flow is what surfaces a violation, same mechanism as every
    other fabrication check there (see D1 wiring).

    Rounds to the nearest year rather than flooring -- a floor
    previously flagged a real, conventional claim as fabrication: 35
    real months is 2.9166 years, and "3+ years" is how anyone actually
    phrases that on a resume, not "2 years". Flooring made that
    genuine, standard phrasing fail the same check meant to catch
    someone claiming, say, "5+ years" off of 35 months. The user
    confirmed this directly after seeing the false flag -- "no need to
    compute 3+ is goo[d] and shouldn't be flagged" -- round() keeps the
    check's real purpose (catching a claim meaningfully beyond the real
    total) without punishing ordinary rounding."""
    max_years = round(total_months / 12)
    violations = []
    for match in _YEARS_CLAIM_RE.finditer(summary_text or ""):
        claimed = int(match.group(1))
        if claimed > max_years:
            violations.append(match.group(0))
    return violations


# ---------------------------------------------------------------------------
# C2 -- role classification
# ---------------------------------------------------------------------------

def is_credential_entry(entry: dict) -> bool:
    """An explicit entry_type field always wins if the user set one;
    otherwise falls back to a narrow keyword match on role/company --
    a heuristic, not a claim of certainty, which is exactly why the
    explicit field exists as an override."""
    entry_type = (entry.get("entry_type") or "").strip().lower()
    if entry_type:
        return entry_type == "credential"
    text = f"{entry.get('role', '')} {entry.get('company', '')}".lower()
    return any(kw in text for kw in _CREDENTIAL_KEYWORDS)


def classify_role(entry: dict, config: dict | None = None, now: date | None = None) -> str:
    """Returns "CREDENTIAL", "EXPERIENCE", or "EARLIER" for one
    experience entry. CREDENTIAL is checked first regardless of
    duration -- a fellowship stays a fellowship even if it ran long.

    A currently-held role (date range ends in "Present") is always
    EXPERIENCE regardless of how short its tenure is so far -- a role
    you're in right now is never "Earlier:" material just because it's
    new. Only past roles get held to the min-months/recency bars."""
    config = config or get_config()
    if is_credential_entry(entry):
        return "CREDENTIAL"

    ec = config["experience_classification"]
    now = now or date.today()
    date_str = entry.get("date", "")
    parsed = parse_date_range(date_str, now=now)
    if parsed is None:
        return "EXPERIENCE"  # can't verify duration/recency -- don't downgrade on a parse failure

    is_current = date_str.strip().lower().endswith("present")
    if is_current:
        return "EXPERIENCE"

    start, end = parsed
    months = _month_index(end) - _month_index(start) + 1  # inclusive of the end month, see total_experience_months
    recency_months = _month_index(now.replace(day=1)) - _month_index(end)

    if months < ec["experience_min_months"] or recency_months > ec["experience_recency_months"]:
        return "EARLIER"
    return "EXPERIENCE"


def detect_concurrent_overlaps(experience: list[dict], config: dict | None = None, now: date | None = None) -> set[int]:
    """Returns the set of experience-list INDEXES that should get
    "(concurrent)" appended -- the later-listed one of any pair whose
    real date ranges overlap by more than concurrent_overlap_days."""
    config = config or get_config()
    max_gap_days = config["experience_classification"]["concurrent_overlap_days"]
    now = now or date.today()

    parsed = []
    for i, entry in enumerate(experience):
        r = parse_date_range(entry.get("date", ""), now=now)
        parsed.append((i, r))

    concurrent = set()
    for a in range(len(parsed)):
        i_a, range_a = parsed[a]
        if range_a is None:
            continue
        for b in range(a + 1, len(parsed)):
            i_b, range_b = parsed[b]
            if range_b is None:
                continue
            overlap_days = (min(range_a[1], range_b[1]) - max(range_a[0], range_b[0])).days
            if overlap_days > max_gap_days:
                # "later-listed" = whichever entry comes second in the
                # profile's own ordering (index), not whichever date is
                # more recent -- matches the rule's own wording.
                concurrent.add(max(i_a, i_b))
    return concurrent


# ---------------------------------------------------------------------------
# C3 -- skills filtering
# ---------------------------------------------------------------------------

def filter_skills(skills: dict, experience: list[dict], projects: list[dict], summary: str, config: dict | None = None) -> tuple[dict, list[str]]:
    """A skill renders only if it also appears in an experience bullet,
    a project bullet, or the summary -- everything else is dropped.
    Returns (filtered_skills, dropped_reasons) -- dropped_reasons is
    always populated when something is cut, never a silent removal.

    Also enforces config's max_skill_items_total (a real global cap on
    the TOTAL skill count across every category) -- found genuinely
    unenforced anywhere: validated as real config at load time, but
    never actually consumed. Real cost: a real evidence-backed resume
    landed at 86 total skills (config's own cap says 32), since neither
    renderer capped by total item count -- docx_generator.py only caps
    by category LINES (max_skill_lines), which still lets one category
    with 21 items count as a single "line", and the PDF renderer
    (document_render_service.py, the format autofill actually attaches)
    enforced no cap at all. Enforced ONCE here rather than per-renderer
    so both inherit the same real limit for free, matching this
    function's own existing single-source-of-truth role (see its call
    site's own comment in tailoring_service.py).

    Truncation is by category priority (config's skills.groups order,
    the same priority order docx_generator.py already uses to decide
    which categories matter most) then by each category's own existing
    item order -- never random, and every cut item is recorded in
    dropped_reasons with its own distinct reason so a length-based cut
    is never confused with "not found in any bullet"."""
    config = config or get_config()
    haystack = summary or ""
    for entry in experience:
        haystack += " " + " ".join(entry.get("bullets", []))
    for project in projects:
        haystack += " " + " ".join(project.get("bullets", []))
    haystack = haystack.lower()

    evidence_backed: dict[str, list[str]] = {}
    dropped: list[str] = []
    for category, items in (skills or {}).items():
        kept = [item for item in items if item.lower() in haystack]
        for item in items:
            if item not in kept:
                dropped.append(f"{item} (not found in any bullet or the summary)")
        if kept:
            evidence_backed[category] = kept

    max_total = config["skills"]["max_skill_items_total"]
    priority_order = list(config["skills"]["groups"])
    ordered_categories = priority_order + [c for c in evidence_backed if c not in priority_order]

    filtered: dict[str, list[str]] = {}
    remaining = max_total
    for category in ordered_categories:
        items = evidence_backed.get(category)
        if not items:
            continue
        if remaining <= 0:
            dropped.extend(f"{item} (cut for length -- over the {max_total}-skill total cap)" for item in items)
            continue
        keep, cut = items[:remaining], items[remaining:]
        filtered[category] = keep
        remaining -= len(keep)
        dropped.extend(f"{item} (cut for length -- over the {max_total}-skill total cap)" for item in cut)

    return filtered, dropped


# ---------------------------------------------------------------------------
# C4 -- metric phrasing
# ---------------------------------------------------------------------------

_HEDGE_WORDS = ("roughly", "approximately", "about", "~")


def _is_hedged(bullet_text: str, match_start: int) -> bool:
    preceding = bullet_text[max(0, match_start - 20):match_start].lower()
    return any(hedge in preceding for hedge in _HEDGE_WORDS)


def hedge_unverified_metrics(bullet_text: str, config: dict | None = None) -> str:
    """Any percentage/multiplier in bullet_text not in the config's
    verified_metrics allowlist gets "roughly " prefixed if it isn't
    already hedged. Plain counts/volumes (e.g. "500GB/day", "12 sources")
    are untouched -- only %/x-multiplier claims are in scope, per C4."""
    config = config or get_config()
    verified = set(config["metrics"]["verified_metrics"])

    def _replace(match: re.Match) -> str:
        claim = match.group(0)
        if claim in verified or _is_hedged(bullet_text, match.start()):
            return claim
        return f"roughly {claim}"

    return _PERCENT_RE.sub(_replace, bullet_text)


def check_bare_percentage(bullet_text: str) -> list[str]:
    """Returns any %/multiplier claim in bullet_text NOT preceded by a
    hedge word, regardless of whether it's in the verified allowlist --
    used for the max_bare_percentages_per_page density count. For the
    D2 fabrication-style check (is this SPECIFIC bare claim actually
    verified), see check_unverified_bare_percentage below."""
    bare = []
    for match in _PERCENT_RE.finditer(bullet_text or ""):
        if not _is_hedged(bullet_text, match.start()):
            bare.append(match.group(0))
    return bare


def check_unverified_bare_percentage(bullet_text: str, config: dict | None = None) -> list[str]:
    """D2: a bare (unhedged) %/multiplier claim that ISN'T in the
    config's verified_metrics allowlist -- this is the real fabrication
    signal (an invented number stated as fact), distinct from
    check_bare_percentage's plain density count above."""
    config = config or get_config()
    verified = set(config["metrics"]["verified_metrics"])
    return [claim for claim in check_bare_percentage(bullet_text) if claim not in verified]


# ---------------------------------------------------------------------------
# C9 -- AI-cliche language
# ---------------------------------------------------------------------------
# Generic filler verbs/adjectives that read as AI-generated regardless of
# who wrote them -- mechanically rewritten to the plainest accurate word.
# Deliberately narrow: words that can carry genuine technical meaning in a
# resume bullet ("orchestrated" real workflow orchestration, "robust"
# error handling, "streamlined" a literal pipeline, "dynamic" programming)
# are left alone rather than risk rewriting away accurate language just to
# chase a style list. This is a tone fix, never surfaced for manual
# review -- same posture as C4's hedging above.
_AI_CLICHE_REPLACEMENTS = {
    "leveraged": "used", "leverages": "uses", "leverage": "use", "leveraging": "using",
    "spearheaded": "led", "spearheads": "leads", "spearhead": "lead", "spearheading": "leading",
    "utilized": "used", "utilised": "used", "utilizes": "uses", "utilises": "uses",
    "utilize": "use", "utilise": "use", "utilizing": "using", "utilising": "using",
    "seamlessly": "smoothly", "seamless": "smooth",
    "synergies": "collaboration", "synergy": "collaboration",
}

_AI_CLICHE_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in sorted(_AI_CLICHE_REPLACEMENTS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def check_ai_cliche_language(bullet_text: str) -> list[str]:
    """Which cliche words (lowercased) appear in bullet_text -- used only
    to log what rewrite_ai_cliche_language below is about to change."""
    return [m.group(0).lower() for m in _AI_CLICHE_RE.finditer(bullet_text or "")]


def rewrite_ai_cliche_language(bullet_text: str) -> str:
    """Mechanically swaps each matched cliche word for its plain synonym,
    preserving the original word's capitalization. Never changes meaning
    or claims -- vocabulary only."""

    def _replace(match: re.Match) -> str:
        original = match.group(0)
        replacement = _AI_CLICHE_REPLACEMENTS[original.lower()]
        if original.isupper():
            return replacement.upper()
        if original[0].isupper():
            return replacement.capitalize()
        return replacement

    return _AI_CLICHE_RE.sub(_replace, bullet_text or "")


# ---------------------------------------------------------------------------
# C10 -- weak/passive bullet openers
# ---------------------------------------------------------------------------
# Real, well-established resume-writing rule (same external resource that
# led to C9): a bullet should open with what the candidate actually DID,
# not a phrase describing the job/task in the passive voice. Unlike C9,
# there's no mechanically safe rewrite here -- "Responsible for owning X"
# could honestly become "Owned X" or a dozen other things depending on
# what the candidate actually did, and guessing would mean inventing
# content. So this is detect-only, surfaced as a soft, non-blocking note
# (same posture as D's self-deprecating-content check below) -- never
# auto-rewritten, never a hard stop.
_WEAK_OPENER_PATTERNS = (
    r"^responsible for\b",
    r"^was responsible for\b",
    r"^worked on\b",
    r"^helped with\b",
    r"^helped to\b",
    r"^assisted (in|with)\b",
    r"^duties included\b",
    r"^tasked with\b",
    r"^in charge of\b",
)
_WEAK_OPENER_RE = re.compile("|".join(_WEAK_OPENER_PATTERNS), re.IGNORECASE)


def check_weak_bullet_opener(bullet_text: str) -> str | None:
    """Returns the matched weak-opener phrase (lowercased) if bullet_text
    opens with one, else None. Only checks the opening of the bullet --
    "I was responsible for growth, which I drove by..." starting with a
    real action isn't what this flags; a bullet that LEADS with the weak
    phrase is."""
    match = _WEAK_OPENER_RE.match((bullet_text or "").strip())
    return match.group(0).lower() if match else None


# ---------------------------------------------------------------------------
# D (inverse check) -- volunteered self-deprecating content
# ---------------------------------------------------------------------------

_SELF_DEPRECATING_PATTERNS = (
    r"\bonly\s+\d+\s*(month|year)s?\b",
    r"\bjust a small part\b",
    r"\bmostly a team effort\b",
    r"\blimited experience with\b",
)


def check_self_deprecating_content(bullet_text: str) -> list[str]:
    """Flags volunteered self-deprecating phrasing the JD never asked
    about (e.g. "only 3 months", "just a small part") -- surfaced for
    human review via the same attention_reason mechanism as every other
    check here, NEVER auto-rewritten. A candidate genuinely choosing to
    say this about their own work is their call, not this app's to
    silently override; the point is making sure it was a deliberate
    choice, not something an LLM added unprompted."""
    lowered = bullet_text or ""
    hits = []
    for pattern in _SELF_DEPRECATING_PATTERNS:
        match = re.search(pattern, lowered, re.IGNORECASE)
        if match:
            hits.append(match.group(0))
    return hits


# ---------------------------------------------------------------------------
# C6 -- project selection per variant
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


# Real ProfileVariant.name values don't always slugify to the exact
# config/resume_rules.yaml variant key (e.g. "ML Engineering" ->
# "ml_engineering", but the config's own key is "ml_ai") -- narrow,
# explicit aliases for the known cases; anything else falls back to a
# plain slugify, which is correct whenever the variant name and the
# config key already match (e.g. "Data Engineering" -> "data_engineering").
_VARIANT_NAME_ALIASES = {
    "ml_engineering": "ml_ai",
    "machine_learning": "ml_ai",
    "machine_learning_engineering": "ml_ai",
    "ml_ai_engineering": "ml_ai",
}


def variant_slug_for_name(variant_name: str) -> str:
    """Maps a real ProfileVariant.name to the config's variant key --
    see _VARIANT_NAME_ALIASES for the known name/key mismatches."""
    slug = _slugify(variant_name or "")
    return _VARIANT_NAME_ALIASES.get(slug, slug)


def select_projects_for_variant(projects: list[dict], variant_slug: str, config: dict | None = None) -> list[dict]:
    """Returns the real project objects (from the candidate's actual
    profile) matching the config's ordered slug list for this variant
    -- never invents a project. A configured slug with no matching
    real project is simply skipped (not an error -- config can list
    aspirational ordering ahead of every project existing)."""
    config = config or get_config()
    pv = config["projects_by_variant"]
    slugs = pv["variants"].get(variant_slug, [])

    by_slug = {_slugify(p.get("name", "")): p for p in projects}
    selected = [by_slug[slug] for slug in slugs if slug in by_slug]
    return selected[: pv["max_projects"]]


# ---------------------------------------------------------------------------
# C8 -- work authorization
# ---------------------------------------------------------------------------

def work_authorization_line(config: dict | None = None) -> str | None:
    """Never generates or infers immigration-status text -- returns
    exactly what config says, or None if the config says not to
    include a line at all."""
    config = config or get_config()
    wa = config["work_authorization"]
    if not wa["include_work_auth_line"]:
        return None
    return wa.get("work_auth_text") or None


# ---------------------------------------------------------------------------
# C9 -- certification curation
# ---------------------------------------------------------------------------

def select_certifications_for_resume(certifications: list[str], config: dict | None = None) -> list[str]:
    """Curates the real certification list for a space-constrained resume
    -- never invents or alters one, only reorders/caps what's real. A
    3+ years candidate listing five certifications where three are
    beginner-level online-course completions reads as padding, not
    strength -- the signal-dense ones (a real, recognized credential body;
    a genuinely rare, verifiable differentiator) get buried at the same
    weight as generic ones just by list position otherwise.

    config['certifications']['priority'] is an ordered list of substrings
    matched case-insensitively against each real certification string;
    matches surface first in that priority order, non-matches keep their
    original relative order after. A priority entry with no matching real
    certification is simply skipped (config can list aspirational
    ordering ahead of every certification existing, same posture as
    select_projects_for_variant). Capped to max_shown if configured.
    Nothing here is ever hidden from the full profile/LinkedIn/portfolio
    -- this only curates what one space-constrained document shows."""
    config = config or get_config()
    cc = config.get("certifications") or {}
    priority = cc.get("priority") or []
    max_shown = cc.get("max_shown")

    if not priority and not max_shown:
        return certifications

    def _priority_rank(cert: str) -> int:
        cert_lower = cert.lower()
        for i, key in enumerate(priority):
            if key.lower() in cert_lower:
                return i
        return len(priority)

    ordered = [
        cert for _, cert in sorted(enumerate(certifications), key=lambda pair: (_priority_rank(pair[1]), pair[0]))
    ]
    return ordered[:max_shown] if max_shown else ordered
