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


def test_file_keys_match_brand_variants_without_guessing():
    from app.ingest.sponsors import _file_keys_for, _index

    keys = ["jpmorgan chase", "nielsen consumer dba nielseniq", "mckinsey united states", "acme", "acme one", "a b c d e f"]
    index = _index(keys)
    aliases = {"quantumblack ai by mckinsey": ["mckinsey united states"]}
    assert _file_keys_for("acme", index, aliases) == ["acme"]  # exact name wins
    assert _file_keys_for("jpmorganchase", index, aliases) == ["jpmorgan chase"]  # spaces ignored
    assert _file_keys_for("nielseniq", index, aliases) == ["nielsen consumer dba nielseniq"]  # doing business as
    assert _file_keys_for("quantumblack ai by mckinsey", index, aliases) == ["mckinsey united states"]
    assert _file_keys_for("unknown co", index, aliases) == []
    ambiguous = _index(["data co", "dataco"])
    assert _file_keys_for("da taco", ambiguous, {}) == []  # two employers fit: left unmatched


def test_sponsorship_alerts_flag_companies_and_debarment_forces_tier_x(tmp_path):
    import json
    from datetime import date, timedelta

    from app.database import Base
    from app.ingest.sponsors import load_sponsorship_alerts
    from app.models import Company, GlobalSettings
    from app.services.company_tier import derive_tier
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    for name in ("Sadup Softech LLC", "Microsoft", "Acme Data"):
        db.add(Company(name=name, normalized_name=__import__("app.services.company_utils", fromlist=["x"]).normalize_company_name(name),
                       lca_filings_total=500, max_wage_level_15xx="III"))
    db.commit()
    future = (date.today() + timedelta(days=30)).isoformat()
    past = (date.today() - timedelta(days=1)).isoformat()
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps({"alerts": [
        {"name": "Sadup Softech, LLC", "kind": "debarred", "until": future, "note": "debarred", "source": "https://dol.gov"},
        {"name": "Microsoft", "kind": "perm_suspended", "until": None, "note": "PERM suspended", "source": "https://example.org"},
    ]}))
    result = load_sponsorship_alerts(db, str(path))
    assert sorted(result["companies_flagged"]) == ["Microsoft (perm_suspended)", "Sadup Softech LLC (debarred)"]

    settings = GlobalSettings(tier_a_min_filings=50, tier_b_min_filings=3, tier_a_min_wage_level=2)
    by = {c.name: c for c in db.query(Company)}
    assert derive_tier(by["Sadup Softech LLC"], settings) == "X"   # banned now, whatever its record
    assert derive_tier(by["Microsoft"], settings) == "A"           # green-card suspension leaves the H-1B record as it is
    assert derive_tier(by["Acme Data"], settings) == "A"
    by["Sadup Softech LLC"].sponsorship_alert_until = date.fromisoformat(past)
    assert derive_tier(by["Sadup Softech LLC"], settings) == "A"   # ban over


def test_a_new_company_on_the_alert_list_is_flagged_when_created():
    from app.database import Base
    from app.services.intake_service import get_or_create_company
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    assert get_or_create_company(db, "Sadup Softech LLC").sponsorship_alert_kind == "debarred"
    assert get_or_create_company(db, "Unlisted Analytics Inc").sponsorship_alert_kind is None


def test_legal_entity_names_count_but_other_businesses_sharing_a_first_word_do_not():
    from app.ingest.sponsors import _file_keys_for, _index

    keys = ["cognizant us", "wipro limited", "bank of america n a", "sigma software", "sigma computing", "rocket mortgage",
            "american express", "american express national bank"]
    index = _index(keys)
    assert _file_keys_for("cognizant", index, {}) == ["cognizant us"]
    assert _file_keys_for("wipro", index, {}) == ["wipro limited"]
    assert _file_keys_for("bank of america", index, {}) == ["bank of america n a"]
    assert _file_keys_for("sigma", index, {}) == []
    assert _file_keys_for("rocket", index, {}) == []
    assert _file_keys_for("sigma", _index(["sigma global", "sigma usa"]), {}) == ["sigma usa"]  # one word: legal suffix only
    assert _file_keys_for("american express", index, {}) == ["american express", "american express national bank"]


def test_aliases_that_normalize_to_the_same_employer_are_counted_once():
    from app.ingest.sponsors import _file_keys_for, _index

    assert _file_keys_for("acme", _index(["acme data"]), {"acme": ["acme data", "acme data"]}) == ["acme data"]
