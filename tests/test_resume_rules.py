from datetime import date

from app.services.resume_rules import (
    check_bare_percentage,
    check_years_claim,
    classify_role,
    detect_concurrent_overlaps,
    filter_skills,
    hedge_unverified_metrics,
    parse_date_range,
    select_projects_for_variant,
    total_experience_months,
    work_authorization_line,
)

_NOW = date(2026, 9, 11)


class TestParseDateRange:
    def test_month_year_range(self):
        assert parse_date_range("Jun 2020 - Dec 2021") == (date(2020, 6, 1), date(2021, 12, 1))

    def test_present_end(self):
        assert parse_date_range("Jan 2022 - Present", now=_NOW) == (date(2022, 1, 1), date(2026, 9, 1))

    def test_year_only_range(self):
        assert parse_date_range("2020 - 2021") == (date(2020, 1, 1), date(2021, 1, 1))

    def test_unparseable_returns_none(self):
        assert parse_date_range("sometime last year") is None

    def test_empty_returns_none(self):
        assert parse_date_range("") is None


class TestTotalExperienceMonths:
    def test_five_calendar_years_but_38_actual_months_never_overclaims(self):
        # ~5 calendar years apart (2021-2026) but the role only ran 38
        # actual months (Aug 2021 - Sep 2024, inclusive) -- the claim
        # must reflect the real 38, not the 5-year calendar span.
        experience = [{"date": "Aug 2021 - Sep 2024"}]
        months = total_experience_months(experience, now=_NOW)
        assert months == 38
        summary = "5+ years of experience building data platforms."
        assert check_years_claim(summary, months) == ["5+ years"]

    def test_overlapping_roles_counted_once(self):
        experience = [
            {"date": "Jan 2020 - Dec 2022"},  # 36 months
            {"date": "Jun 2021 - Jun 2021"},   # fully inside the first -- 0 extra
        ]
        assert total_experience_months(experience, now=_NOW) == 36

    def test_non_overlapping_roles_summed(self):
        experience = [
            {"date": "Jan 2018 - Jan 2019"},   # 13 months, inclusive
            {"date": "Jan 2020 - Jan 2021"},   # 13 months, inclusive
        ]
        assert total_experience_months(experience, now=_NOW) == 26

    def test_unparseable_entry_excluded_not_assumed_zero(self):
        experience = [
            {"date": "Jan 2020 - Jan 2021"},  # 13 real months, inclusive
            {"date": "a while back"},          # excluded, not counted as anything
        ]
        assert total_experience_months(experience, now=_NOW) == 13


class TestCheckYearsClaim:
    def test_accurate_claim_passes(self):
        assert check_years_claim("3 years of experience", 36) == []

    def test_inflated_claim_is_flagged(self):
        assert check_years_claim("10+ years of experience", 36) == ["10+ years"]

    def test_omitted_claim_is_fine(self):
        assert check_years_claim("Built large-scale data platforms.", 36) == []


class TestClassifyRole:
    def test_recent_long_role_is_experience(self):
        entry = {"date": "Jan 2022 - Present"}
        assert classify_role(entry, now=_NOW) == "EXPERIENCE"

    def test_short_role_is_earlier(self):
        entry = {"date": "Jan 2024 - Mar 2024"}  # 2 months, below the 6-month floor
        assert classify_role(entry, now=_NOW) == "EARLIER"

    def test_old_role_is_earlier_even_if_long(self):
        entry = {"date": "Jan 2015 - Jan 2018"}  # long, but ended >36 months ago
        assert classify_role(entry, now=_NOW) == "EARLIER"

    def test_fellowship_is_credential_regardless_of_duration(self):
        entry = {"role": "Data Fellowship", "company": "Acme Fellows Program", "date": "Jan 2022 - Present"}
        assert classify_role(entry, now=_NOW) == "CREDENTIAL"

    def test_explicit_entry_type_overrides_heuristic(self):
        entry = {"role": "Software Engineer", "entry_type": "credential", "date": "Jan 2022 - Present"}
        assert classify_role(entry, now=_NOW) == "CREDENTIAL"

    def test_unparseable_date_defaults_to_experience_not_earlier(self):
        entry = {"date": "unclear"}
        assert classify_role(entry, now=_NOW) == "EXPERIENCE"


class TestConcurrentOverlaps:
    def test_overlapping_roles_flag_the_later_listed_one(self):
        experience = [
            {"date": "Jan 2022 - Dec 2022"},
            {"date": "Feb 2022 - Present"},  # fellowship overlapping the job above
        ]
        assert detect_concurrent_overlaps(experience, now=_NOW) == {1}

    def test_non_overlapping_roles_not_flagged(self):
        experience = [
            {"date": "Jan 2018 - Jan 2019"},
            {"date": "Jan 2020 - Jan 2021"},
        ]
        assert detect_concurrent_overlaps(experience, now=_NOW) == set()


class TestFilterSkills:
    def test_skill_in_a_bullet_is_kept(self):
        skills = {"Languages": ["Python", "Scala"]}
        experience = [{"bullets": ["Built pipelines in Python."]}]
        filtered, dropped = filter_skills(skills, experience, [], "", )
        assert filtered == {"Languages": ["Python"]}
        assert any("Scala" in d for d in dropped)

    def test_skill_in_summary_is_kept(self):
        skills = {"Cloud": ["AWS"]}
        filtered, dropped = filter_skills(skills, [], [], "Experienced with AWS.")
        assert filtered == {"Cloud": ["AWS"]}

    def test_skill_in_project_bullet_is_kept(self):
        skills = {"Tools": ["Kafka"]}
        projects = [{"bullets": ["Used Kafka for streaming."]}]
        filtered, dropped = filter_skills(skills, [], projects, "")
        assert filtered == {"Tools": ["Kafka"]}

    def test_dropped_skill_is_logged_with_a_reason(self):
        skills = {"Languages": ["Rust"]}
        filtered, dropped = filter_skills(skills, [], [], "")
        assert filtered == {}
        assert len(dropped) == 1
        assert "Rust" in dropped[0]


class TestHedgeUnverifiedMetrics:
    def test_unverified_percentage_gets_hedged(self):
        result = hedge_unverified_metrics("Reduced latency by 47%.")
        assert "roughly 47%" in result

    def test_already_hedged_not_double_hedged(self):
        result = hedge_unverified_metrics("Reduced latency by roughly 47%.")
        assert result.count("roughly") == 1

    def test_scope_counts_are_not_percentages_so_untouched(self):
        result = hedge_unverified_metrics("Processed 500GB/day across 12 sources.")
        assert result == "Processed 500GB/day across 12 sources."

    def test_negative_percentage_hedges_before_the_sign_not_after(self):
        # Found via a real bullet while building the Part F golden
        # fixture: "(-35% silent drift)" must not hedge to
        # "(-roughly 35%...)" -- the sign belongs with the number.
        result = hedge_unverified_metrics("Monitors feature drift (-35% silent drift).")
        assert "roughly -35%" in result
        assert "-roughly" not in result


class TestCheckBarePercentage:
    def test_unhedged_percentage_is_flagged(self):
        assert check_bare_percentage("Cut runtime by 40%.") == ["40%"]

    def test_hedged_percentage_is_not_flagged(self):
        assert check_bare_percentage("Cut runtime by roughly 40%.") == []

    def test_negative_percentage_includes_the_sign_in_the_claim(self):
        assert check_bare_percentage("Reduced drift by -35%.") == ["-35%"]


_PROJECT_SELECTION_TEST_CONFIG = {
    "projects_by_variant": {
        "max_projects": 3,
        "bullets_per_project_min": 1,
        "bullets_per_project_max": 2,
        "variants": {
            "data_engineering": ["career_pilot", "talentvenue_eventintel", "ai_model_observability"],
        },
    },
}


class TestSelectProjectsForVariant:
    # A self-contained config, deliberately NOT the real
    # config/resume_rules.yaml default -- that file's real slug list is
    # derived from the live profile's actual (long, descriptive)
    # project titles and legitimately changes whenever those titles do
    # (see that file's own comment), so these tests shouldn't be
    # coupled to whatever it currently says.
    def test_selects_real_projects_in_configured_order(self):
        projects = [
            {"name": "Career Pilot"},
            {"name": "Sales RFM Segmentation"},
            {"name": "TalentVenue EventIntel"},
            {"name": "AI Model Observability"},
        ]
        selected = select_projects_for_variant(projects, "data_engineering", config=_PROJECT_SELECTION_TEST_CONFIG)
        names = [p["name"] for p in selected]
        assert names == ["Career Pilot", "TalentVenue EventIntel", "AI Model Observability"]

    def test_missing_project_is_skipped_not_invented(self):
        projects = [{"name": "Career Pilot"}]
        selected = select_projects_for_variant(projects, "data_engineering", config=_PROJECT_SELECTION_TEST_CONFIG)
        assert [p["name"] for p in selected] == ["Career Pilot"]

    def test_unknown_variant_returns_empty(self):
        result = select_projects_for_variant(
            [{"name": "Career Pilot"}], "nonexistent_variant", config=_PROJECT_SELECTION_TEST_CONFIG
        )
        assert result == []


class TestWorkAuthorizationLine:
    def test_defaults_to_none(self):
        assert work_authorization_line() is None
