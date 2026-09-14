from datetime import date

from app.services.resume_rules import (
    check_bare_percentage,
    check_years_claim,
    classify_role,
    detect_concurrent_overlaps,
    filter_skills,
    hedge_unverified_metrics,
    parse_date_range,
    select_certifications_for_resume,
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

    def test_rounds_to_nearest_year_not_floor(self):
        # 35 real months is 2.9166 years -- "3+ years" is ordinary,
        # conventional resume phrasing for that, not an inflated claim.
        # A floor-based check previously flagged this as fabrication;
        # rounding to the nearest year is what a human actually means.
        assert check_years_claim("3+ years of experience", 35) == []

    def test_rounding_still_catches_a_real_inflation(self):
        # 35 months rounds to 3 -- claiming "5+" off that is still a
        # real, meaningful overclaim, not just rounding noise.
        assert check_years_claim("5+ years of experience", 35) == ["5+ years"]


class TestClassifyRole:
    def test_recent_long_role_is_experience(self):
        entry = {"date": "Jan 2022 - Present"}
        assert classify_role(entry, now=_NOW) == "EXPERIENCE"

    def test_short_role_is_earlier(self):
        entry = {"date": "Jan 2024 - Mar 2024"}  # 2 months, below the 6-month floor
        assert classify_role(entry, now=_NOW) == "EARLIER"

    def test_short_but_currently_held_role_stays_experience(self):
        # A role you're in right now is never "Earlier:" material just
        # because it's new -- only a PAST role gets held to the
        # min-months bar. Matches the real Objectways case: 4 months in,
        # current, must render as full Experience, not collapse away.
        entry = {"date": "Jun 2026 - Present"}  # ~3 months as of _NOW, below the 6-month floor
        assert classify_role(entry, now=_NOW) == "EXPERIENCE"

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
    # Isolated from the real config's verified_metrics allowlist (which
    # holds Deshraj's actual confirmed-real percentages) -- these tests
    # exercise the hedging mechanism itself, not any specific business
    # data, so they pass their own empty allowlist rather than coincidentally
    # relying on a percentage not showing up in the live config.
    _UNVERIFIED_CONFIG = {"metrics": {"verified_metrics": []}}

    def test_unverified_percentage_gets_hedged(self):
        result = hedge_unverified_metrics("Reduced latency by 47%.", self._UNVERIFIED_CONFIG)
        assert "roughly 47%" in result

    def test_already_hedged_not_double_hedged(self):
        result = hedge_unverified_metrics("Reduced latency by roughly 47%.", self._UNVERIFIED_CONFIG)
        assert result.count("roughly") == 1

    def test_scope_counts_are_not_percentages_so_untouched(self):
        result = hedge_unverified_metrics("Processed 500GB/day across 12 sources.", self._UNVERIFIED_CONFIG)
        assert result == "Processed 500GB/day across 12 sources."

    def test_negative_percentage_hedges_before_the_sign_not_after(self):
        # Found via a real bullet while building the Part F golden
        # fixture: "(-35% silent drift)" must not hedge to
        # "(-roughly 35%...)" -- the sign belongs with the number.
        result = hedge_unverified_metrics("Monitors feature drift (-35% silent drift).", self._UNVERIFIED_CONFIG)
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
    def test_returns_none_when_disabled(self):
        config = {"work_authorization": {"include_work_auth_line": False, "work_auth_text": "should never appear"}}
        assert work_authorization_line(config) is None

    def test_returns_none_when_enabled_but_text_empty(self):
        # Enabled with no text configured yet must not invent a placeholder.
        config = {"work_authorization": {"include_work_auth_line": True, "work_auth_text": ""}}
        assert work_authorization_line(config) is None

    def test_returns_exact_configured_text_verbatim(self):
        config = {"work_authorization": {"include_work_auth_line": True, "work_auth_text": "Authorized to work in the U.S."}}
        assert work_authorization_line(config) == "Authorized to work in the U.S."

    def test_real_config_is_enabled_with_accurate_stem_opt_text(self):
        # Locks in the real, current product decision (2026-09-14, at the
        # candidate's explicit direction): the line must be precise that
        # OPT means no employer action is needed to hire NOW, and H-1B is
        # only relevant to continue PAST OPT -- a vaguer line risks being
        # misread as "needs sponsorship now" and screened out for the
        # wrong reason.
        line = work_authorization_line()
        assert line is not None
        assert "STEM OPT" in line
        assert "H-1B" in line
        assert "now" in line.lower()


class TestSelectCertificationsForResume:
    """A 3+ years candidate listing 5 certifications where 3 are
    beginner-level online-course completions reads as padding, not
    strength. This curates ONE document, never the underlying profile."""

    _CERTS = [
        "Tableau Business Intelligence Analyst - Feb 2026",
        "ElevateMe Bootcamp Data Analytics Certificate of Completion - Feb 2026",
        "IBM Data Analyst Specialization - Sep 2025",
        "Generative AI Mastermind (Outskill) - Oct 2025",
        "Guinness World Record - AI Training Hackathon (Kanz) - Jul 2026",
    ]

    def test_returns_everything_unchanged_when_no_config_section(self):
        # Note: an explicit {} (falsy) would fall through to get_config()
        # via the same `config or get_config()` pattern work_authorization_line
        # uses -- pass a real, present-but-empty section to test "no
        # priority/max_shown configured" specifically, not "no config passed".
        config = {"certifications": {}}
        assert select_certifications_for_resume(self._CERTS, config) == self._CERTS

    def test_priority_matches_surface_first_in_configured_order(self):
        config = {"certifications": {"priority": ["Guinness World Record", "Tableau"]}}
        result = select_certifications_for_resume(self._CERTS, config)
        assert result[0] == "Guinness World Record - AI Training Hackathon (Kanz) - Jul 2026"
        assert result[1] == "Tableau Business Intelligence Analyst - Feb 2026"

    def test_non_matches_keep_original_relative_order_after_matches(self):
        config = {"certifications": {"priority": ["Guinness World Record"]}}
        result = select_certifications_for_resume(self._CERTS, config)
        non_matched = result[1:]
        assert non_matched == self._CERTS[:4]  # original order, GWR (index 4) removed

    def test_max_shown_caps_the_list(self):
        config = {"certifications": {"priority": ["Guinness World Record", "Tableau"], "max_shown": 2}}
        result = select_certifications_for_resume(self._CERTS, config)
        assert len(result) == 2
        assert result == [
            "Guinness World Record - AI Training Hackathon (Kanz) - Jul 2026",
            "Tableau Business Intelligence Analyst - Feb 2026",
        ]

    def test_priority_key_with_no_match_is_skipped_not_an_error(self):
        config = {"certifications": {"priority": ["Nonexistent Cert", "Tableau"]}}
        result = select_certifications_for_resume(self._CERTS, config)
        assert result[0] == "Tableau Business Intelligence Analyst - Feb 2026"

    def test_never_invents_or_drops_a_real_certification_without_max_shown(self):
        config = {"certifications": {"priority": ["Tableau"]}}
        result = select_certifications_for_resume(self._CERTS, config)
        assert sorted(result) == sorted(self._CERTS)  # same 5, just reordered

    def test_real_config_leads_with_gwr_then_tableau_then_genai(self):
        # Locks in the real, current product decision (2026-09-14, at the
        # candidate's explicit direction): the resume shows exactly the 3
        # highest-signal real certifications, GWR first.
        result = select_certifications_for_resume(self._CERTS)
        assert len(result) == 3
        assert "Guinness World Record" in result[0]
        assert "Tableau" in result[1]
        assert "Generative AI" in result[2]
