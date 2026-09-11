from app.models import Company
from app.services.company_tier import derive_tier


def _company(**overrides):
    defaults = dict(
        name="Test Co", normalized_name="test co",
        lca_filings_total=0, h1b_approvals_total=0,
        max_wage_level_15xx=None, is_cap_exempt=False,
    )
    defaults.update(overrides)
    return Company(**defaults)


class TestTierA:
    def test_exact_threshold_with_qualifying_wage_level_is_a(self, settings):
        # defaults: tier_a_min_filings=10, tier_a_min_wage_level=3 (III)
        company = _company(lca_filings_total=10, max_wage_level_15xx="III")
        assert derive_tier(company, settings) == "A"

    def test_one_below_filing_threshold_is_not_a(self, settings):
        company = _company(lca_filings_total=9, max_wage_level_15xx="IV")
        assert derive_tier(company, settings) != "A"
        assert derive_tier(company, settings) == "B"

    def test_enough_filings_but_wage_level_too_low_is_not_a(self, settings):
        company = _company(lca_filings_total=15, max_wage_level_15xx="II")
        assert derive_tier(company, settings) != "A"
        assert derive_tier(company, settings) == "B"

    def test_enough_filings_but_no_wage_level_on_record_is_not_a(self, settings):
        company = _company(lca_filings_total=20, max_wage_level_15xx=None)
        assert derive_tier(company, settings) != "A"

    def test_combines_lca_and_uscis_counts(self, settings):
        company = _company(lca_filings_total=6, h1b_approvals_total=4, max_wage_level_15xx="IV")
        assert derive_tier(company, settings) == "A"  # 6 + 4 = 10, meets threshold


class TestTierB:
    def test_exact_threshold_is_b(self, settings):
        # tier_b_min_filings=3
        company = _company(lca_filings_total=3)
        assert derive_tier(company, settings) == "B"

    def test_one_below_b_threshold_is_c(self, settings):
        company = _company(lca_filings_total=2)
        assert derive_tier(company, settings) == "C"


class TestTierC:
    def test_one_filing_is_c(self, settings):
        company = _company(lca_filings_total=1)
        assert derive_tier(company, settings) == "C"

    def test_cap_exempt_with_zero_filings_is_c(self, settings):
        company = _company(lca_filings_total=0, h1b_approvals_total=0, is_cap_exempt=True)
        assert derive_tier(company, settings) == "C"


class TestTierX:
    def test_zero_filings_not_cap_exempt_is_x(self, settings):
        company = _company(lca_filings_total=0, h1b_approvals_total=0, is_cap_exempt=False)
        assert derive_tier(company, settings) == "X"

    def test_null_filing_counts_treated_as_zero(self, settings):
        company = _company(lca_filings_total=None, h1b_approvals_total=None, is_cap_exempt=False)
        assert derive_tier(company, settings) == "X"


class TestConfigDriven:
    def test_changing_settings_changes_the_boundary(self, settings):
        company = _company(lca_filings_total=5)
        assert derive_tier(company, settings) == "C" if settings.tier_b_min_filings > 5 else "B"
        settings.tier_b_min_filings = 5
        assert derive_tier(company, settings) == "B"
        settings.tier_b_min_filings = 6
        assert derive_tier(company, settings) == "C"
