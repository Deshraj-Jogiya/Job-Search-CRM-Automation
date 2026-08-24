"""Mechanical (non-LLM) EEO/application-preference answer matching --
app/services/autofill/common_answers.py. Pure logic, no browser/LLM
needed."""

from app.services.autofill.common_answers import (
    is_referral_source_question,
    mechanical_common_answer,
    referral_source_answer,
)


def test_eeo_field_matched_by_label():
    profile = {"eeo": {"gender": "Male"}}
    assert mechanical_common_answer("Gender", profile) == "Male"


def test_unrecognized_label_returns_none():
    profile = {"eeo": {"gender": "Male"}}
    assert mechanical_common_answer("What's your favorite color?", profile) is None


def test_missing_profile_section_returns_none():
    assert mechanical_common_answer("Gender", {}) is None


def test_salary_combines_minimum_and_negotiable():
    profile = {"application_preferences": {"salary_minimum": "$120,000+", "salary_negotiable": True}}
    assert mechanical_common_answer("What are your salary expectations?", profile) == "$120,000+ minimum, negotiable"


def test_salary_minimum_only():
    profile = {"application_preferences": {"salary_minimum": "$120,000+"}}
    assert mechanical_common_answer("Desired salary?", profile) == "$120,000+ minimum"


def test_salary_negotiable_only():
    profile = {"application_preferences": {"salary_negotiable": True}}
    assert mechanical_common_answer("Compensation expectation?", profile) == "negotiable"


def test_salary_unset_returns_none():
    profile = {"application_preferences": {}}
    assert mechanical_common_answer("What are your salary expectations?", profile) is None


def test_referral_source_detection():
    assert is_referral_source_question("How did you hear about this position?")
    assert not is_referral_source_question("What is your gender?")


def test_referral_source_answer_by_intake_source():
    assert referral_source_answer("linkedin") == "LinkedIn"
    assert referral_source_answer("greenhouse") == "Job Board"
    assert referral_source_answer("manual") == "Job Board"
