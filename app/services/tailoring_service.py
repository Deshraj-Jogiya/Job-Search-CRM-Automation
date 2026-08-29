"""
Resume/cover-letter tailoring. Unlike a naive single-shot LLM tailoring
pass with a hardcoded/faked confidence score, this runs a genuine
tailor -> verify -> refine loop against experience AND projects
together and reports whatever score the LAST verify pass actually
produced, capped at max_refine_passes so a stubborn JD can't loop
forever. Project selection is relevance-driven, not a fixed count --
a JD keyword can be genuinely resolved by either an experience bullet
or a project bullet.

Cover letter scoring is a separate, independent LLM pass -- it is not
derived from the resume's ATS score, since a resume can be a strong
keyword match while the letter itself is weak, or vice versa.
"""

import json
import re

from sqlalchemy.orm import Session

from ..models import JobApplication, TailoredDocument
from .activity_logger import log_activity
from .llm import get_llm_provider, parse_json_response
from .matching_service import MatchingServiceError, get_profile_content_for_application

TARGET_ATS_SCORE = 90
MAX_REFINE_PASSES = 2

# Hard, mechanical cap on how many projects can land in a tailored
# resume -- the LLM is told to select by relevance, not by count, but
# this still bounds real-world resume length regardless of what it
# returns. Enforced in code, not just prompt wording. Lowered 6 -> 4
# -> 3 (2026-08-29): a real generation at 6 projects + full experience
# bullets rendered to 4 physical pages, 4 projects still overflowed to
# 3 pages by about half a page even after aggressive PDF density
# tightening. 3 matches the user's own real reference resume (7
# experience roles + 3 projects, confirmed to fit cleanly in 2 pages
# with the same bullet density this pipeline already produces) --
# genuinely the number that fits, not an arbitrary round number.
_MAX_TAILORED_PROJECTS = 3

# Tools genuinely interchangeable at a skill level, within a single
# narrow category -- e.g. real hands-on Tableau experience is honest
# evidence of BI/data-visualization capability even when a JD names
# Power BI specifically. Used both to soften the tailoring prompts'
# stance on naming a JD's specific tool and, mechanically, in
# _find_unsupported_keywords() below so the fabrication check doesn't
# flag an honest "I used the equivalent tool" claim as fabrication.
# Deliberately narrow and hand-curated (not an LLM judgment call) --
# only tools that are truly substitutable day-to-day are grouped
# together, never whole platforms/ecosystems (e.g. AWS vs. GCP stays
# ungrouped -- genuinely different, non-transferable operational depth).
_TOOL_EQUIVALENCE_GROUPS = [
    {"tableau", "power bi", "powerbi", "looker", "qlik", "qlikview", "metabase"},
    {"airflow", "apache airflow", "prefect", "dagster", "luigi"},
    {"kafka", "apache kafka", "kinesis", "aws kinesis", "pub/sub", "pubsub", "rabbitmq"},
    {"github actions", "gitlab ci", "jenkins", "circleci", "travis ci", "azure devops"},
    {"kubernetes", "amazon ecs", "ecs", "docker swarm", "nomad"},
    # Statistical/experimentation methods -- a real, confirmed false
    # positive (2026-08-27): a JD's own generic "A/B testing, hypothesis
    # testing" phrasing got flagged as unsupported even though the real
    # profile documents Kolmogorov-Smirnov tests and Population Stability
    # Index work -- a KS-test IS a hypothesis test, just named more
    # specifically than the JD's own vocabulary. Same underlying
    # statistical skill, different name.
    {"hypothesis testing", "a/b testing", "ab testing", "kolmogorov-smirnov", "ks-test", "ks test",
     "chi-square test", "t-test", "z-test", "statistical significance testing", "population stability index"},
]

# Re-tailoring one of these would silently undo a decision the human
# already made (submitted, cleared to submit, or explicitly passed on) --
# refuse rather than reverting status back into the confirmation queue.
_FINAL_STATUSES = ("Applied", "Approved", "Rejected", "Interviewing", "Offer", "Not Selected")


def _tailor_experience_and_projects_pass(experience: list, projects: list, jd_text: str) -> dict:
    """Rewrites experience bullets AND selects + rewrites whichever
    projects are genuinely relevant to this JD -- one unified pass
    instead of two disconnected ones. Project selection is relevance-
    driven, not a fixed count: a candidate with many real projects
    (this app's own personal instance has 19) shouldn't have 16 of them
    permanently invisible to every tailored resume just because an
    earlier version hard-capped selection at "exactly 3" regardless of
    the JD."""
    llm = get_llm_provider()
    raw = llm.complete_json(
        system="You are an expert resume writer. You return only raw JSON.",
        prompt=(
            "Rewrite this candidate's professional experience bullets, and separately select + rewrite "
            "whichever of their real projects are genuinely relevant to this job description, to highlight "
            "the skills and achievements -- already present below -- that matter for this JD.\n\n"
            "The experience and projects below are your only source of truth about what this candidate "
            "actually did -- the job description is context for which real achievements to emphasize, "
            "never a source of facts about the candidate. Do not fabricate any work history, dates, "
            "companies, metrics, tools, or techniques, and do not stretch a bullet to imply skills or "
            "domain experience it doesn't genuinely describe. It is normal and expected for a real "
            "candidate's background to not cover every skill a job description mentions -- reflecting that "
            "honestly is correct, not a shortfall to fix.\n\n"
            "One narrow exception: skill and tool are not the same thing. If the JD names a specific tool "
            "the candidate hasn't used, but a bullet already shows hands-on use of a directly comparable "
            "tool in the exact same category (e.g. Tableau vs. Power BI -- both BI/data-visualization "
            "tools; Airflow vs. Prefect/Dagster -- both workflow orchestrators), it is honest to word that "
            "bullet around the real tool the candidate actually used, not the JD's tool name -- the "
            "underlying skill genuinely transfers even though the product name doesn't match. Never claim "
            "hands-on use of the JD's specific tool itself if the candidate has never touched it.\n\n"
            "For projects specifically: select based on genuine relevance to THIS job description, not a "
            "fixed count -- include as many or as few as are actually strong matches (up to 3). A project "
            "belongs in the selection because its real technologies or outcomes would matter to whoever "
            "reads this JD, not to hit a target number. Rewrite each selected project into 3 concise, "
            "metrics-driven bullets using the same real-evidence-only rules as experience above.\n\n"
            "Real resume density, not an essay: a 1-2 page resume is the market standard for this "
            "candidate's experience level, and that only works if bullets stay tight. Aim for roughly 3 "
            "concise, high-impact bullets per experience role (not 5+) -- pick the strongest evidence for "
            "THIS job description rather than listing everything the candidate has ever done in that role.\n\n"
            f"Original Experience:\n{json.dumps(experience, indent=2)}\n\n"
            f"Original Projects:\n{json.dumps(projects, indent=2)}\n\n"
            f"Job Description:\n{jd_text}\n\n"
            "Respond with EXACTLY this JSON shape:\n"
            "{\n"
            '  "experience": [{"role": "...", "company": "...", "location": "...", "date": "...", "bullets": ["...", "..."]}],\n'
            '  "projects": [{"name": "...", "bullets": ["...", "..."], "technologies": ["..."]}]\n'
            "}\n"
            "experience must have the same roles, same order, same dates as the input -- only bullets "
            "change. projects must each be a real project from the input (same name), selected by "
            "relevance, at most 3. Do not wrap the output in markdown code fences."
        ),
        temperature=0.3,
        # Default complete_json budget (2000 tokens) was sized for a
        # single section (just experience, or just summary/skills/3
        # projects) -- this call asks for experience AND up to 3
        # projects together, and a real run hit real truncation (a
        # response cut off mid-string fails JSON parsing outright, not
        # a graceful partial result). Sized generously, not tightly,
        # since underestimating here means a hard failure, not just a
        # shorter response.
        max_tokens=4000,
    )
    result = parse_json_response(raw)
    return {
        "experience": result.get("experience", experience),
        "projects": result.get("projects", [])[:_MAX_TAILORED_PROJECTS],
    }


def _verify_ats_score(experience: list, projects: list, jd_text: str) -> dict:
    """Scores against the candidate's tailored projects too, not just
    experience bullets -- the tailoring loop's own re-score was
    previously blind to projects entirely (same root issue as the
    match_score fix in matching_service.py). Both are already-tailored
    by this point (_tailor_experience_and_projects_pass /
    _refine_experience_and_projects_pass); this function only scores,
    it never rewrites."""
    llm = get_llm_provider()
    raw = llm.complete_json(
        system="You are an ATS parser. You return only raw JSON.",
        prompt=(
            "Score how well this candidate's experience AND projects together match the job description "
            "(0-100), and list any high-priority JD keywords still missing from both. A specific "
            "accomplishment or technology named in a project counts as real evidence, same as an "
            "experience bullet. Skill and tool are not the same thing: if the JD names a specific tool and "
            "the candidate's experience or projects show hands-on use of a directly comparable tool in the "
            "exact same category instead (e.g. Tableau vs. Power BI -- both BI/data-visualization tools; "
            "Airflow vs. Prefect/Dagster -- both workflow orchestrators), treat that as the underlying "
            "skill being covered -- do not list the JD's specific tool name as missing just because the "
            "product name differs from a genuinely equivalent one already shown.\n\n"
            "Weight REQUIRED qualifications far more heavily than PREFERRED/nice-to-have ones -- most JDs "
            "separate the two explicitly (a 'Requirements' section vs. a 'Nice to Have'/'Bonus' section, or "
            "inline phrasing like 'is a plus', 'familiarity with', 'exposure to'). A candidate who strongly "
            "satisfies every required qualification but lacks several explicitly-optional ones should score "
            "85%+, not be capped just because the missing_keywords list has several items on it -- only "
            "list a keyword as high-priority/missing if the JD phrases it as required or a must-have.\n\n"
            f"Candidate Experience:\n{json.dumps(experience, indent=2)}\n\n"
            f"Candidate Projects:\n{json.dumps(projects, indent=2)}\n\n"
            f"Job Description:\n{jd_text}\n\n"
            'Respond with EXACTLY this JSON shape: {"score": 90, "missing_keywords": ["Spark", "Kubernetes"]}\n'
            "Do not wrap the output in markdown code fences."
        ),
        temperature=0.2,
    )
    return parse_json_response(raw)


def _refine_experience_and_projects_pass(experience: list, projects: list, jd_text: str, missing_keywords: list) -> dict:
    llm = get_llm_provider()
    raw = llm.complete_json(
        system="You are an expert resume writer. You return only raw JSON.",
        prompt=(
            "For each keyword below, decide honestly whether it genuinely applies to something this "
            "candidate actually did, based only on their real experience and projects below -- then weave "
            "it into whichever genuinely fits, an experience bullet or a project bullet. Leaving a keyword "
            "out is the correct outcome whenever it doesn't genuinely apply to either; do not force one in "
            "by stretching an unrelated bullet or adding a claim the experience/projects don't support "
            "(e.g. only mention 'Docker' if a bullet already involves containerization/deployment). The "
            "job description is context for interpreting these keywords, never a source of facts about the "
            "candidate.\n\n"
            "One narrow exception: a keyword naming a specific tool can genuinely apply even when the "
            "candidate used a different, directly comparable tool in the exact same category (e.g. real "
            "Tableau experience genuinely supports a 'Power BI' keyword -- both are BI/data-visualization "
            "tools; real Airflow experience genuinely supports a 'Dagster' keyword -- both are workflow "
            "orchestrators). In that case, weave in the REAL tool the candidate actually used, never the "
            "JD's tool name -- the underlying skill transfers, the product name still shouldn't be "
            "invented. If no genuinely comparable tool exists anywhere in the real experience/projects, "
            "leave the keyword out as usual.\n\n"
            f"Keywords to check (include ONLY the ones that genuinely fit): {json.dumps(missing_keywords)}\n\n"
            f"Candidate's real, existing experience (the only source of truth):\n{json.dumps(experience, indent=2)}\n\n"
            f"Candidate's already-selected, real projects (the only source of truth):\n{json.dumps(projects, indent=2)}\n\n"
            f"Job Description:\n{jd_text}\n\n"
            "Respond with EXACTLY this JSON shape (same roles/projects, same order, same dates/names -- only "
            "bullets change, and only where a keyword genuinely applies):\n"
            "{\n"
            '  "experience": [...],\n'
            '  "projects": [...]\n'
            "}\n"
            "Do not wrap the output in markdown code fences."
        ),
        temperature=0.3,
        max_tokens=4000,  # same reasoning as _tailor_experience_and_projects_pass above
    )
    result = parse_json_response(raw)
    return {
        "experience": result.get("experience", experience),
        "projects": result.get("projects", projects)[:_MAX_TAILORED_PROJECTS],
    }


def run_multi_pass_tailoring(experience: list, projects: list, jd_text: str) -> tuple[list, list, int, list, list]:
    """The real tailor -> verify -> refine loop. Returns
    (final_experience, final_projects, final_score,
    initial_missing_keywords, remaining_missing_keywords) -- the score
    is whatever the LAST verify pass actually reported, never a
    placeholder. initial_missing (the gap BEFORE any refinement) lets
    the caller detect fabrication: any keyword that moved from
    "missing" to "resolved" needs checking against the candidate's
    real, untouched profile -- see _find_unsupported_keywords().

    Experience and projects are tailored and refined together in the
    same loop -- a JD keyword can be genuinely resolved by either an
    experience bullet or a project bullet, and project selection is
    relevance-driven (not a fixed count) so a candidate's strongest
    project evidence for THIS JD isn't permanently capped out."""
    tailored = _tailor_experience_and_projects_pass(experience, projects, jd_text)
    tailored_experience, tailored_projects = tailored["experience"], tailored["projects"]
    verification = _verify_ats_score(tailored_experience, tailored_projects, jd_text)
    score = int(verification.get("score", 0))
    missing = verification.get("missing_keywords", [])
    initial_missing = list(missing)

    passes = 0
    while score < TARGET_ATS_SCORE and missing and passes < MAX_REFINE_PASSES:
        refined = _refine_experience_and_projects_pass(tailored_experience, tailored_projects, jd_text, missing)
        tailored_experience, tailored_projects = refined["experience"], refined["projects"]
        verification = _verify_ats_score(tailored_experience, tailored_projects, jd_text)
        score = int(verification.get("score", score))
        missing = verification.get("missing_keywords", [])
        passes += 1

    tailored_projects = _restore_static_project_fields(projects, tailored_projects)
    return tailored_experience, tailored_projects, score, initial_missing, missing


def _restore_static_project_fields(original_projects: list, tailored_projects: list) -> list:
    """The tailoring LLM's response schema only asks for {name, bullets,
    technologies} -- fields that exist on the real project but aren't
    part of that schema (github_url) silently vanish from the tailored
    output otherwise, not because anything went wrong, just because
    nothing asked the LLM to carry them through. A repo URL is static,
    objective data with nothing to tailor about it, so it's restored
    here by matching on project name rather than asking an LLM to
    faithfully echo back a URL string it has no reason to alter but also
    no instruction to preserve."""
    by_name = {p.get("name"): p for p in original_projects}
    for project in tailored_projects:
        original = by_name.get(project.get("name"))
        if original and original.get("github_url") and not project.get("github_url"):
            project["github_url"] = original["github_url"]
    return tailored_projects


def _find_unsupported_keywords(original_profile_content: dict, resolved_keywords: list, extra_text: str = "") -> list:
    """Keywords the refinement loop (or cover letter) claims to have
    resolved/used that don't appear ANYWHERE in the candidate's real,
    untouched profile -- a strong fabrication signal, since there'd be
    no genuine source material for the LLM to have drawn from. Checked
    mechanically (substring match) rather than by another LLM call, so
    it can't be fooled by the same failure mode it's checking for.

    A keyword also counts as supported if the profile shows hands-on
    experience with a directly comparable tool/technique in the same
    _TOOL_EQUIVALENCE_GROUPS category -- e.g. real Tableau experience
    is honest evidence for a "Power BI" keyword, since the underlying
    BI/visualization skill genuinely transfers even though the product
    name differs. Still fully mechanical/deterministic, not an LLM
    judgment call -- only the narrow, curated equivalence groups count,
    nothing else.

    Group matching is substring-based, not exact-match -- a real
    confirmed false positive (2026-08-27): "missing keywords" here are
    whatever the ATS-verify LLM pass extracted from the JD, often a
    whole verbose requirement phrase ("statistics and experimentation
    (A/B testing, hypothesis testing)"), not a single clean term. An
    exact `keyword in group` check never matches a phrase like that even
    when it plainly contains a group member ("hypothesis testing") --
    the real profile's Kolmogorov-Smirnov/PSI project got flagged as
    unsupported despite being the exact real evidence for that
    requirement, just under more specific vocabulary than the JD used."""
    haystack = (json.dumps(original_profile_content) + " " + extra_text).lower()

    def _is_supported(keyword: str) -> bool:
        kw_lower = keyword.lower()
        if kw_lower in haystack:
            return True
        for group in _TOOL_EQUIVALENCE_GROUPS:
            keyword_touches_group = any(term in kw_lower for term in group)
            if keyword_touches_group and any(equivalent in haystack for equivalent in group):
                return True
        # A keyword bundling real tool names inside descriptive wording
        # ("Terraform (Infrastructure as Code)", "Automated agent
        # evaluation tooling (LangSmith/Opik/Langfuse)") fails the exact-
        # phrase check above even when the real profile lists the exact
        # same tools under different surrounding phrasing ("Terraform
        # (IaC)", "LLMOps & Automated Agent Evaluation (LangSmith, Opik,
        # Langfuse)"). Real confirmed false positive (2026-08-29): all
        # three of Terraform, LangSmith/Opik/Langfuse, and a third case
        # were genuinely in the profile's skills list, just worded
        # differently than the JD-extraction pass's phrasing. Checking
        # each atomic term (parenthetical contents, comma/slash-
        # separated pieces) individually catches this without loosening
        # the check for keywords that are genuinely just one made-up
        # concept with no real term anywhere in the profile.
        if any(term in haystack for term in _extract_candidate_terms(keyword)):
            return True
        return False

    return [kw for kw in resolved_keywords if not _is_supported(kw)]


def _extract_candidate_terms(keyword: str) -> list:
    """Breaks a JD-derived keyword phrase into its atomic technology
    terms -- the parts most likely to be literal, checkable tool/product
    names, as opposed to the surrounding descriptive language a JD-
    extraction pass tends to wrap around them. Pulls out parenthetical
    content separately from the text around it, then splits on the
    comma/slash/'and'/'&' separators real JD phrases use to bundle
    several real tool names together. Short generic words are dropped
    (len < 3) but this stays intentionally permissive otherwise -- a
    false match here only means a real keyword-not-fabricated call, the
    same posture as the equivalence-group check right above it."""
    terms = []
    paren_match = re.search(r"\(([^)]*)\)", keyword)
    parts = [keyword]
    if paren_match:
        parts = [keyword[: paren_match.start()], paren_match.group(1)]
    for part in parts:
        for piece in re.split(r"[,/&]|\band\b", part, flags=re.IGNORECASE):
            piece = piece.strip(" ()").lower()
            if len(piece) >= 3:
                terms.append(piece)
    return terms


def _tailor_summary_skills(profile_content: dict, jd_text: str) -> dict:
    """Summary/skills only -- project selection and tailoring now
    happens inside run_multi_pass_tailoring, alongside experience, so
    it can participate in the same JD-keyword refine loop instead of a
    disconnected single-shot pass."""
    llm = get_llm_provider()
    raw = llm.complete_json(
        system="You are an expert resume writer. You return only raw JSON.",
        prompt=(
            "Tailor this candidate's professional summary and skills grouping to align with the job "
            "description. Do not fabricate anything. Keep the summary to 3 concise sentences (roughly "
            "4-5 lines on a resume) -- a resume summary is a hook, not a full recap of the skills section "
            "that follows it.\n\n"
            f"Candidate Profile:\n{json.dumps(profile_content, indent=2)}\n\n"
            f"Job Description:\n{jd_text}\n\n"
            "Respond with EXACTLY this JSON shape:\n"
            "{\n"
            '  "summary": "tailored professional summary",\n'
            '  "skills": { ...same grouping keys as the input profile skills... }\n'
            "}\n"
            "Do not wrap the output in markdown code fences."
        ),
        temperature=0.3,
    )
    return parse_json_response(raw)


def clean_cover_letter(text: str) -> str:
    """Strip any greeting/sign-off lines the LLM added despite
    instructions -- the print template supplies its own."""
    if not text:
        return text
    lines = text.strip().split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip().lower()
        if not stripped:
            cleaned.append("")
            continue
        if stripped.startswith(("dear", "to the", "hello", "hi ", "attention:")):
            continue
        if stripped.startswith(("sincerely", "best regards", "warm regards", "respectfully", "thank you for your time")):
            continue
        cleaned.append(line)
    result = "\n".join(cleaned).strip()
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    return result


def generate_cover_letter(profile_content: dict, company_name: str, job_title: str, jd_text: str) -> str:
    projects_context = ""
    for proj in profile_content.get("projects", []):
        bullets = proj.get("bullets") or ([proj["description"]] if "description" in proj else [])
        projects_context += f"- {proj.get('name', '')}: {' '.join(bullets)}\n"

    llm = get_llm_provider()
    text = llm.complete_text(
        system=(
            "You are a professional cover letter writer. You return ONLY the 3 body paragraphs -- no "
            "greeting, no closing, no signature, since the template supplies those."
        ),
        prompt=(
            "Write an extremely professional, compelling, tailored cover letter (exactly 3 paragraphs, under "
            "300 words total) for this candidate applying to this job.\n\n"
            f"Candidate: {profile_content.get('name')}\n"
            f"Profile: {profile_content.get('summary')}\n"
            f"Recent Projects:\n{projects_context}\n\n"
            f"Target Company: {company_name}\n"
            f"Target Role: {job_title}\n"
            f"Job Description:\n{jd_text}\n\n"
            "Paragraph 1: direct hook expressing strong, specific interest in this role at this company.\n"
            "Paragraph 2: map 1-2 of the candidate's most relevant projects/experience directly to the JD's "
            "technical requirements, with concrete impact.\n"
            "Paragraph 3: reiterate value, professional call-to-action.\n\n"
            "No generic template phrases ('I am excited to apply', 'please find my resume attached'). No "
            "greetings or sign-offs -- start directly with paragraph 1's body text."
        ),
        temperature=0.5,
        max_tokens=700,
    )
    return clean_cover_letter(text)


def score_cover_letter(cover_letter_text: str, jd_text: str) -> int:
    """Independent scoring pass -- deliberately not derived from the
    resume's ATS score, since the two can diverge."""
    llm = get_llm_provider()
    raw = llm.complete_json(
        system="You are an expert hiring manager evaluating cover letters. You return only raw JSON.",
        prompt=(
            "Score how compelling and well-targeted this cover letter is for the job description below, on "
            "a 0-100 scale. Consider specificity, relevance to the JD's actual requirements, and whether it "
            "reads as genuine rather than generic.\n\n"
            f"Cover Letter:\n{cover_letter_text}\n\n"
            f"Job Description:\n{jd_text}\n\n"
            'Respond with EXACTLY this JSON shape: {"score": 85}\n'
            "Do not wrap the output in markdown code fences."
        ),
        temperature=0.2,
    )
    return int(parse_json_response(raw).get("score", 0))


def _upsert_document(db: Session, application_id: int, document_type: str, content: str, ats_score: int = None):
    doc = (
        db.query(TailoredDocument)
        .filter(TailoredDocument.application_id == application_id, TailoredDocument.document_type == document_type)
        .first()
    )
    if not doc:
        doc = TailoredDocument(application_id=application_id, document_type=document_type)
        db.add(doc)
    doc.content = content
    doc.ats_score = ats_score
    db.commit()
    return doc


def tailor_application(db: Session, application_id: int) -> JobApplication:
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise MatchingServiceError(f"Application {application_id} not found.")
    if application.status in _FINAL_STATUSES:
        raise MatchingServiceError(
            f"Application is '{application.status}' -- can't re-tailor a finalized application."
        )

    profile_content, variant_id = get_profile_content_for_application(db, application)
    posting = application.posting
    jd_text = posting.job_description

    try:
        tailored_experience, tailored_projects, final_score, initial_missing, remaining_missing = run_multi_pass_tailoring(
            profile_content.get("experience", []), profile_content.get("projects", []), jd_text
        )
        extras = _tailor_summary_skills(profile_content, jd_text)
    except Exception as e:
        raise MatchingServiceError(f"Resume tailoring failed: {e}") from e

    resolved_keywords = [kw for kw in initial_missing if kw not in remaining_missing]
    unsupported = _find_unsupported_keywords(profile_content, resolved_keywords)

    resume_doc = {
        "name": profile_content.get("name"),
        "title": profile_content.get("title"),
        "contact": profile_content.get("contact"),
        "summary": extras.get("summary", profile_content.get("summary")),
        "skills": extras.get("skills", profile_content.get("skills")),
        "experience": tailored_experience,
        "projects": tailored_projects,
        "education": profile_content.get("education", []),
        "certifications": profile_content.get("certifications", []),
    }
    _upsert_document(db, application.id, "resume", json.dumps(resume_doc, indent=2), ats_score=final_score)

    try:
        cl_text = generate_cover_letter(
            profile_content, posting.company_name_raw, posting.job_title, jd_text
        )
        cl_score = score_cover_letter(cl_text, jd_text)
    except Exception as e:
        raise MatchingServiceError(f"Cover letter generation failed: {e}") from e

    _upsert_document(db, application.id, "cover_letter", cl_text, ats_score=cl_score)

    # Independent check: which JD keywords does the cover letter (a
    # separate LLM call/prompt from the resume loop) actually claim,
    # and are those claims backed by the real profile? Checked against
    # initial_missing (not `unsupported`) so this can't just be a
    # subset of what the resume check already found.
    cl_mentioned = [kw for kw in initial_missing if kw.lower() in cl_text.lower()]
    cl_unsupported = _find_unsupported_keywords(profile_content, cl_mentioned)
    all_unsupported = sorted(set(unsupported) | set(cl_unsupported))

    application.match_score = final_score
    application.cover_letter_score = cl_score
    application.profile_variant_id = variant_id
    application.status = "Tailored"

    if all_unsupported:
        application.attention_reason = (
            "Possible fabrication: the AI tailoring may have claimed experience with "
            f"{', '.join(all_unsupported)} that doesn't appear anywhere in your real profile. "
            "Review the tailored resume/cover letter before using them."
        )[:250]
        log_activity(
            db,
            f"FABRICATION WARNING on '{posting.job_title}' at {posting.company_name_raw}: "
            f"unsupported keywords {all_unsupported} -- resume/cover letter need manual review.",
            "WARNING",
        )
    else:
        application.attention_reason = None

    db.commit()

    log_activity(
        db,
        f"Tailored '{posting.job_title}' at {posting.company_name_raw}: "
        f"resume {final_score}%, cover letter {cl_score}%.",
        "INFO",
    )

    # Hand off to the confirmation queue -- routes to Needs
    # Review if the fabrication check above (or intake's scam-pattern
    # check) flagged anything, otherwise into a timed Pending
    # Confirmation with a notification. Import here, not at module
    # scope, to keep tailoring_service usable standalone / in tests
    # without pulling in the notification/email stack.
    from .confirmation_service import evaluate_and_enqueue
    evaluate_and_enqueue(db, application.id)
    db.refresh(application)

    return application
