import pandas as pd
import pytest

from app.ingest.column_utils import ColumnResolutionError, resolve_column
from app.ingest.sponsors import load_dol_lca_data, load_uscis_h1b_data
from app.services.company_utils import normalize_company_name
from app.models import Company


def _make_company(db, name):
    company = Company(name=name, normalized_name=normalize_company_name(name))
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


class TestResolveColumn:
    def test_matches_case_and_whitespace_insensitively(self):
        columns = ["  Employer Name ", "SOC_CODE"]
        assert resolve_column(columns, ["employer name"]) == "  Employer Name "

    def test_raises_with_actual_columns_listed(self):
        with pytest.raises(ColumnResolutionError) as exc:
            resolve_column(["Foo", "Bar"], ["Employer"])
        assert "Foo" in str(exc.value) and "Bar" in str(exc.value)

    def test_not_required_returns_none(self):
        assert resolve_column(["Foo"], ["Employer"], required=False) is None


class TestUscisIngest:
    def test_matches_only_tracked_companies_and_aggregates_across_years(self, db, tmp_path):
        _make_company(db, "Acme Corp")
        _make_company(db, "Untracked Employer LLC")

        csv_path = tmp_path / "uscis.csv"
        pd.DataFrame(
            [
                {
                    "Employer": "Acme Corp",
                    "Fiscal Year": 2022,
                    "Initial Approval": 10,
                    "Initial Denial": 1,
                    "Continuing Approval": 5,
                    "Continuing Denial": 0,
                },
                {
                    "Employer": "Acme Corporation",  # same normalized name (suffix stripped)
                    "Fiscal Year": 2023,
                    "Initial Approval": 20,
                    "Initial Denial": 2,
                    "Continuing Approval": 0,
                    "Continuing Denial": 1,
                },
                {
                    "Employer": "Some Employer Never Tracked Inc",
                    "Fiscal Year": 2023,
                    "Initial Approval": 999,
                    "Initial Denial": 0,
                    "Continuing Approval": 0,
                    "Continuing Denial": 0,
                },
            ]
        ).to_csv(csv_path, index=False)

        result = load_uscis_h1b_data(db, str(csv_path))

        acme = db.query(Company).filter(Company.name == "Acme Corp").first()
        assert acme.h1b_approvals_total == 10 + 5 + 20 + 0
        assert acme.h1b_denials_total == 1 + 0 + 2 + 1
        assert acme.h1b_last_fiscal_year == 2023
        assert acme.sponsorship_tier == "Occasional"
        assert acme.h1b_data_updated_at is not None

        untracked = db.query(Company).filter(Company.name == "Untracked Employer LLC").first()
        assert untracked.h1b_approvals_total is None  # never appeared in the file

        assert result["companies_matched"] == 1
        assert result["companies_total"] == 2

    def test_rerun_recomputes_instead_of_doubling(self, db, tmp_path):
        _make_company(db, "Acme Corp")
        csv_path = tmp_path / "uscis.csv"
        pd.DataFrame(
            [{
                "Employer": "Acme Corp", "Fiscal Year": 2023,
                "Initial Approval": 10, "Initial Denial": 0,
                "Continuing Approval": 0, "Continuing Denial": 0,
            }]
        ).to_csv(csv_path, index=False)

        load_uscis_h1b_data(db, str(csv_path))
        load_uscis_h1b_data(db, str(csv_path))  # rerun against the same file

        acme = db.query(Company).filter(Company.name == "Acme Corp").first()
        assert acme.h1b_approvals_total == 10  # not 20

    def test_sponsorship_tier_buckets(self, db, tmp_path):
        _make_company(db, "Zero Co")
        _make_company(db, "Rare Co")
        _make_company(db, "Frequent Co")
        csv_path = tmp_path / "uscis.csv"
        pd.DataFrame(
            [
                {"Employer": "Zero Co", "Fiscal Year": 2023, "Initial Approval": 0, "Initial Denial": 0, "Continuing Approval": 0, "Continuing Denial": 0},
                {"Employer": "Rare Co", "Fiscal Year": 2023, "Initial Approval": 2, "Initial Denial": 0, "Continuing Approval": 0, "Continuing Denial": 0},
                {"Employer": "Frequent Co", "Fiscal Year": 2023, "Initial Approval": 100, "Initial Denial": 0, "Continuing Approval": 0, "Continuing Denial": 0},
            ]
        ).to_csv(csv_path, index=False)

        load_uscis_h1b_data(db, str(csv_path))

        assert db.query(Company).filter(Company.name == "Zero Co").first().sponsorship_tier == "None"
        assert db.query(Company).filter(Company.name == "Rare Co").first().sponsorship_tier == "Rare"
        assert db.query(Company).filter(Company.name == "Frequent Co").first().sponsorship_tier == "Frequent"


class TestDolLcaIngest:
    def test_counts_only_certified_h1b_filings(self, db, tmp_path):
        _make_company(db, "Beta Inc")
        xlsx_path = tmp_path / "lca.xlsx"
        pd.DataFrame(
            [
                {"EMPLOYER_NAME": "Beta Inc", "CASE_STATUS": "Certified", "VISA_CLASS": "H-1B"},
                {"EMPLOYER_NAME": "Beta Inc", "CASE_STATUS": "Certified", "VISA_CLASS": "H-1B"},
                {"EMPLOYER_NAME": "Beta Inc", "CASE_STATUS": "Withdrawn", "VISA_CLASS": "H-1B"},
                {"EMPLOYER_NAME": "Beta Inc", "CASE_STATUS": "Certified", "VISA_CLASS": "E-3"},
            ]
        ).to_excel(xlsx_path, index=False)

        result = load_dol_lca_data(db, str(xlsx_path), "FY2024Q1")

        beta = db.query(Company).filter(Company.name == "Beta Inc").first()
        assert beta.lca_filings_total == 2
        assert beta.lca_last_fiscal_quarter == "FY2024Q1"
        assert result["companies_matched"] == 1

    def test_rerun_overwrites_not_doubles(self, db, tmp_path):
        _make_company(db, "Beta Inc")
        xlsx_path = tmp_path / "lca.xlsx"
        pd.DataFrame(
            [{"EMPLOYER_NAME": "Beta Inc", "CASE_STATUS": "Certified", "VISA_CLASS": "H-1B"}]
        ).to_excel(xlsx_path, index=False)

        load_dol_lca_data(db, str(xlsx_path), "FY2024Q1")
        load_dol_lca_data(db, str(xlsx_path), "FY2024Q1")

        beta = db.query(Company).filter(Company.name == "Beta Inc").first()
        assert beta.lca_filings_total == 1

    def test_tracks_max_wage_level_for_soc_15_roles(self, db, tmp_path):
        _make_company(db, "Beta Inc")
        xlsx_path = tmp_path / "lca.xlsx"
        pd.DataFrame(
            [
                {"EMPLOYER_NAME": "Beta Inc", "CASE_STATUS": "Certified", "VISA_CLASS": "H-1B", "SOC_CODE": "15-1252", "PW_WAGE_LEVEL": "II"},
                {"EMPLOYER_NAME": "Beta Inc", "CASE_STATUS": "Certified", "VISA_CLASS": "H-1B", "SOC_CODE": "15-2051", "PW_WAGE_LEVEL": "IV"},
                {"EMPLOYER_NAME": "Beta Inc", "CASE_STATUS": "Certified", "VISA_CLASS": "H-1B", "SOC_CODE": "15-1252", "PW_WAGE_LEVEL": "III"},
            ]
        ).to_excel(xlsx_path, index=False)

        result = load_dol_lca_data(db, str(xlsx_path), "FY2024Q1")

        beta = db.query(Company).filter(Company.name == "Beta Inc").first()
        assert beta.max_wage_level_15xx == "IV"  # highest of II/IV/III
        assert result["wage_level_matched"] == 1

    def test_ignores_non_15_soc_codes_for_wage_level(self, db, tmp_path):
        _make_company(db, "Beta Inc")
        xlsx_path = tmp_path / "lca.xlsx"
        pd.DataFrame(
            [{"EMPLOYER_NAME": "Beta Inc", "CASE_STATUS": "Certified", "VISA_CLASS": "H-1B", "SOC_CODE": "13-2011", "PW_WAGE_LEVEL": "IV"}]
        ).to_excel(xlsx_path, index=False)

        load_dol_lca_data(db, str(xlsx_path), "FY2024Q1")

        beta = db.query(Company).filter(Company.name == "Beta Inc").first()
        assert beta.max_wage_level_15xx is None

    def test_missing_soc_or_wage_level_columns_does_not_crash(self, db, tmp_path):
        _make_company(db, "Beta Inc")
        xlsx_path = tmp_path / "lca.xlsx"
        pd.DataFrame(
            [{"EMPLOYER_NAME": "Beta Inc", "CASE_STATUS": "Certified", "VISA_CLASS": "H-1B"}]
        ).to_excel(xlsx_path, index=False)

        result = load_dol_lca_data(db, str(xlsx_path), "FY2024Q1")

        assert result["wage_level_matched"] == 0
        beta = db.query(Company).filter(Company.name == "Beta Inc").first()
        assert beta.lca_filings_total == 1
        assert beta.max_wage_level_15xx is None

    def test_normalizes_numeric_and_level_prefixed_wage_values(self, db, tmp_path):
        _make_company(db, "Beta Inc")
        xlsx_path = tmp_path / "lca.xlsx"
        pd.DataFrame(
            [{"EMPLOYER_NAME": "Beta Inc", "CASE_STATUS": "Certified", "VISA_CLASS": "H-1B", "SOC_CODE": "15-1252", "PW_WAGE_LEVEL": "Level 3"}]
        ).to_excel(xlsx_path, index=False)

        load_dol_lca_data(db, str(xlsx_path), "FY2024Q1")

        beta = db.query(Company).filter(Company.name == "Beta Inc").first()
        assert beta.max_wage_level_15xx == "III"
