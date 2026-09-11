from app.ingest.cap_exempt import apply_cap_exempt_flags, classify_cap_exempt
from app.services.company_utils import normalize_company_name
from app.models import Company


class TestClassifyCapExempt:
    def test_matches_known_national_lab(self):
        is_exempt, reason = classify_cap_exempt("Lawrence Berkeley National Laboratory")
        assert is_exempt is True
        assert "Lawrence Berkeley" in reason

    def test_matches_university_pattern(self):
        is_exempt, reason = classify_cap_exempt("Stanford University")
        assert is_exempt is True
        assert "pattern" in reason

    def test_does_not_match_ordinary_company(self):
        is_exempt, reason = classify_cap_exempt("Acme Corp")
        assert is_exempt is False
        assert reason is None

    def test_does_not_false_positive_on_substring(self):
        # "Colleges" isn't a word-boundary match for "college" the way
        # a naive substring check would wrongly allow -- guards against
        # a company like "Collegiate Sports Inc" false-positiving.
        is_exempt, _ = classify_cap_exempt("Collegiate Sports Inc")
        assert is_exempt is False


class TestApplyCapExemptFlags:
    def test_sweeps_and_flags_matching_companies(self, db):
        db.add(Company(name="MIT University", normalized_name=normalize_company_name("MIT University")))
        db.add(Company(name="Acme Corp", normalized_name=normalize_company_name("Acme Corp")))
        db.commit()

        result = apply_cap_exempt_flags(db)

        mit = db.query(Company).filter(Company.name == "MIT University").first()
        acme = db.query(Company).filter(Company.name == "Acme Corp").first()
        assert mit.is_cap_exempt is True
        assert acme.is_cap_exempt is False
        assert result["companies_flagged"] == 1
        assert result["companies_total"] == 2

    def test_rerun_unflags_after_removed_from_seeds(self, db, monkeypatch):
        db.add(Company(name="Acme University", normalized_name=normalize_company_name("Acme University")))
        db.commit()
        apply_cap_exempt_flags(db)
        acme = db.query(Company).filter(Company.name == "Acme University").first()
        assert acme.is_cap_exempt is True

        # Simulate the seed pattern being removed/tightened -- idempotent
        # recompute should un-flag, not just ever accumulate flags.
        import app.ingest.cap_exempt as cap_exempt_module
        monkeypatch.setattr(cap_exempt_module, "_load_seeds", lambda: {"name_patterns": [], "curated_employers": []})

        apply_cap_exempt_flags(db)
        acme = db.query(Company).filter(Company.name == "Acme University").first()
        assert acme.is_cap_exempt is False
