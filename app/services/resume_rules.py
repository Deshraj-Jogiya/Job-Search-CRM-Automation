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
    require(pv, "select_by_jd", bool, required=False)
    require(pv, "variants", dict)

    wa = require(data, "work_authorization", dict)
    require(wa, "include_work_auth_line", bool)
    require(wa, "work_auth_text", str, required=False)

    cc = require(data, "certifications", dict)
    require(cc, "max_shown", int, min=1, required=False)
    require(cc, "priority", list, required=False)
    sec = cc.get("section")
    if sec is not None:
        if not isinstance(sec, dict):
            raise ConfigValidationError("certifications.section must be an object.")
        require(sec, "enabled", bool)
        require(sec, "title", str)
        require(sec, "max_entries", int, min=1)
        require(sec, "bullets_per_entry_max", int, min=0)
        require(sec, "core_terms", int, min=3, required=False)
        require(sec, "mention_max_entries", int, min=0, required=False)
        require(sec, "additional_max_entries", int, min=0, required=False)
        require(sec, "additional_min_relevance", float, min=0.0, required=False)
        for k in ("min_item_gain", "pointer_min_total_gain", "strong_coverage", "mention_min_relevance", "pointer_fill_relevance"):
            require(sec, k, float, min=0.0, required=False)

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
    """The entry's date range, kept clean (no free text appended) so
    parse_date_range/total_experience_months keep getting a parseable
    string -- appending text directly into "date" once silently broke
    the years-claim math for every entry it touched."""
    return entry.get("date", "")


def display_company(entry: dict) -> str:
    """The company name with its employment_type in parentheses, e.g.
    "Technoid LLC (Contract)", placed after the employer so a reader spots
    at a glance whether each position was contract, part-time, full-time,
    or another type."""
    company = entry.get("company", "")
    employment_type = entry.get("employment_type")
    return f"{company} ({employment_type})" if company and employment_type else company


def earlier_line(entry: dict, suffix: str = "") -> str:
    """One-line summary of an older role, e.g. "Earlier: Data Analyst,
    Kronic Keys (Full-Time) | Junagadh, India | Aug 2021 - Mar 2022".
    Pipe separators keep the employment-type parentheses from nesting
    inside a second set around the location and dates."""
    bits = [b for b in (entry.get("location"), display_date_range(entry)) if b]
    tail = "".join(f" | {b}" for b in bits)
    return f"Earlier: {entry.get('role', '')}, {display_company(entry)}{tail}{suffix}"


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


# Empty intensifiers an ATS resume checker flags as filler ("a real PySpark
# job", "an actual pipeline"). Removed outright, except where the word is
# part of a genuine term: "real-time" (hyphen, so \s+ never matches it),
# "real time", "real world", "real estate", "real numbers".
_FILLER_ADJECTIVE_RE = re.compile(
    r"\b(?:real|actual|genuine|genuinely|truly)\s+(?!time\b|world\b|estate\b|numbers?\b)",
    re.IGNORECASE,
)


def check_ai_cliche_language(bullet_text: str) -> list[str]:
    """Which cliche words (lowercased) appear in bullet_text -- used only
    to log what rewrite_ai_cliche_language below is about to change."""
    hits = [m.group(0).lower() for m in _AI_CLICHE_RE.finditer(bullet_text or "")]
    hits += [m.group(0).strip().lower() for m in _FILLER_ADJECTIVE_RE.finditer(bullet_text or "")]
    return hits


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

    text = _AI_CLICHE_RE.sub(_replace, bullet_text or "")
    text = _FILLER_ADJECTIVE_RE.sub("", text)
    return text[:1].upper() + text[1:] if bullet_text and bullet_text[:1].isupper() else text


# ---------------------------------------------------------------------------
# C11 -- quantified results first
# ---------------------------------------------------------------------------
# ATS resume checkers flag bullets that name no figure ("Lack of
# Accomplishment"). This never adds a number -- inventing one would be
# fabrication -- it only puts bullets that already carry a figure ahead of
# those that don't, so the strongest evidence leads and the page-fit
# reduction (which drops an entry's LAST bullet) removes an unquantified
# bullet before a quantified one.
def has_quantified_result(bullet_text: str) -> bool:
    return bool(re.search(r"\d", bullet_text or ""))


def quantified_first(bullets: list[str]) -> list[str]:
    return sorted(bullets, key=lambda b: 0 if has_quantified_result(b) else 1)


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
# C6b -- project and bullet selection by job description
#
# Real gap found 2026-10-10 (Deshraj, looking at three resumes for different
# roles): every resume used the same three projects, because the only
# selection was a fixed list per profile variant -- a data-platform role and an
# HR-systems role got the same projects. Selection now ranks EVERY real project
# in the profile against the job description and takes the best matches, so a
# role about integrations and testing surfaces the integration and testing work.
# Pure keyword matching, no LLM, so it costs nothing and is reproducible; it
# only ever reorders and picks real projects and real bullets, never writes any.
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset("""
a about above after again all also an and any are as at be because been before being below between both but by can
could did do does doing down during each few for from further had has have having he her here hers him his how i if
in into is it its itself just me more most my no nor not now of off on once only or other our out over own same she
should so some such than that the their them then there these they this those through to too under until up us very
was we were what when where which while who whom why will with within without would you your yours
experience experienced work working works role roles team teams ability strong skills skill preferred required
requirements responsibilities responsibility including include includes etc using use used years year plus well
build building built develop developing development design designing designed support supporting provide providing
ensure ensuring across various related new new
""".split())

_JD_BLEND = 0.7  # share of a project's rank that comes from job-description overlap (the rest: profile order)

_TOKEN_RE = re.compile(r"[a-z][a-z0-9+#.\-]*[a-z0-9+#]|[a-z]{2}")


def _tokens(text: str) -> list[str]:
    out = []
    for raw in _TOKEN_RE.findall((text or "").lower()):
        token = raw.strip(".-")
        if len(token) >= 2 and token not in _STOPWORDS:
            out.append(token)
    return out


def _entry_text(entry: dict) -> str:
    parts = [entry.get("name", ""), " ".join(entry.get("technologies") or []), " ".join(entry.get("bullets") or [])]
    return " ".join(parts)


def _jd_weights(jd_text: str, corpus_texts: list[str]) -> dict[str, float]:
    """Weight of each JD term: how often the JD uses it, scaled down for terms that appear in nearly every
    project (a word like "python" says little about which project fits this particular job)."""
    import math
    from collections import Counter

    jd_counts = Counter(_tokens(jd_text))
    corpus = [set(_tokens(t)) for t in corpus_texts] or [set()]
    n = len(corpus)
    weights = {}
    for term, tf in jd_counts.items():
        df = sum(1 for doc in corpus if term in doc)
        idf = math.log((n + 1) / (df + 1)) + 1.0
        weights[term] = (1.0 + math.log(tf)) * idf
    return weights


def _score_text(text: str, weights: dict[str, float]) -> float:
    import math

    tokens = _tokens(text)
    if not tokens:
        return 0.0
    hit = sum(weights.get(t, 0.0) for t in set(tokens))
    return hit / math.sqrt(len(set(tokens)))


def rank_projects_for_jd(projects: list[dict], jd_text: str) -> list[dict]:
    """Every real project, best match for this job description first. Ties keep the profile's own order
    (which lists the strongest, most recent work first)."""
    weights = _jd_weights(jd_text, [_entry_text(p) for p in projects])
    raw = [_score_text(_entry_text(p), weights) for p in projects]
    top = max(raw) if raw and max(raw) > 0 else 1.0
    n = len(projects)
    scored = []
    for i, p in enumerate(projects):
        # Keyword overlap alone is crude (a project that repeats "AI/ML" can outrank stronger work), so it is
        # blended with the profile's own ordering, which lists the strongest work first.
        prior = 1.0 - i / (n - 1) if n > 1 else 1.0
        scored.append((-(_JD_BLEND * raw[i] / top + (1 - _JD_BLEND) * prior), i, p))
    return [p for _, _, p in sorted(scored, key=lambda t: (t[0], t[1]))]


def rank_bullets_for_jd(bullets: list[str], jd_text: str, corpus: list[str] | None = None) -> list[str]:
    """The same bullets, best match for this job description first (ties keep their original order)."""
    weights = _jd_weights(jd_text, corpus or bullets)
    scored = [(-_score_text(b, weights), i, b) for i, b in enumerate(bullets)]
    return [b for _, _, b in sorted(scored, key=lambda t: (t[0], t[1]))]


def select_projects_for_jd(projects: list[dict], jd_text: str, config: dict | None = None, limit: int | None = None) -> list[dict]:
    """The best-matching real projects for this job description, capped at config max_projects (or limit)."""
    config = config or get_config()
    cap = limit if limit is not None else config["projects_by_variant"]["max_projects"]
    return rank_projects_for_jd(projects, jd_text)[:cap]


# ---------------------------------------------------------------------------
# C8 -- work authorization
# ---------------------------------------------------------------------------

def contact_info_parts(contact: dict) -> list[str]:
    """The header's non-link details in one fixed order: location (with the ZIP inside it, before any parenthetical), phone, email."""
    contact = contact or {}
    location = str(contact.get("location") or "").strip()
    zip_code = str(contact.get("zip") or "").strip()
    if location and zip_code and zip_code not in location:
        head, sep, tail = location.partition(" (")
        location = f"{head} {zip_code}" + (f" ({tail}" if sep else "")
    elif zip_code and not location:
        location = zip_code
    return [v for v in (location, str(contact.get("phone") or "").strip(), str(contact.get("email") or "").strip()) if v]


def contact_link_values(contact: dict) -> list[str]:
    return [str(v) for v in (contact or {}).values() if v and str(v).startswith("http")]


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

def select_certifications_for_resume(certifications: list, config: dict | None = None) -> list:
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

    def _priority_rank(cert) -> int:
        cert_lower = certification_display(cert).lower()
        for i, key in enumerate(priority):
            if key.lower() in cert_lower:
                return i
        return len(priority)

    ordered = [
        cert for _, cert in sorted(enumerate(certifications), key=lambda pair: (_priority_rank(pair[1]), pair[0]))
    ]
    return ordered[:max_shown] if max_shown else ordered


# ---------------------------------------------------------------------------
# Certifications & Training section (rule for every resume)
# ---------------------------------------------------------------------------
# A certification in the profile is either a plain string ("Name - Mon YYYY") or an object:
#   {"name", "issuer", "status": "Completed" | "In progress", "date", "bullets": [..]}.
# Every resume gets a Certifications & Training section right after Professional Experience (config certifications.section).
# Entries show where the credential came from and whether it is finished, so in-progress work is never read as completed, and each entry
# carries pointers (what the programme covered) ranked against the job description. Pointers always come from the profile; nothing is generated.

_CERT_DATE_RE = re.compile(r"\s+[\u2013\u2014-]\s+((?:[A-Z][a-z]{2,8}\.? )?\d{4})\s*$")
CERT_STATUSES = ("Completed", "In progress")


def normalize_certification(cert) -> dict:
    """A structured certification entry from either storage form; legacy strings keep their trailing date and carry no issuer or pointers."""
    if isinstance(cert, dict):
        d = dict(cert)
        d["name"] = str(d.get("name") or "").strip()
        d["status"] = d.get("status") if d.get("status") in CERT_STATUSES else "Completed"
        d["bullets"] = [b for b in (d.get("bullets") or []) if isinstance(b, str) and b.strip()]
        d.setdefault("issuer", None)
        d.setdefault("date", None)
        return d
    text = str(cert or "").strip()
    m = _CERT_DATE_RE.search(text)
    return {"name": text[: m.start()].strip() if m else text, "issuer": None, "status": "Completed", "date": m.group(1) if m else None, "bullets": []}


def certification_display(cert) -> str:
    """One readable line, used on the Profile page, in matching and in interview prep: 'Name | Issuer | Status, date'."""
    e = normalize_certification(cert)
    tail = e["status"] + (f", {e['date']}" if e.get("date") and e["status"] == "Completed" else "")
    return " | ".join(b for b in (e["name"], e.get("issuer"), tail) if b) if isinstance(cert, dict) else (str(cert) if cert else "")


def certification_mention_line(entry: dict) -> str:
    """One compact line for a credential that is only mentioned: 'Name (Issuer), Mon YYYY' or 'Name (Issuer), in progress'. Course counts and hours stay on the pointer view."""
    e = normalize_certification(entry)
    issuer = (e.get("issuer") or "").split("|")[0].strip()
    tail = "in progress" if e["status"] == "In progress" else (e.get("date") or "")
    head = f"{e['name']} ({issuer})" if issuer else e["name"]
    return f"{head}, {tail}" if tail else head


def certification_section_config(config: dict | None = None) -> dict | None:
    sec = ((config or get_config()).get("certifications") or {}).get("section")
    return sec if sec and sec.get("enabled") else None


_BOILERPLATE = frozenset("""please visit apply global equal opportunity employer benefits join mission values culture inclusion diversity
 environment looking seeking ideal candidate day apply today location remote hybrid onsite full-time part-time salary range compensation""".split())


def certification_core_terms(jd_text: str, company_name: str = "", limit: int = 15) -> dict[str, float]:
    """The job's most important subject terms, weighted by how often the posting uses them. Company-name words and recruiting boilerplate are left out."""
    import math
    from collections import Counter

    skip = set(_tokens(company_name))
    counts = Counter(t for t in _tokens(jd_text) if t not in _BOILERPLATE and t not in skip and len(t) >= 2)
    weights = {t: 1.0 + math.log(n) for t, n in counts.items()}
    return dict(sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))[:limit])


def resume_evidence_terms(resume_doc: dict) -> set[str]:
    """What the experience and projects already show: their role titles, project names and technologies, and bullets."""
    parts = []
    for e in resume_doc.get("experience") or []:
        parts += [e.get("role", ""), e.get("company", "")] + list(e.get("bullets") or [])
    for pr in resume_doc.get("projects") or []:
        parts += [pr.get("name", "")] + list(pr.get("technologies") or []) + list(pr.get("bullets") or [])
    return set(_tokens(" ".join(parts)))


_CERT_GENERIC_TOKENS = frozenset("""professional certificate certification completion specialization bootcamp resources coursera learning partner training program course
 courses mastermind world record""".split())


def additional_certification_names(entries: list[dict], shown_names: set[str], core: dict[str, float], limit: int, min_relevance: float = 0.04) -> list[str]:
    """Credentials worth mentioning that did not fit the section, as short names for a one-line 'Additional certificates:' entry (like the
    'Earlier:' lines under experience). Only credentials whose own subject (name and issuer) touches the job's core terms are listed: an unrelated
    credential never fills the line just because there is room. Finished credentials come first, then in-progress ones; each group is ordered by relevance."""
    total = sum(core.values()) or 1.0

    def rel(e):
        terms = set(_tokens(" ".join([e["name"], e.get("issuer") or ""]))) - _CERT_GENERIC_TOKENS
        return sum(w for t, w in core.items() if t in terms) / total

    rest = [(i, e, rel(e)) for i, e in enumerate(entries) if e["name"] not in shown_names]
    rest = [r for r in rest if r[2] >= min_relevance]
    rest.sort(key=lambda r: (r[1]["status"] != "Completed", -r[2], r[0]))
    return [e["name"] + (" (in progress)" if e["status"] == "In progress" else "") for _, e, _r in rest[:limit]]


def plan_certification_section(certifications: list, jd_text: str, resume_doc: dict, config: dict | None = None, company_name: str = "") -> dict:
    """Decides how much room the certifications get on THIS resume, from the job and the resume's overall fit.

    Strong experience and projects come first. Pointers (what a programme covered) are added only when they would make the resume stronger: when the
    certifications cover core terms of the job that the experience and projects do not, and the resume does not already cover the core of the job.
    Otherwise the credentials are only mentioned in a compact list at the end (name, issuer, status), and only those that relate to the job or are
    configured highlights. Never invents anything: pointers are the profile's own text.

    Returns {"mode": "pointers" | "mention" | "none", "entries": [...], "lines": [...], "coverage": float, "total_gain": float, "reason": str}."""
    config = config or get_config()
    cfg = (config.get("certifications") or {})
    sec = certification_section_config(config)
    # A course that is part of a larger credential ("component_of") stays in the profile but is never listed on its own: the parent credential covers it.
    entries = [e for e in (normalize_certification(c) for c in (certifications or [])) if e["name"] and not e.get("component_of")]
    plan = {"mode": "none", "entries": [], "lines": [], "additional": [], "coverage": None, "total_gain": 0.0, "reason": ""}
    if not sec or not entries:
        plan["reason"] = "section disabled or no certifications in the profile"
        return plan

    core = certification_core_terms(jd_text, company_name, sec.get("core_terms", 15))
    evidence = resume_evidence_terms(resume_doc)
    total = sum(core.values()) or 1.0
    plan["coverage"] = round(sum(w for t, w in core.items() if t in evidence) / total, 3)
    cert_terms = [set(_tokens(" ".join([e["name"], e.get("issuer") or ""] + e["bullets"]))) for e in entries]
    priority = cfg.get("priority") or []

    def is_priority(e):
        low = (e["name"] + " " + (e.get("issuer") or "")).lower()
        return any(k.lower() in low for k in priority)

    # Terms of the job that only a certification can add: in the core, not shown by experience or projects.
    missing = {t: w for t, w in core.items() if t not in evidence}
    chosen, covered = [], set()
    subject_terms = [set(_tokens(" ".join([e["name"], e.get("issuer") or ""]))) - _CERT_GENERIC_TOKENS for e in entries]

    def subject_ok(i):
        """A credential is only ever shown (pointers, list, or additional line) when its own subject touches the job: bullet words alone never qualify it."""
        return sum(w for t, w in core.items() if t in subject_terms[i]) / total >= sec.get("additional_min_relevance", 0.04)

    pool = [i for i, e in enumerate(entries) if e["bullets"] and cert_terms[i] & set(missing) and subject_ok(i)]  # pointers need pointers to give
    while pool and len(chosen) < sec["max_entries"]:
        gains = {i: sum(w for t, w in missing.items() if t in cert_terms[i] and t not in covered) / total for i in pool}
        best = max(pool, key=lambda i: (gains[i], -i))
        if gains[best] < sec.get("min_item_gain", 0.03):
            break
        chosen.append(best)
        covered |= cert_terms[best] & set(missing)
        pool.remove(best)
    plan["total_gain"] = round(sum(w for t, w in missing.items() if t in covered) / total, 3)
    gate_open = bool(chosen) and plan["total_gain"] >= sec.get("pointer_min_total_gain", 0.10) and plan["coverage"] < sec.get("strong_coverage", 0.60)
    if gate_open:
        # Once the gate is open, credentials that clearly match the job (even where experience already shows the same words) fill the remaining slots.
        def rel(i):
            return sum(w for t, w in core.items() if t in cert_terms[i]) / total
        extra = sorted((i for i, e in enumerate(entries) if e["bullets"] and i not in chosen and subject_ok(i) and rel(i) >= sec.get("pointer_fill_relevance", 0.20)), key=lambda i: (-rel(i), i))
        chosen += extra[: sec["max_entries"] - len(chosen)]

    if gate_open:
        order = sorted(chosen, key=lambda i: (entries[i]["status"] != "Completed", chosen.index(i)))  # finished credentials first, then by gain
        for i in order:
            e = entries[i]
            focus = {t: w for t, w in missing.items() if t in cert_terms[i]}
            ranked = sorted(e["bullets"], key=lambda b: -sum(w for t, w in focus.items() if t in set(_tokens(b))))
            status = e["status"] + (f", {e['date']}" if e.get("date") and e["status"] == "Completed" else "")
            plan["entries"].append({"name": e["name"], "issuer": e.get("issuer") or "", "status": status, "bullets": ranked[: sec["bullets_per_entry_max"]]})
        plan["mode"] = "pointers"
        plan["additional"] = additional_certification_names(entries, {e["name"] for e in plan["entries"]}, core, sec.get("additional_max_entries", 6), sec.get("additional_min_relevance", 0.04))
        plan["reason"] = (f"experience and projects cover {plan['coverage']:.0%} of the job's core terms and these credentials add {plan['total_gain']:.0%} more, "
                          "so their pointers strengthen the resume")
        return plan

    # Otherwise: mention only. Configured highlights come first, in the configured order (the candidate's own ranking), then credentials that clearly match
    # the job (at least mention_min_relevance of its core weight); weak lexical overlap with generic words is not enough to show one.
    def relevance(i):
        return sum(w for t, w in core.items() if t in cert_terms[i]) / total

    def priority_index(e):
        low = (e["name"] + " " + (e.get("issuer") or "")).lower()
        return next((k for k, key in enumerate(priority) if key.lower() in low), None)

    highlights = sorted((i for i, e in enumerate(entries) if priority_index(e) is not None and subject_ok(i)), key=lambda i: (priority_index(entries[i]), i))
    related = sorted((i for i in range(len(entries)) if i not in highlights and subject_ok(i) and relevance(i) >= sec.get("mention_min_relevance", 0.25)), key=lambda i: (-relevance(i), i))
    for i in (highlights + related)[: sec.get("mention_max_entries", 3)]:
        plan["lines"].append(certification_mention_line(entries[i]))
    shown_names = {entries[i]["name"] for i in (highlights + related)[: sec.get("mention_max_entries", 3)]}
    plan["additional"] = additional_certification_names(entries, shown_names, core, sec.get("additional_max_entries", 6), sec.get("additional_min_relevance", 0.04))
    plan["mode"] = "mention" if plan["lines"] else "none"
    plan["reason"] = ("experience and projects already carry the job (" + f"{plan['coverage']:.0%} of core terms); credentials are listed without pointers"
                      if plan["coverage"] >= sec.get("strong_coverage", 0.60) else
                      "no credential adds core terms of the job beyond what experience and projects show; credentials are listed without pointers")
    return plan
