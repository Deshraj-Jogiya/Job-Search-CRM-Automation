"""parse_json_response's truncation recovery -- real LLM responses get cut
off mid-JSON any time real content legitimately outgrows a fixed
max_tokens ceiling (an uncapped round count, an uncapped follow-up list,
a schema that gained a field). This used to raise and lose an entire
generation; now it salvages the longest valid JSON prefix and drops only
the truncated tail. See app/services/llm/__init__.py's docstring for the
full story -- this happened for real, repeatedly, in
interview_prep_service.py before this existed."""

import json

import pytest

from app.services.llm import parse_json_response


def test_parses_normal_json_unchanged():
    assert parse_json_response('{"a": 1, "b": [1, 2, 3]}') == {"a": 1, "b": [1, 2, 3]}


def test_strips_markdown_code_fences():
    raw = '```json\n{"a": 1}\n```'
    assert parse_json_response(raw) == {"a": 1}


def test_recovers_when_truncated_mid_string_inside_last_array_element():
    # Mirrors the real failure: "Unterminated string starting at..."
    raw = '{"qa_pairs": [{"q": "a"}, {"q": "b"}, {"q": "c and then it just cu'
    result = parse_json_response(raw)
    assert result == {"qa_pairs": [{"q": "a"}, {"q": "b"}]}


def test_recovers_when_truncated_right_after_a_comma():
    # Mirrors the real failure: "Expecting value: line N column M"
    raw = '{"qa_pairs": [{"q": "a"}], "other_possible_questions": ["x", "y",'
    result = parse_json_response(raw)
    assert result["qa_pairs"] == [{"q": "a"}]
    # the truncated trailing list is dropped entirely since it never
    # reached a safe closing point of its own
    assert "other_possible_questions" not in result or result["other_possible_questions"] == []


def test_recovers_dropping_only_the_incomplete_tail_field():
    raw = '{"rounds": [{"round_name": "Recruiter Screen"}, {"round_name": "Technical'
    result = parse_json_response(raw)
    assert result == {"rounds": [{"round_name": "Recruiter Screen"}]}


def test_braces_inside_string_values_dont_confuse_the_scanner():
    raw = '{"draft_answer": "I said \\"we handle {edge cases}\\" in the demo"}'
    assert parse_json_response(raw) == {"draft_answer": 'I said "we handle {edge cases}" in the demo'}


def test_genuinely_malformed_json_still_raises():
    with pytest.raises(json.JSONDecodeError):
        parse_json_response("not json at all, just prose")


def test_empty_string_raises():
    with pytest.raises(json.JSONDecodeError):
        parse_json_response("")


def test_top_level_array_truncation_recovers():
    raw = '[{"question": "one"}, {"question": "two"}, {"question": "thr'
    result = parse_json_response(raw)
    assert result == [{"question": "one"}, {"question": "two"}]
