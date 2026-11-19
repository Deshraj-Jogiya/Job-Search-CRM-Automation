"""Resume rules taken from ATS and recruiter research (2026-11-15), all mechanical, no LLM.

1. Exact wording. Recruiters find applicants by typing a few words into the ATS search bar, and exact matching still
   decides tool names, certifications and titles. A resume that says "LLMs" does not come up for "large language
   models". `exact_phrase_report` lists the posting's terms missing word for word, split by whether the profile backs
   them; `add_posting_forms_to_skills` writes the posting's form next to the one the resume uses ("Google Cloud (GCP,
   BigQuery)"). Nothing is added that the profile does not already hold.
2. Headline title. Jobscan's 2.5M-application data ties a resume title that matches the posting's title to a much
   higher interview rate. `core_posting_title` strips "New College Grad -", levels and team names.
3. Keywords in bullets. A skill that only sits in the skills list is weaker evidence than one shown in work.
   `keywords_only_in_skills` flags them.
4. One page for new-grad postings. NACE: 66% of employers say one page suits new college graduates.
   `page_limit_for_posting` returns the limit; page_fit_service honours a resume's own "page_limit".
5. GPA scale. A GPA on a 10-point or percentage scale is misread by US parsers and recruiters, and a conversion is
   an estimate, so `us_scale_gpa_only` leaves out any GPA not on the 4.0 scale.
6. Numbered results. Checklists aim for about 80% of bullets with a measured result. `quantified_share` reports it;
   numbered bullets are ordered and kept first, and no number is ever invented to reach the target.
7. Stock phrases. A summary closing such as "Ready to bring..." repeats across resumes and reads as AI-written.
   `stock_summary_phrases` flags them so the sentence is written in the posting's own terms.
Also: a term flagged by rule 3 gets one of the profile's own bullets that shows it (`add_bullets_showing_terms`), and
the projects heading is the plain "Projects" (document_render_service, docx_generator) to match standard headers.
"""
from __future__ import annotations

import json
import re

# ---- 5. GPA ----
_GPA_RE = re.compile(r"\s*(?:[·|,;•-]\s*)?GPA:?\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*", re.IGNORECASE)


def us_scale_gpa_only(text: str | None) -> str | None:
    """Drop a GPA written on any scale other than 4.0 (7.8/10, 82/100); keep 3.9/4.0 as it is."""
    if not text:
        return text

    def keep_or_drop(m: re.Match) -> str:
        return m.group(0) if float(m.group(2)) == 4.0 else " "

    return _GPA_RE.sub(keep_or_drop, text).strip(" ·|,;•-") or None


def education_for_display(edu: dict) -> dict:
    return {**edu, **{k: us_scale_gpa_only(edu.get(k)) for k in ("date", "degree", "gpa") if k in edu}}


# ---- 2. headline title ----
_NEW_GRAD_PREFIX_RE = re.compile(
    r"^\s*(?:new\s+college\s+grad(?:uate)?|new\s+grad(?:uate)?|university\s+grad(?:uate)?|recent\s+grad(?:uate)?|"
    r"early\s+career|entry[- ]level|campus\s+hire)\s*[-–—:,|]?\s*", re.IGNORECASE)
_SENIORITY_RE = re.compile(r"^\s*(?:senior|sr\.?|junior|jr\.?|lead|staff|principal|associate|mid[- ]level)\s+", re.IGNORECASE)
_LEVEL_RE = re.compile(r"(?:\s+(?:level\s+\d+|l\d|i{1,3}|iv|v|\d+))+\s*$", re.IGNORECASE)
_TITLE_CUT_RE = re.compile(r"\s+[-–—|]\s+|,|\s*\(|:")


def core_posting_title(title: str | None) -> str:
    """'New College Grad - Data Scientist, AI for Business Functions' -> 'Data Scientist';
    'Machine Learning Engineer, Level 4' -> 'Machine Learning Engineer'; 'Senior Data Engineer II' -> 'Data Engineer'."""
    t = _NEW_GRAD_PREFIX_RE.sub("", title or "").strip()
    t = _TITLE_CUT_RE.split(t, maxsplit=1)[0].strip()
    t = _SENIORITY_RE.sub("", t).strip()
    return _LEVEL_RE.sub("", t).strip()


def headline_has_posting_title(headline: str | None, posting_title: str | None) -> bool:
    core = core_posting_title(posting_title).lower()
    return bool(core) and core in (headline or "").lower()


def headline_for_posting(profile_title: str | None, posting_title: str | None, role_status: str) -> str | None:
    """The posting's own core title, unless the fit check says the role type is not one the candidate does."""
    core = core_posting_title(posting_title)
    if role_status == "mismatch" or not core:
        return profile_title
    return core


# ---- 4. page limit ----
_NEW_GRAD_TITLE_RE = re.compile(
    r"\bnew\s+(?:college\s+)?grad|\buniversity\s+grad|\brecent\s+grad|\bearly\s+career\b|\bentry[- ]level\b|\bcampus\b|"
    r"\bgraduate\s+program\b", re.IGNORECASE)
_NEW_GRAD_JD_RE = re.compile(
    r"\b0\s*(?:-|–|to)\s*[12]\+?\s*years|currently pursuing or recently completed|\brecent graduates?\b|"
    r"\bnew graduates?\b|\bnew college grad", re.IGNORECASE)


def is_new_grad_posting(title: str | None, jd: str | None) -> bool:
    return bool(_NEW_GRAD_TITLE_RE.search(title or "") or _NEW_GRAD_JD_RE.search(jd or ""))


def page_limit_for_posting(title: str | None, jd: str | None, config: dict) -> int | None:
    """The page limit for this posting when it differs from the default (one page for new-grad postings), else None."""
    pf = config.get("page_fit", {})
    limit = pf.get("new_grad_max_pages")
    if limit and is_new_grad_posting(title, jd) and limit < pf.get("max_pages", limit + 1):
        return limit
    return None


# ---- 1. exact wording ----
# (short form, long form). Short forms are matched case-sensitively so "AI" does not match inside "said".
ABBREVIATIONS = [
    ("LLMs", "large language models"), ("LLM", "large language model"), ("GCP", "Google Cloud"),
    ("ML", "machine learning"), ("AI", "artificial intelligence"), ("NLP", "natural language processing"),
    ("BI", "business intelligence"), ("AWS", "Amazon Web Services"), ("RAG", "retrieval-augmented generation"),
    ("CI/CD", "continuous integration"), ("EDA", "exploratory data analysis"), ("KPIs", "key performance indicators"),
    ("GenAI", "generative AI"), ("CV", "computer vision"), ("DL", "deep learning"),
]

# Technical terms that postings use and recruiters search for. Soft skills are left out: they belong in bullets and
# letters, not in a keyword list.
POSTING_TERMS = [
    "statistical modeling", "statistical analysis", "statistics", "data visualization", "data mining", "data analysis",
    "predictive analytics", "predictive modeling", "predictive models", "machine learning", "deep learning",
    "generative AI", "large language models", "prompt engineering", "AI agents", "agentic", "natural language processing",
    "computer vision", "time series", "forecasting", "optimization", "simulation", "A/B testing", "experimentation",
    "hypothesis testing", "causal inference", "feature engineering", "data pipelines", "data modeling", "data warehouse",
    "data warehousing", "data lake", "lakehouse", "data quality", "data governance", "ETL", "ELT", "business intelligence",
    "dashboards", "recommendation systems", "classification", "regression", "clustering", "anomaly detection",
    "model deployment", "model monitoring", "MLOps", "cloud platforms", "Google Cloud", "BigQuery", "Snowflake",
    "Databricks", "Spark", "PySpark", "Airflow", "dbt", "Kafka", "SQL", "Python", "Java", "Scala", "Tableau", "Power BI",
    "Plotly", "Excel", "Pandas", "NumPy", "Scikit-learn", "TensorFlow", "PyTorch", "XGBoost", "Docker", "Kubernetes",
    "Terraform", "CI/CD", "Git", "REST APIs", "microservices", "AWS", "Azure", "Looker", "PostgreSQL", "NoSQL",
    "MongoDB", "LangChain", "RAG", "vector databases", "embeddings", "fine-tuning", "NLP", "LLMs",
]

# Profile evidence that backs a posting term worded differently in the profile.
BACKED_BY = {
    "statistical modeling": r"regression|hypothesis test|survival analysis|z-test|ks-test|chi-square|statistical",
    "statistical analysis": r"regression|hypothesis test|survival analysis|z-test|ks-test|chi-square|statistical",
    "statistics": r"regression|hypothesis test|statistical|z-test|ks-test",
    "data visualization": r"dashboard|tableau|power bi|plotly|matplotlib|seaborn|visuali",
    "data analysis": r"analy",
    "predictive analytics": r"predict|forecast", "predictive modeling": r"predict|forecast", "predictive models": r"predict|forecast",
    "optimization": r"optimiz", "simulation": r"simulat", "experimentation": r"a/b test|hypothesis test",
    "dashboards": r"dashboard", "data pipelines": r"pipeline", "data warehouse": r"warehouse|snowflake",
    "data warehousing": r"warehouse|snowflake", "model deployment": r"deploy", "model monitoring": r"drift|monitor",
    "cloud platforms": r"aws|azure|gcp|google cloud|snowflake", "agentic": r"agent", "AI agents": r"agent",
    "classification": r"classif", "regression": r"regression", "clustering": r"cluster|k-means",
    "anomaly detection": r"anomal", "embeddings": r"embedding", "fine-tuning": r"fine-tun|lora",
    "REST APIs": r"rest(?:ful)? api", "time series": r"time[- ]series|forecast",
    "data modeling": r"star schema|dimensional|data model", "ELT": r"\bdbt\b|\belt\b",
    "data governance": r"governance|lineage|row-level security|data contract|personally identifiable|\bpii\b|audit log",
    "data quality": r"data quality|great expectations|validation", "feature engineering": r"feature (?:engineering|extraction)",
    "data lake": r"data lake|adls|\bs3\b|delta lake|iceberg", "lakehouse": r"lakehouse|delta lake|iceberg",
    "MLOps": r"mlops|mlflow|drift", "deep learning": r"pytorch|tensorflow|bert|transformer|neural",
    "natural language processing": r"\bnlp\b|bert|spacy|ner\b|sentiment", "computer vision": r"image|object detection|opencv|yolo",
    "recommendation systems": r"recommend", "microservices": r"microservice", "data mining": r"anomal|cluster|pattern|k-means",
}


def backed_posting_terms(jd: str, profile_text: str) -> list[dict]:
    """The posting's own terms that the candidate's work backs, with how ('exact', 'other form', 'evidence'). The
    tailoring writes these in the posting's words: the profile holds what was done, the posting decides how it is
    said. Rewording real work is not fabrication; a term with no backing never appears here."""
    out = []
    for term in posting_terms(jd):
        how = profile_backs(term, profile_text)
        if how:
            out.append({"term": term, "backed_by": how})
    return out


def _term_re(term: str) -> re.Pattern:
    if term.isupper() or term in ("LLMs", "GenAI"):  # an abbreviation: exact case
        return re.compile(r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])")
    body = re.escape(term).replace(r"\ ", r"[\s-]+")
    if len(term) > 4 and term[-1] == "s" and term[-2] != "s":  # "AI agents" also matches "an AI agent", "dashboards" "dashboard"
        body += "?"
    elif len(term) > 4 and term[-1].isalpha():
        body += "s?"
    return re.compile(r"(?<![A-Za-z])" + body + r"(?![A-Za-z])", re.IGNORECASE)


def has_term(text: str, term: str) -> bool:
    return bool(_term_re(term).search(text or ""))


def _other_forms(term: str) -> list[str]:
    out = []
    for short, long in ABBREVIATIONS:
        if term.lower() == short.lower():
            out.append(long)
        elif term.lower() == long.lower():
            out.append(short)
    return out


def posting_terms(jd: str) -> list[str]:
    """POSTING_TERMS that the posting uses word for word, longest first, without a shorter term inside a found one."""
    found = [t for t in sorted(POSTING_TERMS, key=len, reverse=True) if has_term(jd, t)]
    return [t for t in found if not any(t != o and t.lower() in o.lower() for o in found)]


def profile_backs(term: str, profile_text: str) -> str | None:
    """How the profile backs a term: 'exact', 'other form', 'evidence', or None."""
    if has_term(profile_text, term):
        return "exact"
    if any(has_term(profile_text, f) for f in _other_forms(term)):
        return "other form"
    rx = BACKED_BY.get(term)
    if rx and re.search(rx, profile_text, re.IGNORECASE):
        return "evidence"
    return None


def exact_phrase_report(resume_text: str, jd: str, profile_text: str) -> dict:
    """on_resume: posting terms already word for word. reword: backed by the profile but worded differently on the
    resume (term, how backed, the resume's own form if it uses an abbreviation pair). not_in_profile: ask the candidate."""
    report = {"on_resume": [], "reword": [], "not_in_profile": []}
    for term in posting_terms(jd):
        if has_term(resume_text, term):
            report["on_resume"].append(term)
            continue
        backed = profile_backs(term, profile_text)
        if backed:
            resume_form = next((f for f in _other_forms(term) if has_term(resume_text, f)), None)
            report["reword"].append({"term": term, "backed_by": backed, "resume_form": resume_form})
        else:
            report["not_in_profile"].append(term)
    return report


def add_posting_forms_to_skills(skills: dict, jd: str) -> tuple[dict, list[str]]:
    """Where the posting uses one form of an abbreviation pair and a skill uses only the other, write both:
    'GCP (BigQuery)' + posting 'Google Cloud' -> 'Google Cloud (GCP, BigQuery)'; 'LLMs' -> 'Large Language Models (LLMs)'.
    Only renames what is already listed, so it never adds a skill."""
    if not isinstance(skills, dict):
        return skills, []
    skills_text = json.dumps(skills)
    changes = []
    out = {}
    pairs = []
    for short, long in ABBREVIATIONS:
        if has_term(jd, long) and not has_term(skills_text, long) and has_term(skills_text, short):
            pairs.append((short, " ".join(w if w[:1].isupper() else w[:1].upper() + w[1:] for w in long.split())))
        elif has_term(jd, short) and not has_term(skills_text, short) and has_term(skills_text, long):
            pairs.append((long, None))  # resume already has the long form; add the short one after it
    for cat, items in skills.items():
        new_items = []
        for item in items:
            for have, want in pairs:
                # Only a skill that IS the term ("LLMs", "GCP (BigQuery)"): "AI Agents" must not become
                # "Artificial Intelligence (AI) Agents".
                if not re.match(r"\s*" + re.escape(have) + r"\s*(?:\(|$)", item, re.IGNORECASE):
                    continue
                if want is None:  # long form present, posting uses the short form
                    short = next(s for s, l in ABBREVIATIONS if l == have)
                    if has_term(item, short):
                        continue
                    new = _term_re(have).sub(lambda m: f"{m.group(0)} ({short})", item, count=1)
                else:
                    m = re.match(r"\s*" + re.escape(have) + r"\s*\((.*)\)\s*$", item)
                    new = f"{want} ({have}, {m.group(1)})" if m else _term_re(have).sub(f"{want} ({have})", item, count=1)
                if new != item:
                    changes.append(f"{item} -> {new}")
                    item = new
            new_items.append(item)
        out[cat] = new_items
    return out, changes


# ---- 3. keywords only in the skills list ----
def keywords_only_in_skills(resume_doc: dict, terms: list[str]) -> list[str]:
    """Terms the resume lists under Skills but never shows in the summary, title or any bullet."""
    skills_text = json.dumps(resume_doc.get("skills") or {})
    shown = " ".join([resume_doc.get("title") or "", resume_doc.get("summary") or ""] + [
        b for section in ("experience", "projects") for e in (resume_doc.get(section) or [])
        if _bullets_render(section, e) for b in (e.get("bullets") or [])
    ])
    out = []
    for term in terms:
        forms = [term] + _other_forms(term)
        if any(has_term(skills_text, f) for f in forms) and not any(has_term(shown, f) for f in forms):
            out.append(term)
    return out


def _bullets_render(section: str, entry: dict) -> bool:
    """False for a role the renderer prints as a one-line "Earlier:" entry or moves to credentials: its bullets never
    reach the page, so they neither count as shown nor take an added bullet."""
    if section != "experience":
        return True
    from . import resume_rules

    return resume_rules.classify_role(entry, resume_rules.get_config()) not in ("EARLIER", "CREDENTIAL")


def trim_for_one_page(doc: dict) -> dict:
    """The content plan for a one-page resume, applied before the page-fit loop (whose reductions are sized for
    trimming a long resume to two pages, and on their own cut every role to one bullet while projects kept four).
    Two bullets for the three most recent roles and one for older ones (numbered bullets first), two projects with
    two and one bullets (experience outranks projects), the full
    summary, no Languages line and no extra-certificates line."""
    from . import resume_rules

    out = dict(doc)
    out["experience"] = [
        {**e, "bullets": resume_rules.quantified_first(list(e.get("bullets") or []))[: 2 if i < 3 else 1]}
        for i, e in enumerate(doc.get("experience") or [])
    ]
    out["projects"] = [  # experience outranks projects: the second project keeps one bullet
        {**p, "bullets": resume_rules.quantified_first(list(p.get("bullets") or []))[: 2 if i == 0 else 1]}
        for i, p in enumerate((doc.get("projects") or [])[:2])
    ]
    out.pop("languages", None)
    out.pop("additional_certifications", None)
    return out


def add_bullets_showing_terms(resume_doc: dict, profile: dict, terms: list[str], per_entry: int = 1) -> tuple[dict, list[str]]:
    """For each term the resume only lists under Skills, add one of the profile's own bullets that shows it, taken from
    a role or project already on the resume (matched by company or project name). Nothing is written or reworded: the
    bullet is the profile's text as stored. At most `per_entry` added bullets per entry, so no entry grows by more than
    that; page fit still decides what stays on the page. Returns (doc, notes)."""
    out = dict(resume_doc)
    notes: list[str] = []
    sources = (("experience", "company"), ("projects", "name"))
    added: dict[tuple[str, int], int] = {}
    remaining = list(terms)
    for section, key in sources:
        prof_entries = {(e.get(key) or "").strip().lower(): e for e in (profile.get(section) or [])}
        entries = [dict(e, bullets=list(e.get("bullets") or [])) for e in (out.get(section) or [])]
        for i, entry in enumerate(entries):
            prof = prof_entries.get((entry.get(key) or "").strip().lower())
            if not prof or not _bullets_render(section, entry):
                continue
            for term in list(remaining):
                if added.get((section, i), 0) >= per_entry:
                    break
                forms = [term] + _other_forms(term)
                for bullet in prof.get("bullets") or []:
                    if bullet not in entry["bullets"] and any(has_term(bullet, f) for f in forms):
                        entry["bullets"].append(bullet)
                        added[(section, i)] = added.get((section, i), 0) + 1
                        remaining.remove(term)
                        notes.append(f"{term}: added a {entry.get(key)} bullet")
                        break
        out[section] = entries
    return out, notes


# ---- 6. numbered results ----
def quantified_share(resume_doc: dict) -> tuple[int, int]:
    """(bullets with a measured result, all bullets). Resume checklists aim for about 80%; numbers are never invented to
    reach it, numbered bullets are chosen over unnumbered ones that show the same thing."""
    from . import resume_rules

    bullets = [b for s in ("experience", "projects") for e in (resume_doc.get(s) or []) for b in (e.get("bullets") or [])]
    return sum(1 for b in bullets if resume_rules.has_quantified_result(b)), len(bullets)


# ---- 7. stock summary phrases ----
# Hiring managers say they spot AI-written resumes, and a closing line that repeats across every resume reads as one.
STOCK_SUMMARY_PHRASES = [
    "ready to bring", "eager to", "passionate about", "results-driven", "results driven", "proven track record",
    "seeking a", "looking to leverage", "detail-oriented", "team player", "dynamic", "go-getter", "hard-working",
    "self-starter", "excited to",
]


def stock_summary_phrases(summary: str | None) -> list[str]:
    """Stock phrases in a summary. A soft note: the sentence is rewritten in the posting's own terms, never blocked."""
    low = (summary or "").lower()
    return [p for p in STOCK_SUMMARY_PHRASES if p in low]
