from app.services.salary_parser import parse_salary_range


class TestExplicitRange:
    def test_comma_separated_range_with_hyphen(self):
        assert parse_salary_range("The salary range is $120,000 - $150,000 per year.") == (120000, 150000)

    def test_range_with_no_spaces(self):
        assert parse_salary_range("Pay: $120,000-$150,000") == (120000, 150000)

    def test_range_with_the_word_to(self):
        assert parse_salary_range("Compensation of $120,000 to $150,000 annually.") == (120000, 150000)

    def test_range_with_only_one_dollar_sign(self):
        assert parse_salary_range("Salary: $120,000 - 150,000") == (120000, 150000)

    def test_reversed_range_is_normalized_low_to_high(self):
        assert parse_salary_range("$150,000 - $120,000") == (120000, 150000)


class TestKAbbreviatedRange:
    def test_lowercase_k(self):
        assert parse_salary_range("Base salary $120k - $150k") == (120000, 150000)

    def test_uppercase_k(self):
        assert parse_salary_range("Base salary $120K-$150K") == (120000, 150000)


class TestSingleAnnualFigure:
    def test_per_year_phrasing(self):
        assert parse_salary_range("This role pays $140,000 per year.") == (140000, 140000)

    def test_slash_year_phrasing(self):
        assert parse_salary_range("$140,000/year") == (140000, 140000)

    def test_annually_phrasing(self):
        assert parse_salary_range("Compensation: $140,000 annually") == (140000, 140000)

    def test_k_form_per_year(self):
        assert parse_salary_range("$140k per year") == (140000, 140000)


class TestHourlyRateConvertedToAnnual:
    def test_per_hour_phrasing(self):
        assert parse_salary_range("This contract pays $50 per hour.") == (104000, 104000)

    def test_slash_hr_phrasing(self):
        assert parse_salary_range("$55/hr") == (114400, 114400)

    def test_decimal_hourly_rate(self):
        result = parse_salary_range("$48.50/hour")
        assert result == (100880, 100880)


class TestNoUnambiguousSalary:
    def test_no_dollar_amount_at_all(self):
        assert parse_salary_range("We offer competitive compensation and great benefits.") is None

    def test_bare_number_with_no_year_or_hour_marker(self):
        # A dollar figure with no range/year/hour marker is genuinely
        # ambiguous (could be a budget, a signing bonus, anything) --
        # never guessed.
        assert parse_salary_range("This project has a $150,000 budget.") is None

    def test_empty_text(self):
        assert parse_salary_range("") is None

    def test_none_text(self):
        assert parse_salary_range(None) is None
