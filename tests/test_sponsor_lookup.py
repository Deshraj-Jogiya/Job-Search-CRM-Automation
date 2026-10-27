"""Sponsorship lookups from the raw USCIS and LCA files: any employer, data roles only, cap-exempt first, staffing firms left out."""

import csv

from app.ingest import sponsor_lookup as sl


def _uscis(tmp_path):
    p = tmp_path / "hub.csv"
    rows = [
        ["Fiscal Year", "Employer", "Tax ID", "State", "City", "Continuing Approval", "Continuing Denial", "Initial Approval", "Initial Denial"],
        ["2025", "ACME ANALYTICS INC", "1", "AZ", "PHOENIX", "4", "0", "6", "1"],
        ["2026", "ACME ANALYTICS INC", "1", "AZ", "PHOENIX", "2", "0", "3", "0"],
        ["2025", "ARIZONA STATE UNIVERSITY", "2", "AZ", "TEMPE", "50", "0", "40", "0"],
        ["2025", "BODYSHOP STAFFING LLC", "3", "NJ", "EDISON", "100", "0", "90", "0"],
    ]
    with open(p, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    return p


def _lca(tmp_path):
    p = tmp_path / "lca.csv"
    rows = [sl.SLIM_COLUMNS]
    for n in range(4):
        rows.append(["ACME ANALYTICS INC", "", "Data Engineer", "15-1252", "II", "Phoenix", "AZ", "541511", "N", "1", "2026-01-05"])
    for n in range(3):
        rows.append(["ARIZONA STATE UNIVERSITY", "", "Data Scientist", "15-2051", "I", "Tempe", "AZ", "611310", "N", "1", "2026-02-01"])
    for n in range(9):
        rows.append(["BODYSHOP STAFFING LLC", "", "ETL Developer", "15-1252", "II", "Phoenix", "AZ", "541511", "Y", "1", "2026-03-01"])
    with open(p, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    return p


def test_data_roles_need_a_data_code_or_a_data_title_on_a_broad_code():
    assert sl.is_data_role("15-2051.00", "Data Scientist")
    assert sl.is_data_role("15-1252.00", "Senior Data Engineer")
    assert not sl.is_data_role("15-1252.00", "Java Developer")
    assert not sl.is_data_role("17-2112.02", "Data Validation Engineer")


def test_lookup_reports_approvals_data_filings_wage_levels_and_cap_exempt(tmp_path):
    uscis, lca = sl.load_uscis(_uscis(tmp_path)), sl.load_lca(_lca(tmp_path))
    acme, asu, unknown = sl.lookup(["Acme Analytics", "Arizona State University", "Nowhere Corp"], uscis, lca)
    assert acme["uscis_initial_approvals"] == 9 and acme["data_role_lcas"] == 4 and acme["wage_levels"] == {"II": 4}
    assert acme["verdict"] == "strong sponsor of data roles" and acme["az_data_role_lcas"] == 4
    assert asu["cap_exempt"] and asu["verdict"] == "cap-exempt (no lottery)"
    assert unknown["verdict"] == "no sponsorship on record" and unknown["matched"] is None


def test_data_sponsors_ranks_cap_exempt_first_and_leaves_out_staffing_firms(tmp_path):
    uscis, lca = sl.load_uscis(_uscis(tmp_path)), sl.load_lca(_lca(tmp_path))
    ranked = sl.data_sponsors(state="AZ", uscis=uscis, lca=lca)
    assert [r["name"] for r in ranked] == ["ARIZONA STATE UNIVERSITY", "ACME ANALYTICS INC"]
    with_staffing = sl.data_sponsors(state="AZ", include_dependent=True, uscis=uscis, lca=lca)
    assert "BODYSHOP STAFFING LLC" in [r["name"] for r in with_staffing]
    assert sl.data_sponsors(state="TX", uscis=uscis, lca=lca) == []


def test_for_profit_and_corporate_universities_are_not_cap_exempt():
    assert sl._cap_exempt("The University of Phoenix, Inc.", True)[0] is False
    assert sl._cap_exempt("Arizona State University", False)[0] is True
    assert sl._cap_exempt("Acme University Holdings LLC", False)[0] is False
    assert sl._cap_exempt("Princeton University", False)[0] is True  # "inc" inside a word is not a corporate suffix


def test_leading_the_matches_and_close_names_are_offered_when_nothing_matches(tmp_path):
    uscis, lca = sl.load_uscis(_uscis(tmp_path)), sl.load_lca(_lca(tmp_path))
    assert sl.lookup(["The Arizona State University"], uscis, lca)[0]["uscis_initial_approvals"] == 40
    miss = sl.lookup(["Acme"], uscis, lca)[0]
    assert miss["matched"] is None and miss["other_matches"] == ["ACME ANALYTICS INC"]
    assert sl.lookup(["Acme Analytics"], uscis, lca)[0]["other_matches"] == []


def test_dependent_employers_outside_staffing_codes_are_kept():
    meta_like = [{"h1b_dependent": "Yes", "naics": "519130"}] * 3
    staffing = [{"h1b_dependent": "Yes", "naics": "541511"}] * 3
    assert sl.staffing_share(meta_like) == 0 and sl.staffing_share(staffing) == 1


def test_a_cap_exempt_name_with_nothing_on_file_is_not_cap_exempt(tmp_path):
    uscis, lca = sl.load_uscis(_uscis(tmp_path)), sl.load_lca(_lca(tmp_path))
    r = sl.lookup(["Maricopa County Community College District"], uscis, lca)[0]
    assert r["cap_exempt"] is False and r["verdict"] == "no sponsorship on record"
