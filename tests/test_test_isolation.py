"""The test run must never send real email or make a paid call, whatever a developer's .env holds.

Regression for 2026-10-22: importing app.main loaded the real .env into the test run, and every
tailoring test emailed a real "Approve? Data Engineer at Acme Corp" notification.
"""

import os
import smtplib

import pytest


def test_dotenv_is_not_loaded_by_the_app():
    import app.main  # noqa: F401  -- the import that used to load .env
    from app.services import email_utils

    assert not email_utils.is_smtp_configured()
    for name in ("SMTP_USER", "SMTP_PASSWORD", "TAVILY_API_KEY", "HUNTER_API_KEY", "ADZUNA_APP_KEY", "OPENAI_API_KEY"):
        assert not os.getenv(name), name


def test_smtp_cannot_be_opened():
    with pytest.raises(RuntimeError, match="real email"):
        smtplib.SMTP("smtp.gmail.com", 587)


def test_ai_calls_cannot_leave_the_machine():
    assert os.environ["ANTHROPIC_API_KEY"] == "test-key-not-a-real-key"
    assert os.environ["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1")
