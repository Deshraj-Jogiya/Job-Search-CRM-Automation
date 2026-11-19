"""The static interview-prep content (app/prep_content/) is shipped to every instance and shown as worked examples,
so every example must actually run and give the stated answer, every SQL query must return the stated rows, and the
question bank must be well formed. A wrong worked example would teach the wrong thing."""

import json
import sqlite3

import pytest

from app.services import prep_content


CODING = prep_content.coding_practice()
BANK = prep_content.question_bank()


def _plain(value):
    return json.loads(json.dumps(value))


@pytest.mark.parametrize("pattern", CODING["patterns"], ids=lambda p: p["id"])
def test_worked_example_runs_and_gives_the_stated_answer(pattern):
    worked = pattern["worked"]
    code = "\n".join(line["code"] for line in worked["lines"])
    namespace: dict = {}
    exec(worked["check"].get("setup", ""), namespace)  # noqa: S102 - our own shipped content
    exec(code, namespace)  # noqa: S102
    assert _plain(eval(worked["check"]["call"], namespace)) == worked["check"]["expect"]  # noqa: S307


@pytest.mark.parametrize("pattern", CODING["patterns"], ids=lambda p: p["id"])
def test_every_step_label_is_used_and_in_range(pattern):
    worked = pattern["worked"]
    used = {line["step"] for line in worked["lines"]} - {0}
    assert used == set(range(1, len(worked["steps"]) + 1))


@pytest.mark.parametrize("pattern", [p for p in CODING["patterns"] if p.get("sql")], ids=lambda p: p["id"])
def test_sql_example_returns_the_stated_rows(pattern):
    conn = sqlite3.connect(":memory:")
    for statement in pattern["sql"]["setup"]:
        conn.execute(statement)
    rows = [list(r) for r in conn.execute(pattern["sql"]["query"]).fetchall()]
    assert rows == pattern["sql"]["expect"]


def test_problem_links_are_well_formed_and_unique_per_pattern():
    for pattern in CODING["patterns"]:
        slugs = [p["slug"] for p in pattern["problems"]]
        assert len(slugs) == len(set(slugs))
        for p in pattern["problems"]:
            assert p["url"] == f"https://leetcode.com/problems/{p['slug']}/"
            assert p["statement"] and p["level"] in ("Easy", "Medium", "Hard")


def test_question_bank_ids_unique_and_every_entry_complete():
    ids = [q["id"] for s in BANK["sections"] for q in s["questions"]]
    assert len(ids) == len(set(ids))
    for section in BANK["sections"]:
        assert section["kind"] in ("process", "concept", "difference")
        for q in section["questions"]:
            assert q["q"].strip()
            if section["kind"] == "process":
                assert len(q["steps"]) >= 3
            elif section["kind"] == "concept":
                assert q["definition"] and q["picture"] and q["how"] and q["use_when"] and q["trade_offs"]
            else:
                assert q["columns"] and q["rows"] and q["rule"]
                assert all(len(row) == len(q["columns"]) + 1 for row in q["rows"])
            for f in q.get("follow_ups", []):
                assert f["q"].strip() and f["a"].strip()


def test_card_keys_are_stable_and_distinct():
    keys = prep_content.all_card_keys()
    assert len(keys) == len(set(keys))
    assert "qb:messy-data" in keys and "code:two-sum" in keys
