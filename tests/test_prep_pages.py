"""The general interview prep sub-pages (deep dives, questions, coding, practice) and the spaced-review service."""
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import models
from app.csrf import CSRF_COOKIE_NAME
from app.main import app as web_app
from app.services import prep_content, prep_practice_service, profile_prep_service
from tests.conftest import make_variant

PROFILE = {
    "name": "Test Candidate",
    "summary": "Data engineer.",
    "experience": [
        {"company": "Acme LLC", "role": "Data Engineer", "date": "2025",
         "bullets": ["Cut ingestion time by 60% with PySpark."]},
    ],
    "projects": [
        {"name": "Public Project", "date": "2026", "technologies": ["Python", "Airflow"], "bullets": ["Built a thing, 15 tests."]},
        {"name": "Secret Team Project", "confidential": True, "technologies": ["Go"], "bullets": ["Under NDA."]},
    ],
    "skills": {"data_engineering": ["SQL", "dbt"]},
    "certifications": ["Cert A"],
}


@pytest.fixture()
def client(db):
    make_variant(db, content=PROFILE)
    c = TestClient(web_app)
    c.get("/interview-prep/practice")
    c.token = c.cookies.get(CSRF_COOKIE_NAME)
    return c


def _exp_key(profile=PROFILE):
    return next(i["key"] for i in prep_content.profile_items(profile) if i["kind"] == "experience")


# ---- spaced review

def test_next_box_moves_up_on_solid_back_to_start_on_could_not():
    assert prep_practice_service.next_box(0, 3) == 1
    assert prep_practice_service.next_box(2, 2) == 2
    assert prep_practice_service.next_box(3, 1) == 0
    top = len(prep_practice_service.INTERVAL_DAYS) - 1
    assert prep_practice_service.next_box(top, 3) == top


def test_rate_stores_one_row_per_item_and_sets_the_next_review(db):
    variant = make_variant(db, content=PROFILE)
    first = prep_practice_service.rate(db, variant.id, "qb:rag", 3)
    again = prep_practice_service.rate(db, variant.id, "qb:rag", 3)
    assert first.id == again.id and again.reviews == 2 and again.box == 2
    gap = again.next_due_at - again.last_reviewed_at
    assert abs(gap - timedelta(days=prep_practice_service.INTERVAL_DAYS[2])) < timedelta(minutes=1)
    with pytest.raises(prep_practice_service.PrepPracticeError):
        prep_practice_service.rate(db, variant.id, "qb:rag", 7)


def test_due_lists_only_valid_past_due_items_weakest_first(db):
    variant = make_variant(db, content=PROFILE)
    for key, rating in (("qb:a", 2), ("qb:b", 1), ("qb:gone", 1)):
        prep_practice_service.rate(db, variant.id, key, rating)
    for row in db.query(models.PrepPractice).all():
        row.next_due_at = datetime.utcnow() - timedelta(hours=1)
    db.commit()
    due = prep_practice_service.due(db, variant.id, {"qb:a", "qb:b"})
    assert [r.card_key for r in due] == ["qb:b", "qb:a"]


# ---- cards

def test_outline_cards_come_from_the_profile_and_skip_confidential_projects():
    cards = prep_content.build_cards(PROFILE, None)
    names = " ".join(c["title"] for c in cards)
    assert "Acme LLC" in names and "Public Project" in names and "Cert A" in names
    assert "Secret Team Project" not in names
    assert all(c["outline"] for c in cards)


def test_a_written_card_is_flagged_when_its_profile_item_changes():
    key = _exp_key()
    card = prep_content.card_from_form(key, {"short": "I built pipelines."}, prep_content.fingerprint_for(PROFILE, key))
    assert next(c for c in prep_content.build_cards(PROFILE, [card]) if c["key"] == key).get("changed_since") is False
    changed = json.loads(json.dumps(PROFILE))
    changed["experience"][0]["bullets"].append("New bullet.")
    assert next(c for c in prep_content.build_cards(changed, [card]) if c["key"] == key)["changed_since"] is True


def test_general_sections_survive_the_answer_refresh(db):
    variant = make_variant(db, content=PROFILE)
    profile_prep_service.save_general_section(db, variant.id, "question_examples", {"rag": "My example."})
    _, drafted = profile_prep_service.get_active_prep(db, variant.id)
    assert drafted["question_examples"] == {"rag": "My example."}
    assert "deep_dives" in profile_prep_service.GENERAL_PREP_KEYS
    assert "question_examples" in profile_prep_service.GENERAL_PREP_KEYS


# ---- pages

@pytest.mark.parametrize("path, text", [
    ("/interview-prep/deep-dives", "Acme LLC"),
    ("/interview-prep/questions", "Common questions"),
    ("/interview-prep/coding", "Pattern drill"),
    ("/interview-prep/practice", "Due now"),
])
def test_each_page_renders_with_the_tab_bar(client, path, text):
    page = client.get(path)
    assert page.status_code == 200
    assert text in page.text and 'class="prep-tabs"' in page.text and 'aria-current="page"' in page.text
    assert "Secret Team Project" not in page.text


def test_saving_a_card_keeps_it_and_warns_about_numbers_not_in_the_profile(client, db):
    key = _exp_key()
    resp = client.post(f"/interview-prep/deep-dives/{key}/edit", follow_redirects=False,
                       data={"csrf_token": client.token, "short": "Cut ingestion time by 60%, saved 99 hours."})
    assert resp.status_code == 303 and "99" in resp.headers["location"] and "60" not in resp.headers["location"]
    variant = db.query(models.ProfileVariant).first()
    _, drafted = profile_prep_service.get_active_prep(db, variant.id)
    assert drafted["deep_dives"][0]["key"] == key
    page = client.get("/interview-prep/deep-dives").text
    assert "saved 99 hours" in page


def test_rating_from_a_page_goes_back_to_that_page_and_rejects_outside_redirects(client):
    resp = client.post("/interview-prep/practice/rate", follow_redirects=False,
                       data={"csrf_token": client.token, "card_key": "qb:rag", "rating": 3,
                             "back": "/interview-prep/questions#q-rag"})
    assert resp.headers["location"].startswith("/interview-prep/questions?message=") and resp.headers["location"].endswith("#q-rag")
    resp = client.post("/interview-prep/practice/rate", follow_redirects=False,
                       data={"csrf_token": client.token, "card_key": "qb:rag", "rating": 3, "back": "https://evil.example"})
    assert resp.headers["location"].startswith("/interview-prep/practice")


def test_the_public_demo_never_writes_and_keeps_practice_in_the_browser(client, db, monkeypatch):
    monkeypatch.setenv("APP_MODE", "showcase")
    key = _exp_key()
    for path, data in ((f"/interview-prep/deep-dives/{key}/edit", {"short": "x"}),
                       ("/interview-prep/questions/rag/example", {"example": "x"}),
                       ("/interview-prep/practice/rate", {"card_key": "qb:rag", "rating": 3})):
        resp = client.post(path, data={"csrf_token": client.token, **data}, follow_redirects=False)
        assert resp.status_code == 303 and "error=" in resp.headers["location"]
    assert db.query(models.PrepPractice).count() == 0
    assert db.query(models.ProfileInterviewPrep).count() == 0
    page = client.get("/interview-prep/questions").text
    assert 'data-local="1"' in page and "Add your example" not in page
