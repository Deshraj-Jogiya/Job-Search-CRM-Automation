import pandas as pd

from app.ingest.wages import load_oews_wage_data, wage_level_for
from app.models import OewsWage


def _load_sample(db, tmp_path, source_year=2024):
    xlsx_path = tmp_path / "oews.xlsx"
    pd.DataFrame(
        [{
            "OCC_CODE": "15-1252",
            "OCC_TITLE": "Software Developers",
            "AREA_TITLE": "San Jose-Sunnyvale-Santa Clara, CA",
            "LEVEL1": 90000,
            "LEVEL2": 110000,
            "LEVEL3": 130000,
            "LEVEL4": 150000,
        }]
    ).to_excel(xlsx_path, index=False)
    return load_oews_wage_data(db, str(xlsx_path), source_year)


class TestLoadOewsWageData:
    def test_upserts_a_row(self, db, tmp_path):
        result = _load_sample(db, tmp_path)
        assert result["rows_upserted"] == 1
        row = db.query(OewsWage).first()
        assert row.soc_code == "15-1252"
        assert row.wage_level_1 == 90000
        assert row.wage_level_4 == 150000

    def test_rerun_updates_in_place_not_duplicates(self, db, tmp_path):
        _load_sample(db, tmp_path)
        _load_sample(db, tmp_path)
        assert db.query(OewsWage).count() == 1


class TestWageLevelFor:
    def test_classifies_each_bracket(self, db, tmp_path):
        _load_sample(db, tmp_path)
        soc, area = "15-1252", "San Jose-Sunnyvale-Santa Clara, CA"

        assert wage_level_for(db, soc, area, 80000) == "Below Level I"
        assert wage_level_for(db, soc, area, 95000) == "I"
        assert wage_level_for(db, soc, area, 115000) == "II"
        assert wage_level_for(db, soc, area, 135000) == "III"
        assert wage_level_for(db, soc, area, 200000) == "IV"

    def test_returns_none_when_no_data_loaded(self, db):
        assert wage_level_for(db, "00-0000", "Nowhere, XX", 100000) is None

    def test_prefers_most_recent_source_year(self, db, tmp_path):
        _load_sample(db, tmp_path, source_year=2023)
        xlsx_path = tmp_path / "oews_2024.xlsx"
        pd.DataFrame(
            [{
                "OCC_CODE": "15-1252",
                "OCC_TITLE": "Software Developers",
                "AREA_TITLE": "San Jose-Sunnyvale-Santa Clara, CA",
                "LEVEL1": 95000,
                "LEVEL2": 115000,
                "LEVEL3": 135000,
                "LEVEL4": 155000,
            }]
        ).to_excel(xlsx_path, index=False)
        load_oews_wage_data(db, str(xlsx_path), 2024)

        level = wage_level_for(db, "15-1252", "San Jose-Sunnyvale-Santa Clara, CA", 92000)
        assert level == "Below Level I"  # 2024's higher Level I (95000), not 2023's (90000)
