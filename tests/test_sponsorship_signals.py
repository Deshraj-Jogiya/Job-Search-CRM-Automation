from app.services.sponsorship_signals import detect_sponsorship_signals


class TestNegativePatterns:
    def test_no_visa_sponsorship_blocks(self):
        result = detect_sponsorship_signals("This role offers no visa sponsorship at this time.")
        assert result["sponsorship_blocked"] is True
        assert "no visa sponsorship" in result["signal_matches"]["blocked"]

    def test_us_citizens_only_blocks(self):
        result = detect_sponsorship_signals("Must be a US citizens only due to contract requirements.")
        assert result["sponsorship_blocked"] is True

    def test_c2c_blocks(self):
        result = detect_sponsorship_signals("W2 only, no C2C candidates.")
        assert result["sponsorship_blocked"] is True

    def test_active_security_clearance_blocks(self):
        result = detect_sponsorship_signals("Candidate must hold an active security clearance.")
        assert result["sponsorship_blocked"] is True

    def test_itar_blocks(self):
        result = detect_sponsorship_signals("This position is subject to ITAR restrictions.")
        assert result["sponsorship_blocked"] is True


class TestPositivePatterns:
    def test_will_sponsor_signals(self):
        result = detect_sponsorship_signals("We will sponsor a work visa for the right candidate.")
        assert result["sponsorship_signal"] is True
        assert result["sponsorship_blocked"] is False

    def test_h1b_mention_signals(self):
        result = detect_sponsorship_signals("H-1B candidates are welcome to apply.")
        assert result["sponsorship_signal"] is True

    def test_cap_exempt_signals(self):
        result = detect_sponsorship_signals("As a cap-exempt research institution, we can sponsor year-round.")
        assert result["sponsorship_signal"] is True

    def test_stem_opt_signals(self):
        result = detect_sponsorship_signals("Open to candidates on STEM OPT extension.")
        assert result["sponsorship_signal"] is True


class TestWorksiteAmbiguity:
    def test_work_from_anywhere_is_ambiguous(self):
        result = detect_sponsorship_signals("This is a work from anywhere role, fully remote.")
        assert result["worksite_ambiguous"] is True

    def test_any_timezone_is_ambiguous(self):
        result = detect_sponsorship_signals("We hire across any timezone globally.")
        assert result["worksite_ambiguous"] is True

    def test_specific_us_city_is_not_ambiguous(self):
        result = detect_sponsorship_signals("This role is based in Austin, TX with hybrid flexibility.")
        assert result["worksite_ambiguous"] is False


class TestFalsePositiveGuard:
    """The exact scenario the user flagged: bare 'sponsorship' inside an
    unrelated event/marketing context must never fire either pattern
    list -- only visa/work-authorization-context phrasing should."""

    def test_event_sponsorship_does_not_fire(self):
        jd = (
            "This role includes managing our conference sponsorship program and sponsorship "
            "opportunities for partners at industry events."
        )
        result = detect_sponsorship_signals(jd)
        assert result["sponsorship_blocked"] is False
        assert result["sponsorship_signal"] is False
        assert result["signal_matches"] == {"blocked": [], "signal": [], "worksite_ambiguous": []}

    def test_bare_sponsorship_word_alone_does_not_fire(self):
        result = detect_sponsorship_signals("Sponsorship opportunities available for our annual gala.")
        assert result["sponsorship_blocked"] is False
        assert result["sponsorship_signal"] is False


class TestBothFireSimultaneously:
    def test_both_negative_and_positive_recorded_independently(self):
        jd = "We will sponsor for most roles, but this specific position requires US citizens only."
        result = detect_sponsorship_signals(jd)
        assert result["sponsorship_blocked"] is True
        assert result["sponsorship_signal"] is True
        assert "US citizens only" in result["signal_matches"]["blocked"]
        assert "will sponsor" in result["signal_matches"]["signal"]


class TestEmptyInput:
    def test_empty_string_returns_all_false(self):
        result = detect_sponsorship_signals("")
        assert result == {
            "sponsorship_blocked": False,
            "sponsorship_signal": False,
            "worksite_ambiguous": False,
            "signal_matches": {"blocked": [], "signal": [], "worksite_ambiguous": []},
        }

    def test_none_returns_all_false(self):
        result = detect_sponsorship_signals(None)
        assert result["sponsorship_blocked"] is False
