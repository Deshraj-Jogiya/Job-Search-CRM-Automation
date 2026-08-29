"""_restore_static_project_fields: the tailoring LLM's response schema
only asks for {name, bullets, technologies} for each project, so a
static field like github_url that exists on the real profile entry but
isn't part of that schema silently disappears from the tailored output
otherwise -- not a fabrication risk, just a field nothing asked the LLM
to carry through. Restored by matching on project name after tailoring,
since a repo URL has nothing to tailor about it."""

from app.services.tailoring_service import _restore_static_project_fields


def test_restores_github_url_by_matching_project_name():
    original = [{"name": "CurioSync", "bullets": ["..."], "github_url": "https://github.com/x/curiosync"}]
    tailored = [{"name": "CurioSync", "bullets": ["rewritten bullet"], "technologies": ["Python"]}]
    result = _restore_static_project_fields(original, tailored)
    assert result[0]["github_url"] == "https://github.com/x/curiosync"
    assert result[0]["bullets"] == ["rewritten bullet"]


def test_project_with_no_original_url_gets_none_added():
    original = [{"name": "Side Project", "bullets": ["..."]}]
    tailored = [{"name": "Side Project", "bullets": ["rewritten"]}]
    result = _restore_static_project_fields(original, tailored)
    assert "github_url" not in result[0]


def test_project_dropped_by_tailoring_is_simply_absent_no_crash():
    original = [
        {"name": "Kept Project", "github_url": "https://github.com/x/kept"},
        {"name": "Dropped Project", "github_url": "https://github.com/x/dropped"},
    ]
    tailored = [{"name": "Kept Project", "bullets": ["..."]}]
    result = _restore_static_project_fields(original, tailored)
    assert len(result) == 1
    assert result[0]["github_url"] == "https://github.com/x/kept"


def test_does_not_overwrite_a_url_the_llm_already_echoed_back():
    original = [{"name": "P", "github_url": "https://github.com/x/original"}]
    tailored = [{"name": "P", "bullets": ["..."], "github_url": "https://github.com/x/original"}]
    result = _restore_static_project_fields(original, tailored)
    assert result[0]["github_url"] == "https://github.com/x/original"
