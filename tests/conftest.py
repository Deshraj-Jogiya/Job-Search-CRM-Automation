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

import pytest  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app import models  # noqa: E402,F401  -- registers every table on Base.metadata


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
