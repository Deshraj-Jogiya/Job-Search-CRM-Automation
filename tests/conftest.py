"""Shared pytest fixtures. Points the app at a disposable, file-based
SQLite database instead of the real DATABASE_URL -- this must happen
before any `app.*` module is imported, since app/database.py binds its
engine at import time from the environment.
"""

import os
import tempfile

_TEST_DB_PATH = os.path.join(tempfile.gettempdir(), "career_pilot_test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production-use")
# No test may start a background LLM refresh of the profile interview prep; the tests that cover it
# turn it back on explicitly (see tests/test_profile_prep.py).
os.environ["PROFILE_PREP_AUTO_REFRESH"] = "0"
# Tests run as an instance with an AI provider configured (llm_available() is True), with a key that
# can never make a paid call: importing app.main loads .env, but load_dotenv never overrides a variable
# already set, so a real key in a developer's .env stays out of the test run. Tests of the no-AI paths
# switch it off explicitly.
os.environ["LLM_PROVIDER"] = "anthropic"
os.environ["ANTHROPIC_API_KEY"] = "test-key-not-a-real-key"
os.environ.pop("APP_MODE", None)

# Tests never read a developer's .env. It holds real Gmail SMTP credentials and paid API keys, and
# importing app.main calls load_dotenv(): on 2026-10-22 that put the real SMTP login into the test run
# and every tailoring test emailed a real "Approve? Data Engineer at Acme Corp" notification (14 per
# full run). Three layers, so no single one is relied on: load_dotenv is disabled before any app module
# imports it, every credential that can send or spend is blanked, and SMTP itself is blocked below.
import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *args, **kwargs: False
for _name in ("SMTP_USER", "SMTP_PASSWORD", "SMTP_SERVER", "NOTIFICATION_EMAIL", "TAVILY_API_KEY", "HUNTER_API_KEY",
              "ADZUNA_APP_ID", "ADZUNA_APP_KEY", "JOBSPIPE_API_KEY", "OPENAI_API_KEY", "OPENAI_API_BASE",
              "CLAUDE_CLI_PATH", "DASHBOARD_PASSWORD", "ADMIN_PASSWORD"):
    os.environ[_name] = ""
# An AI call a test forgot to mock goes to a closed local port and fails at once, instead of reaching
# the real API (which it did, with the real key, before these lines existed).
os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9"
os.environ["OLLAMA_HOST"] = "http://127.0.0.1:9"

import smtplib  # noqa: E402


class _NoSmtp:
    """Any attempt to open an SMTP connection in a test fails loudly instead of sending mail."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError("A test tried to send real email. Mock email_utils.send_email instead.")


smtplib.SMTP = smtplib.SMTP_SSL = _NoSmtp

import pytest  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app import models  # noqa: E402,F401  -- registers every table on Base.metadata


def pytest_addoption(parser):
    # Part F: the golden resume fixture (test_golden_resume.py) never
    # auto-updates on a mismatch -- regenerating it requires this
    # explicit flag, which also prints a full diff, so a real
    # regression can never accidentally get rubber-stamped as "the new
    # golden".
    parser.addoption(
        "--update-golden", action="store_true", default=False,
        help="Regenerate tests/fixtures/golden/ from the current code instead of asserting against it.",
    )


@pytest.fixture()
def update_golden(request):
    return request.config.getoption("--update-golden")


@pytest.fixture()
def db():
    """A fresh schema for every test -- several service functions commit
    internally, so a simple transaction rollback wouldn't isolate tests
    from each other."""
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def settings(db):
    return models.get_or_create_settings(db)


def make_company(db, name="Acme Corp"):
    company = models.Company(name=name, normalized_name=name.lower())
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


def make_posting(db, company, source="greenhouse", **overrides):
    defaults = dict(
        company_id=company.id,
        company_name_raw=company.name,
        job_title="Data Scientist",
        job_url="https://example.com/job/1",
        job_description="A great job.",
        source=source,
    )
    defaults.update(overrides)
    posting = models.JobPosting(**defaults)
    db.add(posting)
    db.commit()
    db.refresh(posting)
    return posting


def make_application(db, posting, **overrides):
    defaults = dict(posting_id=posting.id, status="Tailored", match_score=75)
    defaults.update(overrides)
    application = models.JobApplication(**defaults)
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


def make_variant(db, name="Data Engineering", is_default=True, content=None):
    import json

    variant = models.ProfileVariant(name=name, is_default=is_default)
    db.add(variant)
    db.commit()
    db.refresh(variant)

    version = models.ProfileVersion(
        variant_id=variant.id,
        content_json=json.dumps(content if content is not None else {"name": "Test Candidate", "experience": []}),
        source="manual",
        is_active=True,
    )
    db.add(version)
    db.commit()
    return variant
