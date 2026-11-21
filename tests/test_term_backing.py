"""A posting term worded differently from the profile is backed by the work it names, never reported as a gap: an A/B test
or a baseline z-test is causal inference, MongoDB is NoSQL (owner, 2026-11-21: "if the things are in my profile and the JD
uses different words they should not be flagged")."""
import json

from app.services import fit_service, resume_ats

PROFILE = {
    "skills": {"ml": ["A/B Testing & Hypothesis Testing (Z-Test)"], "data": ["MongoDB"]},
    "experience": [{"role": "Engineer", "company": "Acme", "bullets": ["Built a streaming pipeline using Node.js and MongoDB."]}],
    "projects": [{"name": "Fairness Audits", "bullets": ["Added a 7-day approval-gap z-test against baseline that alerts at 3 standard errors."]}],
}
JD = "You have experience with experimentation, causal inference, or observational analysis, and NoSQL databases."


def test_resume_check_backs_reworded_terms():
    report = resume_ats.exact_phrase_report("", JD, json.dumps(PROFILE))
    assert report["not_in_profile"] == []
    backed = {r["term"]: r["backed_by"] for r in report["reword"]}
    assert backed["causal inference"] == "evidence" and backed["NoSQL"] == "evidence"


def test_no_evidence_still_reported():
    report = resume_ats.exact_phrase_report("", JD, json.dumps({"skills": {"x": ["Excel"]}}))
    assert {"causal inference", "NoSQL"} <= set(report["not_in_profile"])


def test_fit_score_counts_reworded_terms():
    overlap = fit_service.skill_overlap(PROFILE, JD)
    matched = {m["term"]: m["evidence"] for m in overlap["matched"]}
    assert matched["Causal inference"] == "Project: Fairness Audits"
    assert matched["NoSQL"] == "Engineer at Acme"
    assert not overlap["missing"]
