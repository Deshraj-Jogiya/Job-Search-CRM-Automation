"""Monthly upgrade check (app/services/upgrade_service.py): the five checks, their data gates, the rule that an
ended project is only ever updated with work from inside its dates, and that every profile change waits for
approval as a pending version."""

import json
from datetime import datetime, timedelta

import pytest
from jinja2 import Environment, FileSystemLoader

from app import models
from app.services import profile_service, upgrade_service
from app.services.upgrade_service import UpgradeServiceError, parse_date_range
from tests.conftest import make_company, make_posting, make_variant

env = Environment(loader=FileSystemLoader("app/templates"))
NOW = datetime(2026, 10, 22, 12, 0)

PROFILE = {
    "name": "Test Candidate",
    "title": "Data Engineer",
    "skills": {"Languages": ["Python", "SQL"], "Tools": ["Kafka", "Airflow"]},
    "experience": [
        {"role": "Data Engineer", "company": "Acme", "date": "May 2026 – Present",
         "bullets": ["Built Airflow pipelines in Python and SQL."]},
        {"role": "Data Analyst", "company": "Old Co", "date": "Jan 2021 – Mar 2022", "bullets": ["Reports."]},
    ],
    "projects": [
        {"name": "Live Project", "date": "Feb 2026 – Present", "github_url": "https://github.com/me/live",
         "technologies": ["Python"], "bullets": ["Cut runtime by 30%."]},
        {"name": "Ended Project", "date": "Apr 2025 – Nov 2026", "github_url": "https://github.com/me/ended",
         "technologies": ["Python"], "bullets": ["A thing."]},
        {"name": "Old Project", "date": "Apr 2024 – Jun 2024", "github_url": "https://github.com/me/old",
         "technologies": ["R"], "bullets": ["Old."]},
        {"name": "Team Project", "date": "Feb 2026 – Present", "github_url": "https://github.com/me/team",
         "confidential": True, "bullets": []},
    ],
}


class FakeGitHub:
    def __init__(self, commits=3, languages=("Python", "Go", "HTML"), readme="Runs 1,145 tests. 30% faster."):
        self.calls = []
        self.commits, self._languages, self._readme = commits, list(languages), readme

    def commit_count(self, owner, repo, since, until):
        self.calls.append((repo, since, until))
        return self.commits

    def languages(self, owner, repo):
        return self._languages

    def readme(self, owner, repo):
        return self._readme


def _profile_db(db, content=PROFILE, updated=datetime(2026, 9, 1)):
    variant = make_variant(db, content=json.loads(json.dumps(content)))
    version = db.query(models.ProfileVersion).filter_by(variant_id=variant.id).one()
    version.created_at = updated
    db.commit()
    return variant


# ---- dates -------------------------------------------------------------------------------------------------

def test_parse_date_range_handles_present_ranges_and_single_months():
    assert parse_date_range("Feb 2026 – Present") == (datetime(2026, 2, 1), None, True)
    start, end, current = parse_date_range("Apr 2025 – Sep 2026")
    assert start == datetime(2025, 4, 1) and end.date().isoformat() == "2026-09-30" and not current
    start, end, _ = parse_date_range("Sep 2026")
    assert start == datetime(2026, 9, 1) and end.month == 9
    assert parse_date_range("2022 - Present")[2] is True
    assert parse_date_range("") == (None, None, False)


# ---- check 2: repos ----------------------------------------------------------------------------------------

def test_repo_check_counts_only_work_inside_each_projects_dates():
    gh = FakeGitHub()
    result = upgrade_service.repo_activity(PROFILE, datetime(2026, 9, 1), gh, NOW)
    by_repo = {repo: (since, until) for repo, since, until in gh.calls}

    assert "team" not in by_repo                      # confidential projects are never looked at
    assert "old" not in by_repo                       # ended before the last profile update
    assert result["ended_projects_not_checked"] == 1
    assert by_repo["live"] == (datetime(2026, 9, 1), None)
    since, until = by_repo["ended"]
    assert since == datetime(2026, 9, 1) and until.date().isoformat() == "2026-11-30"  # stops at the end date


def test_ended_project_never_gets_languages_or_present():
    result = upgrade_service.repo_activity(PROFILE, datetime(2026, 9, 1), FakeGitHub(), NOW)
    items = {i["name"]: i for i in result["items"]}
    assert items["Live Project"]["new_languages"] == ["Go"]   # HTML skipped, Python already listed
    assert items["Ended Project"]["new_languages"] == []
    assert items["Ended Project"]["is_current"] is False


def test_readme_numbers_missing_from_bullets_are_listed():
    result = upgrade_service.repo_activity(PROFILE, datetime(2026, 9, 1), FakeGitHub(), NOW)
    live = next(i for i in result["items"] if i["name"] == "Live Project")
    assert "1,145 tests" in live["new_numbers"]
    assert not any(n.startswith("30") for n in live["new_numbers"])  # already in a bullet


def test_projects_without_commits_are_not_reported():
    result = upgrade_service.repo_activity(PROFILE, datetime(2026, 9, 1), FakeGitHub(commits=0), NOW)
    assert result["items"] == []


# ---- check 3: prompts --------------------------------------------------------------------------------------

def test_prompts_only_for_current_roles_and_public_current_projects():
    prompts = upgrade_service.accomplishment_prompts(PROFILE, datetime(2026, 9, 1))
    targets = [p["target"] for p in prompts]
    assert targets == ["experience:0", "project:0"]
    assert "September 2026" in prompts[0]["questions"][0]


def test_answer_becomes_a_pending_bullet_in_his_words(db):
    _profile_db(db)
    version = upgrade_service.draft_answer(db, "experience:0", "  cut validation time from 40 to 12 minutes ")
    assert version.is_active is False and version.source == "upgrade_check"
    content = json.loads(version.content_json)
    assert content["experience"][0]["bullets"][-1] == "Cut validation time from 40 to 12 minutes."
    # the active profile is untouched until he approves
    assert "Cut validation" not in json.dumps(profile_service.get_default_profile_content(db))
    pending = profile_service.get_variant_display_data(db, version.variant_id)["pending_versions"]
    assert [p.id for p in pending] == [version.id]


@pytest.mark.parametrize("target,answer", [("experience:0", "   "), ("experience:9", "x"), ("bogus:0", "x"),
                                           ("experience:0", "x" * 401)])
def test_bad_answers_are_refused(db, target, answer):
    _profile_db(db)
    with pytest.raises(UpgradeServiceError):
        upgrade_service.draft_answer(db, target, answer)


def test_languages_can_be_drafted_for_a_current_project_only(db):
    _profile_db(db)
    version = upgrade_service.draft_languages(db, 0, ["Go", "Python"])
    assert json.loads(version.content_json)["projects"][0]["technologies"] == ["Python", "Go"]
    with pytest.raises(UpgradeServiceError, match="ended"):
        upgrade_service.draft_languages(db, 1, ["Go"])


# ---- check 1: market ---------------------------------------------------------------------------------------

def _postings(db, n, description, title="Data Engineer"):
    company = make_company(db, name=f"Co {title}")
    for i in range(n):
        make_posting(db, company, job_title=title, job_url=f"https://x/{title}/{i}", job_description=description,
                     first_seen_at=NOW - timedelta(days=5))


def test_market_check_stays_quiet_below_the_minimum(db):
    _postings(db, upgrade_service.MIN_POSTINGS - 1, "Requirements: Python, Snowflake")
    result = upgrade_service.market_demand(db, PROFILE, NOW)
    assert result["ready"] is False and result["skills"] == [] and result["target_postings"] == 29


def test_market_check_ranks_required_skills_against_evidence(db):
    _postings(db, 30, "Requirements:\nPython, SQL, Kafka and Snowflake\nNice to have:\nTerraform")
    _postings(db, 10, "Requirements: Excel", title="Registered Nurse")  # not a target role
    result = upgrade_service.market_demand(db, PROFILE, NOW)
    skills = {s["term"]: s for s in result["skills"]}

    assert result["ready"] and result["target_postings"] == 30
    assert skills["Python"]["level"] == "work"
    assert skills["Kafka"]["level"] == "listed"          # only in the skills list
    assert skills["Snowflake"]["level"] == "none"
    assert "Terraform" not in skills and "Excel" not in skills
    assert {g["term"] for g in result["gaps"]} == {"Kafka", "Snowflake"}


# ---- checks 4 and 5 ----------------------------------------------------------------------------------------

def test_outcomes_wait_for_enough_sent_and_replies(db):
    result = upgrade_service.outcome_learning(db)
    assert result["ready"] is False and result["sent"] == 0 and result["min_sent"] > 0


def _debrief(db, app_id, issues):
    s = models.MockInterviewSession(application_id=app_id, round_name="Technical", status="completed",
                                    debrief_json=json.dumps({"areas_to_improve": [{"issue": i} for i in issues]}),
                                    ended_at=NOW)
    db.add(s)
    db.commit()


def test_interview_themes_need_three_debriefs_and_two_sessions(db):
    from tests.conftest import make_application
    app = make_application(db, make_posting(db, make_company(db)))
    _debrief(db, app.id, ["SQL window function question left unanswered"])
    _debrief(db, app.id, ["Unanswered SQL window function question"])
    assert upgrade_service.interview_patterns(db)["ready"] is False
    _debrief(db, app.id, ["Talked too fast during the system design walkthrough"])
    result = upgrade_service.interview_patterns(db)
    assert result["ready"] and len(result["themes"]) == 1
    assert len(result["themes"][0]["sessions"]) == 2


# ---- running ----------------------------------------------------------------------------------------------

def test_run_check_stores_a_report_and_the_first_one_is_quarterly(db):
    _profile_db(db)
    row = upgrade_service.run_check(db, FakeGitHub(), NOW)
    report = json.loads(row.report_json)
    assert row.is_quarterly and "quarter" in report
    assert upgrade_service.action_count(report) == len(report["repos"]["items"]) + len(report["prompts"])

    second = upgrade_service.run_check(db, FakeGitHub(), NOW + timedelta(days=30))
    assert not second.is_quarterly
    third = upgrade_service.run_check(db, FakeGitHub(), NOW + timedelta(days=90))
    assert third.is_quarterly


def test_run_check_never_changes_the_active_profile(db):
    _profile_db(db)
    before = profile_service.get_default_profile_content(db)
    upgrade_service.run_check(db, FakeGitHub(), NOW)
    assert profile_service.get_default_profile_content(db) == before
    assert db.query(models.ProfileVersion).count() == 1


def test_run_if_due_waits_thirty_days(db, monkeypatch):
    _profile_db(db)
    monkeypatch.setattr(upgrade_service, "GitHubClient", lambda: FakeGitHub())
    assert upgrade_service.run_if_due(db) is not None
    assert upgrade_service.run_if_due(db) is None


def test_run_check_without_a_profile_explains(db):
    with pytest.raises(UpgradeServiceError, match="profile"):
        upgrade_service.run_check(db, FakeGitHub(), NOW)


# ---- page ------------------------------------------------------------------------------------------------

def _render(**extra):
    ctx = {"csrf_token": "t", "static_version": "0", "is_authenticated": True, "message": None, "error": None,
           "run_every_days": 30, "max_answer_chars": 400}
    ctx.update(extra)
    return env.get_template("upgrades.html").render(**ctx)


def test_page_before_any_run_offers_the_button():
    html = _render(row=None, report=None, count=0)
    assert "Run the check now" in html and "Not run yet" in html and 'href="/upgrades"' in html


def test_page_shows_each_check_with_its_gate(db):
    _profile_db(db)
    row = upgrade_service.run_check(db, FakeGitHub(), NOW)
    html = _render(row=row, report=json.loads(row.report_json), count=2)
    assert "Waiting for data: 0 of 30 postings" in html
    assert "Live Project" in html and "1,145 tests" in html and "Draft adding it" in html
    assert "never reopened" in html
    assert 'name="target" value="experience:0"' in html
    assert "Not enough data yet" in html and "0 of 3 mock-interview debriefs" in html
    assert "This quarter" in html


def test_routes_redirect_with_messages(db):
    from app.routers import upgrades as router
    _profile_db(db)
    r = router.answer(target="experience:0", answer="Shipped the thing.", db=db)
    assert r.status_code == 303 and "message=" in r.headers["location"]
    r = router.answer(target="experience:0", answer="", db=db)
    assert "error=" in r.headers["location"]
    r = router.languages(project_index=1, languages="Go", db=db)
    assert "error=" in r.headers["location"]
