"""b1.3: fuzzy header-mapping fallback + learning loop for the
USCIS/DOL LCA/OEWS loaders (app/ingest/column_utils.py). Patches
_MAPPING_FILE to a tmp_path for every test so real runs never touch
data/column_mappings.yaml."""

from unittest.mock import patch

import pytest
import yaml

from app.ingest.column_utils import ColumnResolutionError, resolve_column_learned
from app.models import AdaptationLog


@pytest.fixture()
def mapping_file(tmp_path):
    path = tmp_path / "column_mappings.yaml"
    with patch("app.ingest.column_utils._MAPPING_FILE", path):
        yield path


class TestExactMatchUnaffected:
    def test_exact_alias_match_never_touches_the_mapping_file(self, db, mapping_file):
        result = resolve_column_learned(db, ["Employer Name"], ["Employer Name"], "dol_lca", "employer")
        assert result == "Employer Name"
        assert not mapping_file.exists()


class TestFuzzyFallback:
    def test_close_header_fuzzy_matches_and_is_recorded(self, db, mapping_file):
        # "Petitioner Business Name" isn't in the alias list at all, but
        # is close enough to "Employer Name" once a similar real header
        # like this appears.
        columns = ["Petitioner Bussiness Name"]  # real-world typo in the header itself
        result = resolve_column_learned(
            db, columns, ["Employer Name", "Petitioner Business Name"], "dol_lca", "employer",
        )
        assert result == "Petitioner Bussiness Name"
        assert mapping_file.exists()
        saved = yaml.safe_load(mapping_file.read_text())
        assert saved["dol_lca"]["employer"]["column"] == "Petitioner Bussiness Name"

    def test_no_match_above_threshold_raises_when_required(self, db, mapping_file):
        with pytest.raises(ColumnResolutionError):
            resolve_column_learned(db, ["Completely Unrelated Field"], ["Employer Name"], "dol_lca", "employer")

    def test_no_match_above_threshold_returns_none_when_not_required(self, db, mapping_file):
        result = resolve_column_learned(
            db, ["Completely Unrelated Field"], ["Employer Name"], "dol_lca", "employer", required=False,
        )
        assert result is None

    def test_second_run_reuses_the_saved_guess_without_a_new_fuzzy_pass(self, db, mapping_file):
        columns = ["Petitioner Bussiness Name"]
        aliases = ["Employer Name", "Petitioner Business Name"]
        resolve_column_learned(db, columns, aliases, "dol_lca", "employer")
        # Second call, same file -- should just reuse the saved column,
        # not require a fresh fuzzy match (still returns the right value
        # even with a threshold that would never let a fresh guess in).
        result = resolve_column_learned(db, columns, aliases, "dol_lca", "employer", threshold=0.999)
        assert result == "Petitioner Bussiness Name"


class TestManualCorrectionDetection:
    def test_editing_the_saved_column_logs_a_correction(self, db, mapping_file):
        columns = ["Petitioner Bussiness Name"]
        aliases = ["Employer Name", "Petitioner Business Name"]
        resolve_column_learned(db, columns, aliases, "dol_lca", "employer")

        # Simulate the user manually correcting the mapping file --
        # editing "column" without touching "guessed_column".
        saved = yaml.safe_load(mapping_file.read_text())
        saved["dol_lca"]["employer"]["column"] = "Business Name (Corrected)"
        mapping_file.write_text(yaml.safe_dump(saved))

        # Next run detects the correction even though the "corrected"
        # value isn't a real column in THIS file -- it should fall
        # through to a fresh fuzzy guess rather than crash.
        resolve_column_learned(db, columns, aliases, "dol_lca", "employer", required=False)

        entry = db.query(AdaptationLog).filter(AdaptationLog.subsystem == "column_mapping").one()
        assert entry.parameter == "dol_lca:employer"
        assert entry.old_value == "Petitioner Bussiness Name"
        assert entry.new_value == "Business Name (Corrected)"
        assert entry.status == "applied"

    def test_correction_is_only_logged_once(self, db, mapping_file):
        columns = ["Petitioner Bussiness Name"]
        aliases = ["Employer Name", "Petitioner Business Name"]
        resolve_column_learned(db, columns, aliases, "dol_lca", "employer")

        saved = yaml.safe_load(mapping_file.read_text())
        saved["dol_lca"]["employer"]["column"] = "Business Name (Corrected)"
        mapping_file.write_text(yaml.safe_dump(saved))

        resolve_column_learned(db, columns, aliases, "dol_lca", "employer", required=False)
        resolve_column_learned(db, columns, aliases, "dol_lca", "employer", required=False)

        count = db.query(AdaptationLog).filter(AdaptationLog.subsystem == "column_mapping").count()
        assert count == 1


class TestConfirmedPairsWeightFutureMatches:
    def test_confirmed_string_is_offered_as_a_candidate_for_a_new_file(self, db, mapping_file):
        from app.services import adaptation_service

        adaptation_service.record_column_mapping_correction(
            db, "dol_lca", "employer", None, "Employer (Petitioner) Legal Name",
        )
        # A brand new file/year with a header close to the CONFIRMED
        # string but not close to the generic alias list at all.
        columns = ["Employer (Petitioner) Legal Name -- Revised"]
        result = resolve_column_learned(
            db, columns, ["Employer Name"], "dol_lca", "employer", threshold=0.7,
        )
        assert result == columns[0]
