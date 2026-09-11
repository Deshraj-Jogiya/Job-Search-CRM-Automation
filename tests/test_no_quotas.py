"""
Part A guardrail: this platform sets, displays, or enforces no target
number of applications or outreach messages per day or week. It counts
what happened; it never tells the user what they should do. These
tests assert that directly against the real source tree, not just
against one code path -- a quota/target/gating mechanism reintroduced
anywhere should fail one of these.
"""

from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"


def _read_all(glob_pattern: str) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in APP_DIR.rglob(glob_pattern))


class TestNoSendDayGating:
    def test_no_holiday_light_day_or_backlog_gating_code_anywhere(self):
        """Checks for actual gating CODE (a function/variable/config key
        implementing the concept), not just the word appearing anywhere
        -- outreach_hygiene.py's own docstring mentions "no holiday
        gating" by name to document its absence, which a bare word
        search can't tell apart from the real thing."""
        source = _read_all("*.py")
        banned_identifiers = (
            "is_holiday", "holiday_calendar", "light_day", "backlog_redistribut",
            "send_day_policy", "send_day_gate", "daily_ceiling",
        )
        for banned in banned_identifiers:
            assert banned not in source, f"Found gating identifier {banned!r} in app/ source"


class TestNoTargetFieldsInSchema:
    def test_no_daily_or_weekly_target_columns_on_global_settings(self):
        models_source = (APP_DIR / "models.py").read_text(encoding="utf-8")
        for banned in ("daily_application_target", "daily_outreach_touch_target", "daily_outreach_cap"):
            assert banned not in models_source, f"Found removed quota column {banned!r} still in models.py"


class TestNoTargetRenderingInTemplates:
    def test_no_target_or_quota_variable_rendered_anywhere(self):
        """Precise on purpose: checks for the specific removed target/
        quota variable names, not a generic "contains a slash" heuristic
        -- a plain ratio display (e.g. metrics.html's real "N replied /
        M sent" segmentation row, which is informational analytics, not
        a goal) would otherwise false-positive on any X/Y-shaped pair of
        Jinja expressions."""
        banned_vars = (
            "counters.applications_today", "counters.outreach_today",
            "applications_target", "outreach_target", "daily_outreach_cap",
            "target_applications_per_week",
        )
        for html_path in (APP_DIR / "templates").glob("*.html"):
            html = html_path.read_text(encoding="utf-8")
            for banned in banned_vars:
                assert banned not in html, f"{html_path.name} renders removed quota variable {banned!r}"
