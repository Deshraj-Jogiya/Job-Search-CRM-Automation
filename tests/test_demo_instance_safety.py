import pytest

from app.app_mode import assert_database_url_safe_for_mode
from app.services.source_visibility import SOURCE_VISIBILITY, is_source_visible_here


class TestAssertDatabaseUrlSafeForMode:
    def test_showcase_mode_with_supabase_url_raises(self, monkeypatch):
        monkeypatch.setenv("APP_MODE", "showcase")
        with pytest.raises(RuntimeError, match="Supabase"):
            assert_database_url_safe_for_mode(
                "postgresql://user:pass@db.abcxyz.supabase.co:5432/postgres"
            )

    def test_showcase_mode_with_sqlite_url_is_fine(self, monkeypatch):
        monkeypatch.setenv("APP_MODE", "showcase")
        assert_database_url_safe_for_mode("sqlite:///./demo.db")  # no raise

    def test_personal_mode_with_supabase_url_is_fine(self, monkeypatch):
        monkeypatch.setenv("APP_MODE", "personal")
        assert_database_url_safe_for_mode(
            "postgresql://user:pass@db.abcxyz.supabase.co:5432/postgres"
        )  # no raise -- this is the real, intended personal-instance config

    def test_unset_app_mode_defaults_to_personal_and_is_fine(self, monkeypatch):
        monkeypatch.delenv("APP_MODE", raising=False)
        assert_database_url_safe_for_mode(
            "postgresql://user:pass@db.abcxyz.supabase.co:5432/postgres"
        )  # no raise

    def test_case_and_pooler_hostname_variants_still_caught(self, monkeypatch):
        monkeypatch.setenv("APP_MODE", "showcase")
        with pytest.raises(RuntimeError):
            assert_database_url_safe_for_mode(
                "postgresql://user:pass@aws-0-us-east-1.pooler.SUPABASE.com:6543/postgres"
            )


class TestSourceVisibility:
    def test_all_built_sources_are_tagged(self):
        for source in (
            "linkedin", "adzuna", "greenhouse", "lever", "ashby",
            "recruitee", "personio", "workable", "smartrecruiters", "jobspipe",
        ):
            assert source in SOURCE_VISIBILITY, f"{source} is missing a visibility tag"
            assert SOURCE_VISIBILITY[source] == "all_instances"

    def test_phase_2b_remote_boards_are_prod_only(self):
        for source in ("remoteok", "remotive", "weworkremotely", "jobspresso"):
            assert source in SOURCE_VISIBILITY, f"{source} is missing a visibility tag"
            assert SOURCE_VISIBILITY[source] == "prod_only"

    def test_remote_board_hidden_on_showcase_instance(self, monkeypatch):
        monkeypatch.setenv("APP_MODE", "showcase")
        for source in ("remoteok", "remotive", "weworkremotely", "jobspresso"):
            assert is_source_visible_here(source) is False

    def test_remote_board_visible_on_personal_instance(self, monkeypatch):
        monkeypatch.setenv("APP_MODE", "personal")
        for source in ("remoteok", "remotive", "weworkremotely", "jobspresso"):
            assert is_source_visible_here(source) is True

    def test_visible_everywhere_when_not_showcase_mode(self, monkeypatch):
        monkeypatch.setenv("APP_MODE", "personal")
        assert is_source_visible_here("greenhouse") is True
        # Even an unlisted/future source is visible outside showcase mode --
        # the restriction only applies to the demo instance.
        assert is_source_visible_here("some_future_source") is True

    def test_all_instances_source_visible_in_showcase_mode(self, monkeypatch):
        monkeypatch.setenv("APP_MODE", "showcase")
        assert is_source_visible_here("greenhouse") is True

    def test_unlisted_source_defaults_to_hidden_in_showcase_mode(self, monkeypatch):
        monkeypatch.setenv("APP_MODE", "showcase")
        assert is_source_visible_here("some_future_remote_board") is False
