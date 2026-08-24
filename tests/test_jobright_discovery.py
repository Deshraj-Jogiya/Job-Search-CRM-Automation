"""Parsing logic for JobRight's public daily job-list table -- pure,
no network call. The real table (captured directly from
jobright-ai/Daily-H1B-Jobs-In-Tech during research) uses a bold-linked
company name only on a role's first row, with a "↳" continuation marker
reusing that same company on subsequent rows -- both cases, plus the
header/separator rows that must NOT be mistaken for a company, are
covered here.
"""

from app.services.jobright_discovery import parse_matching_companies

_SAMPLE_TABLE = """
| Company | Job Title |  Level  | Location | H1B status | Link | Date Posted |
| ------- | --------- |  -----  | -------- | ---------- | ---- | ----------- |
| **[Vanguard](http://investor.vanguard.com/corporate-portal)** | Manager, Data Science |  Mid-Level,Senior  | Charlotte, NC | 🏅 | [apply](https://jobright.ai/jobs/info/1) | 2026-05-06 |
| **[Anthropic](https://www.anthropic.com)** | GTM Engineer |  Mid-Level,Senior  | San Francisco, CA | 🏅 | [apply](https://jobright.ai/jobs/info/2) | 2026-05-06 |
| ↳ | Data Scientist, Developer Productivity |  Senior  | San Francisco, CA | 🏅 | [apply](https://jobright.ai/jobs/info/3) | 2026-05-06 |
| **[AECOM](http://www.aecom.com/)** | Dams / Reservoir Engineering |  Senior  | New York, NY | 🏅 | [apply](https://jobright.ai/jobs/info/4) | 2026-05-06 |
"""


def test_extracts_company_from_a_bold_linked_row_matching_a_keyword():
    companies = parse_matching_companies(_SAMPLE_TABLE, ["Data Science"])
    assert "Vanguard" in companies


def test_continuation_row_attributes_to_the_company_above_it():
    companies = parse_matching_companies(_SAMPLE_TABLE, ["Data Scientist"])
    assert "Anthropic" in companies


def test_non_matching_title_is_excluded():
    companies = parse_matching_companies(_SAMPLE_TABLE, ["Data Scientist", "Data Science"])
    assert "AECOM" not in companies  # "Dams / Reservoir Engineering" matches neither keyword


def test_header_and_separator_rows_never_become_a_company():
    companies = parse_matching_companies(_SAMPLE_TABLE, ["Company", "Job Title"])
    assert companies == []


def test_deduplicates_a_company_appearing_via_multiple_matching_rows():
    table = (
        "| **[Anthropic](https://www.anthropic.com)** | Data Engineer | Senior | SF | 🏅 | [apply](x) | 2026-05-06 |\n"
        "| ↳ | Data Scientist | Senior | SF | 🏅 | [apply](x) | 2026-05-06 |\n"
    )
    companies = parse_matching_companies(table, ["Data Engineer", "Data Scientist"])
    assert companies.count("Anthropic") == 1


def test_empty_keyword_list_returns_no_companies():
    assert parse_matching_companies(_SAMPLE_TABLE, []) == []
