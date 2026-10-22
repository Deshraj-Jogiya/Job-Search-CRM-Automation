"""
Rule-based fit between one profile and one job posting. No LLM, no network.

Two jobs:

1. A fit score anyone can check. It finds the technical requirements a job
   description names, looks each one up in the profile (skills, experience
   bullets, projects) and records where it was found. The score is the
   weighted share of requirements met, combined with whether the posting's
   role type is one the profile targets. Used when no LLM is configured (the
   public demo, or a fork without an API key) and shown alongside the AI
   score otherwise, so a number never stands without its evidence.

2. A role-type guard on the AI score. An LLM asked "how well does this
   resume match" can rate an SDE resume 88% for a Data Analyst posting
   because the tools overlap. When the posting's role type is not one the
   profile targets or has held, the fit score is capped (ROLE_MISMATCH_CAP)
   and the reason is recorded. The cap is a fixed rule, so the model cannot
   talk its way past it, and it applies before any automatic step (fast
   track, auto-tailor, autofill launch) reads the score.

Role types are classified from titles only, by plain phrase lists. A title
that matches none of them is "unclassified" and never capped: a guard that
fires on titles it does not understand would punish real matches.
"""

import re

from .resume_rules import total_experience_months

ROLE_MISMATCH_CAP = 40

# Checked in order; the first family with a matching phrase wins, so the more
# specific phrases come first ("analytics engineer" before "analyst").
ROLE_FAMILIES = [
    ("ml_ai", "Machine learning / AI engineering", [
        "machine learning", "ml engineer", "mlops", "ml ops", "ai engineer", "ai/ml", "ml/ai", "deep learning",
        "llm", "applied scientist", "computer vision", "nlp engineer", "genai", "generative ai",
    ]),
    ("data_science", "Data science", ["data scientist", "data science", "research scientist", "quantitative analyst"]),
    ("data_engineering", "Data engineering", [
        "data engineer", "analytics engineer", "etl", "elt", "data platform", "data pipeline", "big data",
        "data architect", "data warehouse", "data warehousing", "data integration", "data infrastructure",
        "data ops", "dataops", "database engineer", "data quality",
    ]),
    ("data_analysis", "Data / business analysis", [
        "data analyst", "business analyst", "bi analyst", "business intelligence", "analytics analyst",
        "reporting analyst", "insights analyst", "data analytics", "power bi", "tableau developer", "bi developer",
        "analytics specialist", "reporting specialist",
    ]),
    ("software_engineering", "Software engineering", [
        "software engineer", "software developer", "sde", "swe", "backend", "back-end", "back end", "frontend",
        "front-end", "front end", "full stack", "full-stack", "fullstack", "web developer", "mobile developer",
        "ios", "android", "devops", "site reliability", "sre", "cloud engineer", "platform engineer",
        "application developer", "programmer",
    ]),
]
FAMILY_LABELS = {key: label for key, label, _ in ROLE_FAMILIES}
UNCLASSIFIED = "unclassified"


def _phrase_in(phrase: str, text: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text) is not None


def role_families(title: str | None) -> set[str]:
    """Every role type a title names: "Machine Learning Data Engineer" is both ML and data engineering."""
    text = (title or "").lower()
    return {key for key, _, phrases in ROLE_FAMILIES if any(_phrase_in(p, text) for p in phrases)}


def role_family(title: str | None) -> str:
    """The first (most specific) role type a title names, for display."""
    text = (title or "").lower()
    for key, _, phrases in ROLE_FAMILIES:
        if any(_phrase_in(p, text) for p in phrases):
            return key
    return UNCLASSIFIED


def profile_role_families(profile: dict, target_titles: list[str] | None = None) -> set[str]:
    """Role types the profile targets (its own title, the search keywords derived from it) or has held
    (every experience entry's role). Having done the work is never a mismatch."""
    titles = [profile.get("title") or ""] + list(target_titles or [])
    titles += [e.get("role") or e.get("title") or "" for e in profile.get("experience", []) or []]
    families = set()
    for t in titles:
        families |= role_families(t)
    return families


# ---------------------------------------------------------------------------
# Skill overlap
# ---------------------------------------------------------------------------

# canonical name -> extra spellings found in job descriptions
_ALIASES = {
    "PostgreSQL": ["postgres", "postgresql"], "Kubernetes": ["kubernetes", "k8s"], "GCP": ["gcp", "google cloud"],
    "AWS": ["aws", "amazon web services"], "Azure": ["azure"], "JavaScript": ["javascript", "js"],
    "TypeScript": ["typescript"], "Node.js": ["node.js", "nodejs"], "Power BI": ["power bi", "powerbi"],
    "scikit-learn": ["scikit-learn", "sklearn"], "CI/CD": ["ci/cd", "ci / cd", "continuous integration"],
    "Machine Learning": ["machine learning"], "A/B testing": ["a/b testing", "a/b tests", "experimentation"],
    "Statistics": ["statistics", "statistical"], "Excel": ["excel", "spreadsheets"], "REST APIs": ["rest api", "rest apis", "restful"],
    "Data modeling": ["data modeling", "data modelling", "dimensional model"], "ETL": ["etl", "elt"],
    "Spark": ["spark", "pyspark"], "Airflow": ["airflow"], "dbt": ["dbt"], "C++": ["c++"], "C#": ["c#"],
    "Go": ["golang"], "Data visualization": ["data visualization", "data visualisation", "dashboards", "dashboard"],
}

_VOCABULARY = [
    "Python", "SQL", "Java", "Scala", "R", "C++", "C#", "Go", "Rust", "JavaScript", "TypeScript", "Node.js", "React",
    "Angular", "Vue", "Django", "Flask", "FastAPI", "Spring", "Ruby", "PHP", "Swift", "Kotlin", "Bash",
    "Airflow", "Prefect", "Dagster", "Luigi", "dbt", "Spark", "Hadoop", "Hive", "Kafka", "Flink", "Beam", "Kinesis",
    "Snowflake", "Redshift", "BigQuery", "Databricks", "Synapse", "Delta Lake", "Iceberg", "PostgreSQL", "MySQL",
    "SQL Server", "Oracle", "MongoDB", "Cassandra", "DynamoDB", "Redis", "Elasticsearch",
    "AWS", "GCP", "Azure", "S3", "Glue", "Lambda", "EMR", "EC2", "Docker", "Kubernetes", "Terraform", "CloudFormation",
    "Ansible", "Jenkins", "GitHub Actions", "CI/CD", "Git", "Linux",
    "Tableau", "Power BI", "Looker", "Excel", "SSIS", "SSRS", "Data visualization", "Statistics", "A/B testing",
    "pandas", "NumPy", "scikit-learn", "TensorFlow", "PyTorch", "Keras", "XGBoost", "MLflow", "Hugging Face",
    "LangChain", "Machine Learning", "NLP", "Computer Vision", "LLM", "RAG",
    "REST APIs", "GraphQL", "Microservices", "Data modeling", "ETL", "Great Expectations", "ArcGIS",
]

# Interchangeable tools in one narrow category earn half credit (same rule the AI scoring prompt uses).
_EQUIVALENT_GROUPS = [
    {"Airflow", "Prefect", "Dagster", "Luigi"}, {"Tableau", "Power BI", "Looker"},
    {"Snowflake", "Redshift", "BigQuery", "Synapse", "Databricks"}, {"Jenkins", "GitHub Actions", "CI/CD"},
    {"PostgreSQL", "MySQL", "SQL Server", "Oracle"}, {"Kafka", "Kinesis"}, {"Flink", "Spark"},
    {"TensorFlow", "PyTorch", "Keras"}, {"Terraform", "CloudFormation"}, {"AWS", "GCP", "Azure"},
]

_PREFERRED_HEADING = re.compile(
    r"(nice[- ]to[- ]have|preferred|bonus|pluses|a plus|good to have|desired|optional)", re.IGNORECASE
)
_SHORT_TERMS = {"R", "Go", "S3", "EC2", "EMR", "NLP", "LLM", "RAG", "Git"}  # matched case-sensitively, too easy to hit by accident


def _patterns(term: str) -> list[str]:
    return [term.lower()] + [a.lower() for a in _ALIASES.get(term, [])]


def _mentions(term: str, text: str, raw_text: str) -> bool:
    if term in _SHORT_TERMS:
        return re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", raw_text) is not None
    return any(_phrase_in(p, text) for p in _patterns(term))


def _split_required_preferred(jd: str) -> tuple[str, str]:
    """Everything after the first 'nice to have / preferred' heading counts as preferred."""
    lines = jd.splitlines()
    for i, line in enumerate(lines):
        if len(line) < 80 and _PREFERRED_HEADING.search(line):
            return "\n".join(lines[:i]), "\n".join(lines[i:])
    return jd, ""


def _profile_vocabulary(profile: dict) -> list[str]:
    """The profile's own skill items, split into atoms ("AWS (S3, Redshift, Glue)" -> AWS, S3, Redshift, Glue)."""
    skills = profile.get("skills") or {}
    items = [s for group in skills.values() for s in (group or [])] if isinstance(skills, dict) else list(skills)
    atoms = []
    for item in items:
        for part in re.split(r"[(),;/]", str(item)):
            part = part.strip()
            if 1 < len(part) <= 30:
                atoms.append(part)
    return atoms


def _evidence_sources(profile: dict) -> list[tuple[str, str]]:
    """(where, text) pairs to search, most specific first: experience and projects before the skills list."""
    sources = []
    for e in profile.get("experience", []) or []:
        where = f"{e.get('role') or e.get('title') or 'Role'} at {e.get('company') or ''}".strip()
        sources.append((where, " ".join([e.get("role") or ""] + list(e.get("bullets") or []))))
    for p in profile.get("projects", []) or []:
        sources.append((f"Project: {p.get('name') or ''}", " ".join(list(p.get("bullets") or []) + list(p.get("technologies") or []))))
    skills = profile.get("skills") or {}
    flat = [s for g in skills.values() for s in (g or [])] if isinstance(skills, dict) else list(skills)
    sources.append(("Skills list", " ".join(map(str, flat))))
    certs = [c if isinstance(c, str) else (c.get("name") or "") for c in profile.get("certifications", []) or []]
    sources.append(("Certifications", " ".join(certs)))
    return sources


def _find_evidence(term: str, sources: list[tuple[str, str]]) -> str | None:
    for where, text in sources:
        if _mentions(term, text.lower(), text):
            return where
    return None


def skill_overlap(profile: dict, jd: str) -> dict:
    jd = jd or ""
    required_text, preferred_text = _split_required_preferred(jd)
    vocab = list(dict.fromkeys(_VOCABULARY + _profile_vocabulary(profile)))
    sources = _evidence_sources(profile)

    requirements = []
    for term in vocab:
        in_required = _mentions(term, required_text.lower(), required_text)
        in_preferred = not in_required and _mentions(term, preferred_text.lower(), preferred_text)
        if in_required or in_preferred:
            requirements.append((term, 1.0 if in_required else 0.5))
    # Drop a term already covered by a longer one ("SQL" inside "SQL Server" is the same requirement).
    names = [t for t, _ in requirements]
    requirements = [(t, w) for t, w in requirements
                    if not any(o != t and t.lower() in o.lower().split() and o in names for o in names)]

    matched, transferable, missing = [], [], []
    earned = total = 0.0
    for term, weight in requirements:
        total += weight
        where = _find_evidence(term, sources)
        if where:
            matched.append({"term": term, "evidence": where, "required": weight == 1.0})
            earned += weight
            continue
        group = next((g for g in _EQUIVALENT_GROUPS if term in g), set())
        alt = next(((o, w) for o in sorted(group - {term}) if (w := _find_evidence(o, sources))), None)
        if alt:
            transferable.append({"term": term, "via": alt[0], "evidence": alt[1], "required": weight == 1.0})
            earned += weight * 0.5
        else:
            missing.append({"term": term, "required": weight == 1.0})

    return {
        "requirements_found": len(requirements),
        "score": round(100 * earned / total) if total else None,
        "matched": matched,
        "transferable": transferable,
        "missing": missing,
    }


# ---------------------------------------------------------------------------
# Fit
# ---------------------------------------------------------------------------

def role_check(profile: dict, job_title: str | None, target_titles: list[str] | None = None) -> dict:
    posting = role_families(job_title)
    families = profile_role_families(profile, target_titles)
    if not posting or not families:
        status = "unknown"
    elif posting & families:
        status = "match"
    else:
        status = "mismatch"
    labels = [FAMILY_LABELS[k] for k, _, _ in ROLE_FAMILIES if k in posting]
    return {
        "posting_family": role_family(job_title),
        "posting_family_label": " / ".join(labels) or "Unclassified title",
        "profile_families": sorted(families),
        "profile_family_labels": [FAMILY_LABELS[f] for f in sorted(families)],
        "status": status,
    }


def mismatch_reason(role: dict) -> str:
    return (f"Role type differs: the posting is {role['posting_family_label']}, the profile targets "
            f"{', '.join(role['profile_family_labels'])}. Fit is capped at {ROLE_MISMATCH_CAP}.")


def apply_role_cap(score: int, role: dict) -> tuple[int, str | None]:
    if role["status"] == "mismatch" and score > ROLE_MISMATCH_CAP:
        return ROLE_MISMATCH_CAP, mismatch_reason(role)
    return score, None


_YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|to|–)?\s*(?:\d{1,2}\s*)?\+?\s*years?", re.IGNORECASE)


def years_check(profile: dict, jd: str) -> dict:
    """Years asked for in the required section (the largest stated minimum) against the profile's total
    experience (overlapping roles counted once)."""
    required_text, _ = _split_required_preferred(jd or "")
    asked = [int(m.group(1)) for m in _YEARS_RE.finditer(required_text) if 0 < int(m.group(1)) <= 15]
    required = max(asked) if asked else None
    have = round(total_experience_months(profile.get("experience", []) or []) / 12, 1)
    if required is None:
        points = 100
    else:
        points = 100 if have >= required else round(100 * have / required)
    return {"required_years": required, "profile_years": have, "points": points}


def rules_fit(profile: dict, job_title: str | None, jd: str, target_titles: list[str] | None = None) -> dict:
    """The no-LLM fit: 60% skill overlap, 20% role type, 20% years asked for, then the role-type cap."""
    skills = skill_overlap(profile, jd)
    role = role_check(profile, job_title, target_titles)
    years = years_check(profile, jd)
    role_points = {"match": 100, "unknown": 50, "mismatch": 0}[role["status"]]
    skill_score = skills["score"] if skills["score"] is not None else 0
    # Years only count beside measurable requirements: meeting "3+ years" says nothing about fit for a
    # posting whose skills this check cannot recognise (a nursing role, say).
    years_points = years["points"] if skills["requirements_found"] else 0
    raw = round(0.6 * skill_score + 0.2 * role_points + 0.2 * years_points)
    score, cap_reason = apply_role_cap(raw, role)
    return {"score": score, "uncapped_score": raw, "cap_reason": cap_reason, "role": role, "skills": skills,
            "years": years}


def analysis_from_rules(fit: dict, visa_sponsorship: str) -> dict:
    """Shape a rules fit like the LLM's match analysis, so every page and the extension read it the same way."""
    skills = fit["skills"]
    matched = [m["term"] for m in skills["matched"]]
    missing = [m["term"] for m in skills["missing"] if m["required"]]
    if skills["requirements_found"] == 0:
        strengths = "No recognisable technical requirements were found in this posting, so skill fit could not be measured."
    else:
        strengths = (f"Meets {len(skills['matched'])} of {skills['requirements_found']} technical requirements found in the "
                     f"posting" + (f", plus {len(skills['transferable'])} through a comparable tool." if skills["transferable"] else "."))
    gaps = []
    if fit["cap_reason"]:
        gaps.append(fit["cap_reason"])
    elif fit["role"]["status"] == "mismatch":
        role = fit["role"]
        gaps.append(f"Role type differs: the posting is {role['posting_family_label']}, the profile targets "
                    f"{', '.join(role['profile_family_labels'])}.")
    years = fit.get("years") or {}
    if years.get("required_years") and years["profile_years"] < years["required_years"]:
        gaps.append(f"The posting asks for {years['required_years']}+ years; the profile shows {years['profile_years']}.")
    if missing:
        gaps.append("Required and not found in the profile: " + ", ".join(missing) + ".")
    return {
        "match_score": fit["score"],
        "matching_skills": matched,
        "missing_keywords": missing,
        "visa_sponsorship": visa_sponsorship,
        "strengths": strengths,
        "gaps_analysis": " ".join(gaps) or "No required technical gaps found.",
        "method": "rules",
    }


# ---------------------------------------------------------------------------
# Rule-based tailoring: reorder, never rewrite
# ---------------------------------------------------------------------------

def _jd_terms(profile: dict, jd: str) -> list[str]:
    vocab = list(dict.fromkeys(_VOCABULARY + _profile_vocabulary(profile)))
    return [t for t in vocab if _mentions(t, (jd or "").lower(), jd or "")]


def _relevance(text: str, terms: list[str]) -> int:
    return sum(1 for t in terms if _mentions(t, (text or "").lower(), text or ""))


def rearrange_for_posting(profile: dict, projects: list[dict], jd: str) -> tuple[list, list, dict]:
    """The no-AI tailoring: every bullet, project and skill is the profile's own text, only reordered so
    the ones naming this posting's requirements come first. Nothing is added or reworded, so nothing can
    be invented. Experience entries keep their order (dates and employers never move)."""
    terms = _jd_terms(profile, jd)

    def by_relevance(items: list, key) -> list:
        # sorted() is stable, so equally relevant items keep the profile's own order
        return sorted(items, key=lambda item: -_relevance(key(item), terms))

    experience = []
    for entry in profile.get("experience", []) or []:
        e = dict(entry)
        e["bullets"] = by_relevance(list(entry.get("bullets") or []), lambda b: b)
        experience.append(e)
    tailored_projects = []
    for project in projects or []:  # already ranked against the posting by resume_rules.select_projects_for_jd
        p = dict(project)
        p["bullets"] = by_relevance(list(project.get("bullets") or []), lambda b: b)
        tailored_projects.append(p)
    skills = profile.get("skills") or {}
    if isinstance(skills, dict):
        skills = {group: by_relevance(list(items or []), str) for group, items in skills.items()}
    return experience, tailored_projects, skills
