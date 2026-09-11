from app.models import Company, GlobalSettings, JobPosting, OewsWage
from app.services import wage_level_service
from app.services.company_utils import normalize_company_name


def _settings(db, **overrides):
    settings = GlobalSettings(**overrides)
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings


def _company(db):
    company = Company(name="Acme Corp", normalized_name=normalize_company_name("Acme Corp"))
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


def _posting(db, company, **overrides):
    defaults = dict(
        company_id=company.id, company_name_raw=company.name, job_title="Data Engineer",
        job_description="d", source="greenhouse",
    )
    defaults.update(overrides)
    posting = JobPosting(**defaults)
    db.add(posting)
    db.commit()
    db.refresh(posting)
    return posting


def _oews_row(db, soc_code="15-1252", area_title="Austin-Round Rock, TX", **overrides):
    defaults = dict(
        soc_code=soc_code, area_title=area_title, source_year=2025,
        wage_level_1=80000, wage_level_2=100000, wage_level_3=130000, wage_level_4=160000,
    )
    defaults.update(overrides)
    row = OewsWage(**defaults)
    db.add(row)
    db.commit()
    return row


class TestExtractCityState:
    def test_plain_city_state(self):
        assert wage_level_service._extract_city_state("Austin, TX") == ("Austin", "TX")

    def test_city_state_with_trailing_annotation(self):
        assert wage_level_service._extract_city_state("Austin, TX (Remote)") == ("Austin", "TX")

    def test_multi_word_city(self):
        assert wage_level_service._extract_city_state("San Jose, CA") == ("San Jose", "CA")

    def test_remote_only_returns_none(self):
        assert wage_level_service._extract_city_state("Remote") is None

    def test_none_returns_none(self):
        assert wage_level_service._extract_city_state(None) is None


class TestFindOewsAreaTitle:
    def test_matches_a_real_loaded_area(self, db):
        _oews_row(db, area_title="Austin-Round Rock, TX")
        result = wage_level_service.find_oews_area_title(db, "Austin, TX", "15-1252")
        assert result == "Austin-Round Rock, TX"

    def test_no_match_for_unloaded_area(self, db):
        _oews_row(db, area_title="Austin-Round Rock, TX")
        result = wage_level_service.find_oews_area_title(db, "Boise, ID", "15-1252")
        assert result is None

    def test_no_match_for_remote(self, db):
        _oews_row(db, area_title="Austin-Round Rock, TX")
        assert wage_level_service.find_oews_area_title(db, "Remote", "15-1252") is None

    def test_does_not_match_wrong_soc_code(self, db):
        _oews_row(db, soc_code="15-1252", area_title="Austin-Round Rock, TX")
        assert wage_level_service.find_oews_area_title(db, "Austin, TX", "15-2051") is None

    def test_does_not_partial_match_a_different_city(self, db):
        # "Austin" should not match an area that merely CONTAINS "Austin"
        # as a substring of a longer, unrelated city name.
        _oews_row(db, area_title="North Austinville-Someplace, TX")
        assert wage_level_service.find_oews_area_title(db, "Austin, TX", "15-1252") is None


class TestComputePerPostingWageLevel:
    def test_parsed_salary_and_area_match_produces_a_wage_level(self, db):
        settings = _settings(db, target_soc_code="15-1252")
        _oews_row(db, area_title="Austin-Round Rock, TX")
        company = _company(db)
        posting = _posting(
            db, company, location="Austin, TX",
            job_description="This role pays $145,000 per year.",
        )
        result = wage_level_service.compute_per_posting_wage_level(db, posting, settings)
        assert result["salary_min"] == 145000
        assert result["salary_source"] == "parsed"
        assert result["wage_level"] == "III"  # between wage_level_3 (130000) and wage_level_4 (160000)

    def test_no_salary_in_jd_leaves_wage_level_none(self, db):
        settings = _settings(db, target_soc_code="15-1252")
        _oews_row(db, area_title="Austin-Round Rock, TX")
        company = _company(db)
        posting = _posting(db, company, location="Austin, TX", job_description="Great benefits, no salary stated.")
        result = wage_level_service.compute_per_posting_wage_level(db, posting, settings)
        assert result["salary_min"] is None
        assert result["wage_level"] is None

    def test_salary_found_but_no_area_match_leaves_wage_level_none(self, db):
        settings = _settings(db, target_soc_code="15-1252")
        _oews_row(db, area_title="Austin-Round Rock, TX")
        company = _company(db)
        posting = _posting(db, company, location="Remote", job_description="Pays $145,000 per year.")
        result = wage_level_service.compute_per_posting_wage_level(db, posting, settings)
        assert result["salary_min"] == 145000
        assert result["wage_level"] is None

    def test_manual_entry_is_used_over_a_parse(self, db):
        settings = _settings(db, target_soc_code="15-1252")
        _oews_row(db, area_title="Austin-Round Rock, TX")
        company = _company(db)
        posting = _posting(
            db, company, location="Austin, TX", job_description="Pays $90,000 per year.",
            offered_salary_min=170000, offered_salary_max=170000, offered_salary_source="manual",
        )
        result = wage_level_service.compute_per_posting_wage_level(db, posting, settings)
        assert result["salary_min"] == 170000
        assert result["salary_source"] == "manual"
        assert result["wage_level"] == "IV"


class TestApplyWageLevelToPosting:
    def test_persists_computed_fields_onto_the_posting(self, db):
        settings = _settings(db, target_soc_code="15-1252")
        _oews_row(db, area_title="Austin-Round Rock, TX")
        company = _company(db)
        posting = _posting(db, company, location="Austin, TX", job_description="Pays $145,000 per year.")
        wage_level_service.apply_wage_level_to_posting(db, posting, settings)
        assert posting.offered_salary_min == 145000
        assert posting.offered_salary_source == "parsed"
        assert posting.wage_level_per_posting == "III"

    def test_never_overwrites_a_manual_entry(self, db):
        settings = _settings(db, target_soc_code="15-1252")
        _oews_row(db, area_title="Austin-Round Rock, TX")
        company = _company(db)
        posting = _posting(
            db, company, location="Austin, TX", job_description="Pays $90,000 per year.",
            offered_salary_min=170000, offered_salary_max=170000, offered_salary_source="manual",
        )
        wage_level_service.apply_wage_level_to_posting(db, posting, settings)
        assert posting.offered_salary_min == 170000
        assert posting.offered_salary_source == "manual"


class TestSetManualSalary:
    def test_sets_fields_and_commits(self, db):
        settings = _settings(db, target_soc_code="15-1252")
        _oews_row(db, area_title="Austin-Round Rock, TX")
        company = _company(db)
        posting = _posting(db, company, location="Austin, TX", job_description="d")
        wage_level_service.set_manual_salary(db, posting, settings, 150000, 160000)
        assert posting.offered_salary_min == 150000
        assert posting.offered_salary_max == 160000
        assert posting.offered_salary_source == "manual"
        assert posting.wage_level_per_posting == "III"
