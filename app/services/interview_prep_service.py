"""
Interview prep generation. Independent pieces:

- General prep: a short, quick-reference cheat sheet of real strengths
  to steer toward and real gaps worth a prepared honest answer for --
  not Q&A (the actual drafted answers live in predicted rounds below),
  just glance-at-your-notes pointers that apply to this type of role
  regardless of which company it's at.
- Company-specific prep: company context, real recently-published work
  (via _research_company_publications), and questions worth asking
  them -- grounded in real research where Tavily is configured, not
  the LLM's own (possibly stale or invented) knowledge of the company.
  Same "discover real info, don't guess" posture as
  contact_discovery_service.py, which is where tavily_search() lives.
  Direct fetches of a company's own site often hit bot-detection this
  app deliberately does not try to route around (e.g. mckinsey.com's
  Akamai WAF) -- Tavily's own search index reaches indexed/cached
  content instead.
- Process research: same posture applied to the interview PROCESS
  itself, not just company facts -- real candidates' reported rounds
  and format (Glassdoor/Blind/etc-style sources, via Tavily), because
  a company like a consulting firm running case interviews + a
  Personal-Experience-Interview needs very different prep than a
  startup running a take-home + one call. Falls back to a clearly-
  labeled generic structure when nothing real is found, same as every
  other optional integration here.
- Predicted rounds: the actual round-by-round study material. Each
  round gets full, ready-to-say drafted answers (qa_pairs) grounded
  only in the candidate's real profile and confirmed behavioral
  stories -- never invented achievements -- plus a short quick_reference
  cue per answer for glancing at during the real call, and
  questions_to_ask_them scoped to who's realistically running that
  specific round (a recruiter screen isn't run by the engineer who'd
  recognize a deep technical question). Nothing here is capped to a
  fixed count -- real interviews aren't bounded by a quota, and neither
  is this prep.

On-demand (a button on the application detail page), not automatic --
same real-LLM-cost reasoning as scoring/tailoring elsewhere in this
app. Available for any application except Rejected -- prepping for a
job you've already passed on isn't useful, but there's no need for a
stricter gate than that (mirrors the on-demand posture of score/tailor,
not a hard-stop pattern like the confirmation queue).
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import InterviewPrep, JobApplication, TailoredDocument, get_or_create_settings
from . import behavioral_story_service
from .activity_logger import log_activity
from .contact_discovery_service import is_tavily_configured, tavily_search
from .llm import get_llm_provider, parse_json_response
from .matching_service import MatchingServiceError, get_profile_content_for_application


class InterviewPrepServiceError(Exception):
    """User-facing failure -- callers show the message instead of a 500."""


_METRIC_PATTERN = re.compile(r"\d+(?:\.\d+)?%|\$\d+(?:\.\d+)?[kKmMbB]?|\d+(?:,\d{3})*\+|\d+x\b", re.IGNORECASE)


def check_answer_grounding(profile_content: dict, predicted_rounds: dict) -> list[str]:
    """Mechanical (non-LLM), best-effort fabrication check on drafted
    answers -- same posture as profile_service.detect_profile_regressions:
    catches the clearest case (a specific metric cited in a drafted
    answer that appears nowhere in the real profile grounding it) without
    claiming to catch everything. A drafted answer is something the
    candidate might say out loud in a real interview, so this deserves
    the same scrutiny tailored resume content already gets. Surfaced as
    warnings for human review, never blocks generation -- a real number
    the model paraphrased differently (e.g. "nearly a third" vs "30%")
    can trigger a false positive, so treat this as a prompt to
    double-check, not a verdict."""
    profile_text = json.dumps(profile_content).lower()
    warnings = []
    for round_ in predicted_rounds.get("rounds", []):
        for qa in round_.get("qa_pairs", []):
            answer = qa.get("draft_answer", "")
            for metric in sorted(set(_METRIC_PATTERN.findall(answer))):
                if metric.lower() not in profile_text:
                    warnings.append(
                        f"{round_.get('round_name', 'Round')} -> "
                        f"\"{qa.get('question', '')[:60]}\": draft answer cites '{metric}', "
                        "not found anywhere in your real profile -- verify before using."
                    )
    return warnings


def _light_company_research(db: Session, company_name: str) -> str:
    """Best-effort, never raises -- an empty string means the
    company-specific prompt falls back to JD-only grounding, same
    graceful-degrade posture as every other optional integration here."""
    if not is_tavily_configured():
        return ""
    try:
        results = tavily_search(db, f"{company_name} company news mission products", max_results=4)
    except Exception:
        return ""
    snippets = [r.get("content", "")[:400] for r in results if r.get("content")]
    return "\n\n".join(snippets)


def _research_company_publications(db: Session, company_name: str) -> list:
    """Best-effort, never raises. Distinct from _light_company_research
    (generic "what does this company do" facts) -- this specifically
    hunts for the company's own recent published work (articles,
    insights, case studies) so company-specific prep can cite something
    a real interviewer might actually expect a candidate to have read,
    not generic boilerplate. Direct fetches of a company's own site
    often hit bot-detection (e.g. mckinsey.com's Akamai WAF returns
    "Access Denied" to automated fetchers) -- Tavily's own search index
    routes around that without this app trying to bypass any block
    itself. Returns a list of {"title", "url", "snippet"}; an empty
    list means the caller falls back to JD-only grounding."""
    if not is_tavily_configured():
        return []
    try:
        results = tavily_search(
            db, f"{company_name} recent published insights articles case studies client work", max_results=6
        )
    except Exception:
        return []
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")[:500]}
        for r in results
        if r.get("content")
    ]


def _research_interview_process(db: Session, company_name: str, job_title: str) -> dict:
    """Best-effort, never raises. Searches for real, reported interview
    rounds/format for this company+role -- distinct from
    _light_company_research, which looks for company facts, not process.
    Returns {"summary": str, "sources": [url, ...]}; an empty summary
    means the round-prediction prompt below falls back to a generic,
    clearly-labeled structure instead of guessing at this company's
    actual process."""
    if not is_tavily_configured():
        return {"summary": "", "sources": []}
    try:
        general_results = tavily_search(
            db,
            f"{company_name} {job_title} interview process rounds format questions candidate experience "
            "Glassdoor Blind Reddit real interview experience",
            max_results=6,
        )
    except Exception:
        general_results = []
    # A separate, dedicated query for the recruiter/HR screen specifically --
    # candidates reporting on a company's overall process tend to focus on
    # the technical/case rounds and skim past the recruiter call in one
    # sentence, so it needs its own search to get real texture (what it
    # actually covers, how long, what they're screening for) instead of
    # inheriting whatever scraps the general query happens to surface.
    try:
        recruiter_results = tavily_search(
            db,
            f"{company_name} recruiter phone screen HR call what to expect questions asked real experience",
            max_results=4,
        )
    except Exception:
        recruiter_results = []
    results = general_results + recruiter_results
    if not results:
        return {"summary": "", "sources": []}
    snippets = [r.get("content", "")[:500] for r in results if r.get("content")]
    sources = list(dict.fromkeys(r.get("url") for r in results if r.get("url")))
    return {"summary": "\n\n".join(snippets), "sources": sources}


def _research_company_reputation(db: Session, company_name: str) -> str:
    """Best-effort, never raises. Distinct from _light_company_research
    (what the company does) -- this hunts specifically for real public
    criticism/controversy, so 'why this company' and values-fit answers
    can be genuinely informed rather than naive PR-brochure enthusiasm. A
    candidate who's never heard of a company's real controversies sounds
    unprepared if an interviewer probes; one who can acknowledge it with
    informed nuance (and explain their real reasons for wanting in
    anyway) sounds like someone who did actual homework. An empty string
    means the caller doesn't force this angle -- not every company has a
    well-known controversy, and nothing should be invented if none was
    found."""
    if not is_tavily_configured():
        return ""
    try:
        results = tavily_search(
            db, f"{company_name} controversy criticism ethics reputation concerns", max_results=4
        )
    except Exception:
        return ""
    snippets = [r.get("content", "")[:400] for r in results if r.get("content")]
    return "\n\n".join(snippets)


def _format_confirmed_stories(confirmed_stories: list) -> str:
    if not confirmed_stories:
        return ""
    lines = []
    for s in confirmed_stories:
        traits = ", ".join(json.loads(s.traits_json)) if s.traits_json else ""
        lines.append(
            f"- \"{s.title}\" ({traits}). Situation: {s.situation} Task: {s.task} "
            f"Action: {s.action} Result: {s.result}"
        )
    return "\n".join(lines)


def _format_publications(publications: list) -> str:
    if not publications:
        return ""
    lines = [f"- \"{p['title']}\" ({p['url']}): {p['snippet']}" for p in publications if p.get("title")]
    return "\n".join(lines)


def _generate_round_structure(
    job_title: str, company_name: str, jd_text: str, process_research: dict,
) -> dict:
    """Phase 1 of predicted-rounds generation: just the shape of the
    process (round names, what each tests, who likely runs it) -- no
    drafted answers yet. Kept as its own small call so its output stays
    naturally bounded regardless of how many rounds a rich JD implies;
    see _generate_round_qa for why the answers themselves are generated
    per-round instead of all at once (a single call trying to hold full
    drafted answers for an uncapped number of rounds kept truncating
    mid-JSON at both 8000 and 16000 max_tokens in real testing -- this
    isn't a bigger-number problem, it's a decomposition problem)."""
    llm = get_llm_provider()
    if process_research.get("summary"):
        research_block = (
            "Real, reported information found about this company's actual interview process:\n"
            f"{process_research['summary']}\n\n"
        )
    else:
        research_block = (
            "No real information about this company's actual interview process was found. Do NOT invent "
            "specific claims about this company's real process, and set grounded_in_real_research to "
            "false -- but that only limits what you claim to know about THIS company's specific process. "
            "It does not mean less preparation: still cover a realistic, comprehensive default structure "
            "(e.g. recruiter/HR screen, technical screen, deeper technical/system-design round, behavioral "
            "round) with full depth in each. A real interview can go in directions research never "
            "predicted -- the goal is broad readiness, not narrowly matching whatever was or wasn't "
            "found.\n\n"
        )
    raw = llm.complete_json(
        system="You are an expert interview coach. You return only raw JSON.",
        prompt=(
            f"Lay out a realistic, thorough round-by-round interview process structure for a candidate "
            f"interviewing for {job_title} at {company_name}.\n\n{research_block}"
            f"Job Description:\n{jd_text}\n\n"
            "Also identify the real ANSWERING METHOD this candidate should use, grounded in whatever real "
            "process research is above and current (2026) market practice for this type of interview -- not "
            "a generic \"use STAR\" platitude. If research above shows this company/round uses a distinctive "
            "format (e.g. a consulting-style Personal Experience Interview built around ONE deep story per "
            "session with 10-25 rapid-fire follow-ups probing exact words/reasoning/reflection, versus a "
            "standard behavioral round where a single 90-120s STAR-shaped answer is the whole response), say "
            "so explicitly and explain how that changes preparation (e.g. 'have 2 stories ready per core "
            "trait this firm evaluates, not just one generic answer per question -- each must survive deep "
            "follow-up drilling, not just sound good once'). If no distinctive format was found in research, "
            "default to explaining standard STAR (Situation, Task, Action, Result) delivered in roughly 90-"
            "120 seconds, which is still the 2026 market default for behavioral rounds.\n\n"
            "Respond with EXACTLY this JSON shape:\n"
            "{\n"
            '  "rounds": [\n'
            "    {\n"
            '      "round_name": "...",\n'
            '      "what_it_tests": "...",\n'
            '      "likely_interviewer": "e.g. \'recruiter/HR generalist, not deeply technical\' or \'senior data engineer\'",\n'
            '      "prep_focus": ["pointer", "..."]\n'
            "    }\n"
            "  ],\n"
            '  "answering_method_guidance": "2-4 sentences: the real answering method/format to use, grounded as described above",\n'
            '  "grounded_in_real_research": true or false\n'
            "}\n"
            "Do not artificially limit yourself to a fixed number of rounds -- include as many as are "
            "realistically distinct for this specific role and company, not a padded-out quota and not an "
            "arbitrarily trimmed-down list. Do not wrap the output in markdown code fences."
        ),
        temperature=0.4,
        max_tokens=3000,
    )
    return parse_json_response(raw)


def _generate_round_qa(
    round_info: dict, job_title: str, company_name: str, jd_text: str,
    profile_content: dict, confirmed_stories: list, publications: list, answer_target: int,
    reputation_research: str = "", answering_method_guidance: str = "",
) -> dict:
    """Phase 2: full drafted answers + questions_to_ask_them for ONE
    round. Called once per round (in parallel -- see _generate_predicted_
    rounds) so each call's output size is bounded by one round's worth
    of content, not the whole process's.

    The number of QUESTIONS surfaced is still uncapped -- a real
    interview isn't bounded by a quota -- but drafting a full, ready-to-
    say answer for every single one is what actually drives real LLM
    cost/latency (confirmed the hard way: JSON truncation at 8000, then
    16000, then per-round at 6000 and 12000 max_tokens, before this
    existed). answer_target caps how many get a full drafted answer;
    anything beyond that still surfaces in other_possible_questions
    (question text only) instead of vanishing -- many of those are
    answerable by adapting a nearby drafted answer anyway. Defaults low
    (see GlobalSettings.interview_prep_answer_target's docstring) since
    this is a public platform other forkers may run on a free-tier key,
    not just this deployment -- live-editable per instance for anyone
    who wants deeper prep and is fine paying more for it."""
    llm = get_llm_provider()
    is_technical_round = round_info.get("likely_interviewer", "").lower().find("recruiter") == -1 and \
        round_info.get("likely_interviewer", "").lower().find("hr") == -1

    stories_block = ""
    if confirmed_stories:
        stories_block = (
            "\nThe candidate has these real, human-confirmed behavioral stories on file. If this round is "
            "behavioral/fit-focused, prefer drafting answers that surface one of these real stories "
            "(referenced naturally, not verbatim-copied) over inventing a new anecdote:\n"
            f"{_format_confirmed_stories(confirmed_stories)}\n"
        )

    publications_block = ""
    if publications and is_technical_round:
        publications_block = (
            "\nThe company's own real, recently published work -- this round is realistically run by "
            "someone close to the technical work, so it's fair to expect they might recognize a genuine, "
            "specific connection to one of these (cite the real title, never invent one):\n"
            f"{_format_publications(publications)}\n"
        )
    elif publications:
        publications_block = (
            "\nThis round is realistically run by a recruiter/HR generalist, not someone who wrote or "
            "would recognize the company's technical publications -- do NOT cite specific articles here.\n"
        )

    reputation_block = ""
    if reputation_research:
        reputation_block = (
            "\nReal, publicly reported criticism/controversy found about this company -- if this round could "
            "plausibly raise a \"why this company\" / values-fit / \"how do you feel about X\" question, the "
            "draft_answer should sound like someone who did real homework, not someone naively reciting "
            "brochure enthusiasm. That means: acknowledge awareness of the real issue if directly relevant, "
            "then give the candidate's own genuine, specific reason for still wanting to join (not a "
            "dismissal of the concern, and not fabricated inside knowledge of the company's remediation --  "
            "only reference the company's actual public response if it's part of the research below). Do NOT "
            "force this into every answer -- only where a values/motivation question would realistically "
            "surface it:\n"
            f"{reputation_research}\n"
        )

    method_block = ""
    if answering_method_guidance:
        method_block = (
            f"\nThe real answering method/format expected for this company's process (use this to shape "
            f"draft_answer structure and pacing, not just as a note to the candidate): {answering_method_guidance}\n"
        )

    raw = llm.complete_json(
        system="You are an expert interview coach. You return only raw JSON.",
        prompt=(
            f"Draft full interview prep for ONE specific round of a {job_title} interview at {company_name}: "
            f"\"{round_info.get('round_name')}\" -- {round_info.get('what_it_tests')} "
            f"(likely run by: {round_info.get('likely_interviewer', 'unknown')}).\n\n"
            f"Job Description:\n{jd_text}\n\n"
            f"Candidate's Real Profile:\n{json.dumps(profile_content, indent=2)}\n"
            f"{stories_block}{publications_block}{reputation_block}{method_block}\n"
            "Draft actual ready-to-say answers, not just topics to mention -- a candidate should be able "
            "to read a draft_answer aloud as a real response, then adapt it in their own words. Keep each "
            "draft_answer realistic spoken length -- what a person would actually say in 60-90 seconds "
            "(roughly 3-5 sentences), not a multi-paragraph essay; a real answer that goes long does so by "
            "the candidate elaborating live, not by the prep material being an essay to memorize. Every "
            "draft_answer must be grounded in something that genuinely appears in the candidate's real "
            "profile above -- never invent achievements, numbers, or tools they don't have.\n\n"
            "How a strong answer actually sounds -- these are not optional style points, a violation makes "
            "the answer unusable:\n"
            "- NEVER reference the source material as a document -- no \"as my resume notes,\" \"my profile "
            "mentions,\" \"as you can see from my application,\" or any variant. The candidate is speaking "
            "from memory about their own life, not citing a paper they're both looking at; say the fact "
            "itself (\"Yes, I'm open to relocating\"), never point at where it's written down.\n"
            "- Open with substance, not a credentials readout. A \"tell me about yourself\"/background "
            "question must NOT open with \"Sure, I have X years of experience...\" or a chronological list "
            "of titles and degrees -- lead with one concrete, specific thing (a real project, a real "
            "outcome, a real problem solved) that makes the interviewer want to hear more, THEN weave in "
            "the credentials as support, not as the headline.\n"
            "- Every answer must show actual work, not just name-drop what was touched. \"I've built "
            "production data pipelines on Databricks and Snowflake\" is a title, not a story -- say what "
            "the pipeline actually did, what problem it solved, or what changed because of it. If a "
            "profile bullet already has a real number or outcome, the answer should use it; if it doesn't, "
            "describe the real mechanism/decision instead of just the tool name.\n"
            "- Answers should read like a person telling a colleague what they actually did, with a real "
            "beginning-middle-end shape (what the situation was, what they did about it, what happened) -- "
            "not a flat list of nouns and technologies stitched together with commas.\n\n"
            "Match question style to who is realistically running THIS round: if it's a recruiter/HR "
            "screen, keep questions accessible (background, motivation, logistics, high-level project "
            "summaries), not deep technical internals; if it's a technical/hiring-manager round, go "
            "genuinely deep. Cover realistic curveballs too (a gap, a tradeoff that didn't pan out, a "
            "disagreement with a teammate), not just the flattering questions. Also draft a few "
            "questions_to_ask_them appropriate to who's running this specific round.\n\n"
            "For every qa_pair whose category is behavioral, motivation, or background: also fill "
            "possible_follow_ups with the realistic probing/cross-questions a sharp interviewer would ask "
            "AFTER hearing that draft_answer -- do NOT answer these, just list the questions themselves, the "
            "way a real interviewer digs deeper (\"what exactly did you say in that moment,\" \"how did the "
            "other person react,\" \"what would you have done differently,\" \"what did that experience "
            "change about how you work now,\" \"walk me through the moment you realized X\"). Include enough "
            "that the candidate can't just have one rehearsed paragraph and call it done -- a thin list here "
            "defeats the purpose. For logistics/technical qa_pairs, possible_follow_ups can be a shorter "
            "list or empty if the question genuinely doesn't invite probing.\n\n"
            f"First, think about every realistically distinct question this round could cover -- don't cap "
            f"that list. Then draft a full, ready-to-say answer (qa_pairs) for up to {answer_target} of the "
            f"most important/likely ones (fewer is fine if the round genuinely has less ground to cover). "
            f"Put every remaining realistic question -- ones a candidate could reasonably answer by adapting "
            f"a nearby drafted answer, or that are lower-priority -- in other_possible_questions as plain "
            f"question text, no answer. Nothing gets silently dropped, it just doesn't all get a full "
            f"pre-written answer.\n\n"
            "Respond with EXACTLY this JSON shape:\n"
            "{\n"
            '  "qa_pairs": [\n'
            '    {"question": "...", "category": "background|motivation|logistics|technical|behavioral", "draft_answer": "full ready-to-say answer", "quick_reference": "one short line -- the cue to glance at during the actual call, not the full answer", "possible_follow_ups": ["realistic follow-up question, not answered", "..."]}\n'
            "  ],\n"
            '  "other_possible_questions": ["...", "..."],\n'
            '  "questions_to_ask_them": ["...", "..."]\n'
            "}\n"
            "Do not wrap the output in markdown code fences."
        ),
        temperature=0.4,
        max_tokens=8000,
    )
    return parse_json_response(raw)


def _generate_predicted_rounds(
    job_title: str, company_name: str, jd_text: str, process_research: dict,
    profile_content: dict, confirmed_stories: list, publications: list, answer_target: int,
    reputation_research: str = "",
) -> dict:
    structure = _generate_round_structure(job_title, company_name, jd_text, process_research)
    rounds = structure.get("rounds", [])
    answering_method_guidance = structure.get("answering_method_guidance", "")

    # Each round's Q&A call is independent of the others (same shared
    # inputs, no cross-round dependency), so they run concurrently
    # instead of one-after-another -- a real QuantumBlack-scale process
    # (7 rounds) took ~17 minutes sequentially before this; running them
    # in parallel is the difference between that and a few minutes.
    with ThreadPoolExecutor(max_workers=min(len(rounds), 6) or 1) as pool:
        qa_results = list(pool.map(
            lambda r: _generate_round_qa(
                r, job_title, company_name, jd_text, profile_content, confirmed_stories, publications,
                answer_target, reputation_research, answering_method_guidance,
            ),
            rounds,
        ))

    for round_info, qa in zip(rounds, qa_results):
        round_info["qa_pairs"] = qa.get("qa_pairs", [])
        round_info["other_possible_questions"] = qa.get("other_possible_questions", [])
        round_info["questions_to_ask_them"] = qa.get("questions_to_ask_them", [])
    return structure


def _generate_general_prep(profile_content: dict, jd_text: str) -> dict:
    """Cheat-sheet-style pointers, not Q&A -- the actual drafted answers now
    live in _generate_predicted_rounds's qa_pairs. This stays scoped to
    quick-reference strengths/gaps, the kind of thing worth a single line
    on a one-page printout, not something you'd read aloud verbatim."""
    llm = get_llm_provider()
    raw = llm.complete_json(
        system="You are an expert interview coach. You return only raw JSON.",
        prompt=(
            "Based on this candidate's real background and the target role, identify the strengths worth "
            "actively steering the conversation toward, and the real gaps worth having a honest, prepared "
            "answer for if raised -- not questions to answer, just quick-reference points for a candidate "
            "glancing at notes.\n\n"
            f"Candidate Profile:\n{json.dumps(profile_content, indent=2)}\n\n"
            f"Target Role (job description):\n{jd_text}\n\n"
            "Respond with EXACTLY this JSON shape:\n"
            "{\n"
            '  "strengths_to_emphasize": ["...", "..."],\n'
            '  "potential_gaps_to_address": ["...", "..."]\n'
            "}\n"
            "Ground every item in something that genuinely appears in the candidate's profile -- do not "
            "invent experience, and do not soften a real gap into something false. Include as many "
            "genuinely relevant items as apply -- don't pad the list to hit a quota, and don't trim a real "
            "one to fit one. Do not wrap the output in markdown code fences."
        ),
        temperature=0.4,
    )
    return parse_json_response(raw)


def _generate_company_prep(
    company_name: str, job_title: str, jd_text: str, research: str, publications: list,
    reputation_research: str = "",
) -> dict:
    llm = get_llm_provider()
    if research or publications:
        research_block = ""
        if research:
            research_block += f"Recent real information found about the company:\n{research}\n\n"
        if publications:
            research_block += (
                "The company's own real, recently published work (cite these by real title when relevant "
                "-- never invent an article that isn't in this list):\n"
                f"{_format_publications(publications)}\n\n"
            )
    else:
        research_block = (
            "No external research was found for this company -- do not invent specific facts (funding, "
            "headcount, recent news, publications). Ground company_context and the questions in the job "
            "description's own specific language instead of generic boilerplate, and set "
            "grounded_in_real_research to false.\n\n"
        )
    reputation_block = ""
    if reputation_research:
        reputation_block = (
            "Real, publicly reported criticism/controversy found about this company:\n"
            f"{reputation_research}\n\n"
            "why_this_company_talking_points should read like someone who did real homework, not brochure "
            "enthusiasm -- it's fine and expected for one talking point to show informed awareness of the "
            "real issue above alongside a genuine, specific reason for still wanting to join (e.g. citing "
            "the company's own real remediation steps if they appear in the research, or the candidate's "
            "own values-based reasoning) -- never a naive answer that reads as if the candidate has never "
            "heard of it, and never a dismissal of the concern as unimportant.\n\n"
        )
    raw = llm.complete_json(
        system="You are an expert interview coach. You return only raw JSON.",
        prompt=(
            f"Generate company-specific interview prep for a candidate interviewing for {job_title} at "
            f"{company_name}.\n\n"
            f"{research_block}{reputation_block}"
            f"Job Description:\n{jd_text}\n\n"
            "Respond with EXACTLY this JSON shape:\n"
            "{\n"
            '  "company_context": "what this company does and what seems to matter to them right now, grounded in whatever real info is above",\n'
            '  "recent_publications": [{"title": "...", "url": "...", "why_it_matters": "..."}],\n'
            '  "why_this_company_talking_points": ["...", "..."],\n'
            '  "questions_to_ask_them": ["...", "..."],\n'
            '  "grounded_in_real_research": true or false\n'
            "}\n"
            "recent_publications should only include real items from the list above (empty array if none "
            "were provided) -- pick the ones a candidate would genuinely benefit from having skimmed, not "
            "every single one indiscriminately. questions_to_ask_them must be a genuine mix, not all "
            "technical: include team/day-to-day questions, growth/success-metric questions, and (only when "
            "real research or publications were provided above) a question tied to something specific and "
            "current about the company -- never a generic 'what's the culture like' filler if you have "
            "anything more specific to draw on. Include as many genuinely distinct, useful "
            "questions/talking-points as apply -- don't pad to a quota, don't trim a real one to fit. Do "
            "not wrap the output in markdown code fences."
        ),
        temperature=0.4,
        max_tokens=4000,
    )
    return parse_json_response(raw)


def resolve_grounding_profile(db: Session, application: JobApplication) -> tuple[dict, int, bool]:
    """Prefer the resume actually tailored/submitted for THIS application
    over the raw base profile -- a tailored resume can emphasize
    different projects/bullets for this specific JD, and anything
    grounded in it (prep, mock-interview feedback) should match what
    the interviewer is actually holding, not a generic baseline. Falls
    back to the base profile when nothing's been tailored yet. Returns
    (profile_content, variant_id, used_tailored_resume) -- variant_id
    is still needed by callers for variant-level lookups like the
    behavioral story bank, which stays tied to the base profile variant
    regardless of which document grounds this particular generation."""
    try:
        base_profile_content, variant_id = get_profile_content_for_application(db, application)
    except MatchingServiceError as e:
        raise InterviewPrepServiceError(str(e)) from e

    tailored_resume = (
        db.query(TailoredDocument)
        .filter(TailoredDocument.application_id == application.id, TailoredDocument.document_type == "resume")
        .first()
    )
    profile_content = json.loads(tailored_resume.content) if tailored_resume else base_profile_content
    return profile_content, variant_id, bool(tailored_resume)


def generate_interview_prep(db: Session, application_id: int) -> JobApplication:
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise InterviewPrepServiceError(f"Application {application_id} not found.")
    if application.status == "Rejected":
        raise InterviewPrepServiceError("Can't generate interview prep for a Rejected application.")

    profile_content, variant_id, used_tailored_resume = resolve_grounding_profile(db, application)

    posting = application.posting
    jd_text = posting.job_description
    confirmed_stories = behavioral_story_service.list_stories(db, variant_id, confirmed_only=True)
    answer_target = get_or_create_settings(db).interview_prep_answer_target or 8

    try:
        general = _generate_general_prep(profile_content, jd_text)
        research = _light_company_research(db, posting.company_name_raw)
        publications = _research_company_publications(db, posting.company_name_raw)
        reputation_research = _research_company_reputation(db, posting.company_name_raw)
        company = _generate_company_prep(
            posting.company_name_raw, posting.job_title, jd_text, research, publications, reputation_research,
        )
        process_research = _research_interview_process(db, posting.company_name_raw, posting.job_title)
        predicted_rounds = _generate_predicted_rounds(
            posting.job_title, posting.company_name_raw, jd_text, process_research,
            profile_content, confirmed_stories, publications, answer_target, reputation_research,
        )
    except Exception as e:
        raise InterviewPrepServiceError(f"Interview prep generation failed: {e}") from e

    predicted_rounds["grounding_warnings"] = check_answer_grounding(profile_content, predicted_rounds)

    # Versioned, same pattern as ProfileVersion -- a regenerate used to
    # silently overwrite the previous prep in place with no way to
    # compare or recover it. Now every generation is a new row; the
    # previously-active one (if any) is deactivated, never deleted.
    db.query(InterviewPrep).filter(
        InterviewPrep.application_id == application.id, InterviewPrep.is_active == True  # noqa: E712
    ).update({"is_active": False})

    prep = InterviewPrep(
        application_id=application.id,
        general_prep_json=json.dumps(general),
        company_prep_json=json.dumps(company),
        process_research_json=json.dumps(process_research),
        predicted_rounds_json=json.dumps(predicted_rounds),
        is_active=True,
        generated_at=utcnow(),
    )
    db.add(prep)
    db.commit()

    log_activity(
        db,
        f"Generated interview prep for '{posting.job_title}' at {posting.company_name_raw}"
        + (" grounded in the tailored resume" if used_tailored_resume else " grounded in the base profile")
        + (", with live company + process research" if research or process_research.get("summary")
           else ", JD-only -- no research configured")
        + (f". {len(predicted_rounds['grounding_warnings'])} grounding warning(s)."
           if predicted_rounds["grounding_warnings"] else "."),
        "INFO",
    )

    db.refresh(application)
    return application


def list_interview_prep_versions(db: Session, application_id: int) -> list[InterviewPrep]:
    return (
        db.query(InterviewPrep)
        .filter(InterviewPrep.application_id == application_id)
        .order_by(InterviewPrep.generated_at.desc())
        .all()
    )


def restore_interview_prep_version(db: Session, prep_id: int) -> InterviewPrep:
    """Makes an older version active again -- deactivate-and-flip, same
    mechanics as generate_interview_prep switching versions, just
    without generating anything new. The version being replaced stays
    in history; nothing is ever deleted by this."""
    target = db.query(InterviewPrep).filter(InterviewPrep.id == prep_id).first()
    if not target:
        raise InterviewPrepServiceError(f"Interview prep version {prep_id} not found.")
    if target.is_active:
        return target

    db.query(InterviewPrep).filter(
        InterviewPrep.application_id == target.application_id, InterviewPrep.is_active == True  # noqa: E712
    ).update({"is_active": False})
    target.is_active = True
    db.commit()
    db.refresh(target)

    log_activity(
        db, f"Restored an earlier interview prep version (generated {target.generated_at:%Y-%m-%d %H:%M} UTC).", "INFO"
    )
    return target


def add_networking_insight_to_round(db: Session, application_id: int, round_name: str, insight_text: str) -> InterviewPrep:
    """Closes the loop a real conversation with a discovered contact
    (see contact_discovery_service.py) can open: something a real person
    told you about a specific round is exactly the kind of grounding
    _generate_round_qa already tries to produce from web research, just
    from a source no search API can reach. Appends the insight to that
    round's prep_focus with a dated source tag (never rewrites what's
    already there) and creates a new version -- same deactivate-and-flip
    mechanics as generate_interview_prep and restore_interview_prep_version,
    nothing is ever overwritten in place."""
    insight_text = (insight_text or "").strip()
    if not insight_text:
        raise InterviewPrepServiceError("Insight text can't be empty.")

    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise InterviewPrepServiceError(f"Application {application_id} not found.")
    current = application.active_interview_prep
    if not current or not current.predicted_rounds_json:
        raise InterviewPrepServiceError("Generate interview prep first -- there's no round to attach this to yet.")

    predicted_rounds = json.loads(current.predicted_rounds_json)
    rounds = predicted_rounds.get("rounds", [])
    target_round = next((r for r in rounds if r.get("round_name") == round_name), None)
    if not target_round:
        raise InterviewPrepServiceError(f"Round '{round_name}' not found in the current prep.")

    dated_note = f"{insight_text} (Source: networking conversation, {utcnow():%Y-%m-%d})"
    target_round.setdefault("prep_focus", []).append(dated_note)

    current.is_active = False
    new_version = InterviewPrep(
        application_id=application_id,
        general_prep_json=current.general_prep_json,
        company_prep_json=current.company_prep_json,
        process_research_json=current.process_research_json,
        predicted_rounds_json=json.dumps(predicted_rounds),
        is_active=True,
        generated_at=utcnow(),
    )
    db.add(new_version)
    db.commit()
    db.refresh(new_version)

    log_activity(db, f"Added a networking insight to the '{round_name}' round.", "INFO")
    return new_version
