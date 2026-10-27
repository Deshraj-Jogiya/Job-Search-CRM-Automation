"""The /extension page's sample form is filled by the same resolver the extension calls."""
from fastapi.testclient import TestClient

from app.csrf import CSRF_COOKIE_NAME
from app.main import app as web_app
from app.routers.extension_page import SAMPLE_FIELDS


def test_page_lists_the_tested_platforms(db):
    page = TestClient(web_app).get("/extension").text
    for name in ("Greenhouse", "Lever", "Ashby", "Recruitee", "Personio", "Workable", "SmartRecruiters", "Workday"):
        assert name in page


def test_sample_fill_uses_the_profile_and_leaves_open_questions(db, monkeypatch):
    from app.routers import extension_page

    seen = {}

    def fake_resolve(_db, application_id, fields):
        seen["fields"] = fields
        return {"first_name": "Jordan", "sponsorship": "Yes"}

    monkeypatch.setattr(extension_page.extension_service, "resolve_field_answers", fake_resolve)
    client = TestClient(web_app)
    client.get("/extension")
    token = client.cookies.get(CSRF_COOKIE_NAME)
    page = client.post("/extension/sample-fill", data={"application_id": 1, "csrf_token": token}).text
    assert 'value="Jordan"' in page and "Filled 2 of" in page and "Why do you want to work here?" in page
    # Radio groups go to the resolver as selects, the way the content script sends them.
    assert {f["field_id"]: f["type"] for f in seen["fields"]}["sponsorship"] == "select"
    assert len(seen["fields"]) == len(SAMPLE_FIELDS)
