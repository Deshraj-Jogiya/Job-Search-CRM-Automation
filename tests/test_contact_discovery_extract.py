"""Parsing + reason-computation logic for the Tavily Extract enrichment
pass added to contact discovery -- pure, no network call. The real
markdown-sectioned shape (captured directly from Tavily Extract against
a real LinkedIn profile during research) uses "## Section Name" headers;
compute_reason's job is to turn that + the candidate's own real profile
into a real, never-fabricated "why this contact" signal, matching the
mechanical-safeguard convention used elsewhere in this app (e.g.
check_answer_grounding) -- no LLM guessing here.
"""

from app.services.contact_discovery_service import compute_reason, parse_extract_sections

_REAL_SAMPLE_EXTRACT = """# Joshua Winter
**Anthropic**
New York, New York, United States, US

## About
As a true partner in cultivating top talent, I work with organizational leadership and...

## Experience
N/A

## Education
N/A

## Activity
- Our AI Reliability team (AIRE) is hiring Software Engineers, Production Engineers, and Site Reliability Engineers!
Joshua Winter shared this
[View Post](https://www.linkedin.com/posts/joshuawinter_staff-software-engineer-ai-reliability-activity)
"""

_PROFILE = {
    "education": [{"school": "Arizona State University", "degree": "MS", "date": "2024"}],
    "experience": [{"company": "Objectways Technologies LLC", "role": "Data Associate"}],
}


def test_parses_real_extract_sections():
    sections = parse_extract_sections(_REAL_SAMPLE_EXTRACT)
    assert sections["about"].startswith("As a true partner")
    assert sections["experience"] == "N/A"
    assert "hiring" in sections["activity"].lower()


def test_missing_raw_content_returns_empty_dict():
    assert parse_extract_sections(None) == {}
    assert parse_extract_sections("") == {}


def test_content_with_no_headers_returns_empty_dict():
    assert parse_extract_sections("just some plain text, no sections") == {}


def test_compute_reason_finds_a_real_hiring_post():
    sections = parse_extract_sections(_REAL_SAMPLE_EXTRACT)
    reason = compute_reason(sections, _PROFILE)
    assert reason is not None
    assert "hiring" in reason.lower()


def test_compute_reason_finds_shared_school_when_no_hiring_post():
    sections = {"about": "Proud alum of Arizona State University, now building things.", "activity": ""}
    reason = compute_reason(sections, _PROFILE)
    assert reason == "Also attended Arizona State University"


def test_compute_reason_finds_shared_employer_when_no_school_or_hiring():
    sections = {"experience": "Previously at Objectways Technologies LLC as an analyst.", "activity": ""}
    reason = compute_reason(sections, _PROFILE)
    assert reason == "Previously worked at Objectways Technologies LLC"


def test_compute_reason_hiring_post_takes_priority_over_school_match():
    sections = {
        "about": "Arizona State University grad.",
        "activity": "We're hiring across the team, apply now!",
    }
    reason = compute_reason(sections, _PROFILE)
    assert "hiring" in reason.lower()


def test_compute_reason_returns_none_when_genuinely_nothing_found():
    sections = {"about": "I like long walks and coffee.", "activity": "Reposted an article about AI."}
    assert compute_reason(sections, _PROFILE) is None


def test_compute_reason_never_fabricates_without_a_profile():
    sections = {"activity": "We're hiring!"}
    reason = compute_reason(sections, None)
    assert reason is not None  # the hiring signal doesn't need a profile to fire

    sections_no_hiring = {"about": "Enjoys hiking."}
    assert compute_reason(sections_no_hiring, None) is None
