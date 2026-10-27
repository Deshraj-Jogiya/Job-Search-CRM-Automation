import random

from app import models
from app.services import demo_usage_service, matching_service, outreach_service, tailoring_service
from app.services.demo_seed_service import seed_demo_data


def _usage(db):
    rows = db.query(models.LlmUsageLog).all()
    return len(rows), sum(r.input_tokens for r in rows), sum(r.output_tokens for r in rows), \
        sum(r.estimated_cost_usd or 0 for r in rows)


def _scoreable(db):
    return db.query(models.JobApplication).filter(models.JobApplication.status == "Ingested").first()


class TestShowcaseRunsAddUsage:
    def test_scoring_adds_one_priced_call(self, db, monkeypatch):
        monkeypatch.setenv("APP_MODE", "showcase")
        seed_demo_data(db)
        calls, inp, out, cost = _usage(db)
        matching_service.score_application(db, _scoreable(db).id)
        db.commit()
        calls2, inp2, out2, cost2 = _usage(db)
        assert calls2 == calls + 1
        assert 3000 <= inp2 - inp <= 6500 and 300 <= out2 - out <= 700
        assert 0.01 < cost2 - cost < 0.04  # a real Sonnet 4.6 scoring call costs about two cents

    def test_tailoring_adds_five_calls(self, db, monkeypatch):
        monkeypatch.setenv("APP_MODE", "showcase")
        seed_demo_data(db)
        app = _scoreable(db)
        matching_service.score_application(db, app.id)
        calls, *_ = _usage(db)
        tailoring_service.tailor_application(db, app.id)
        db.commit()
        assert _usage(db)[0] == calls + 5
        assert db.query(models.LlmUsageLog).filter(models.LlmUsageLog.model.like("%(simulated)")).count() == 6

    def test_outreach_draft_adds_one_call(self, db, monkeypatch):
        monkeypatch.setenv("APP_MODE", "showcase")
        seed_demo_data(db)
        calls, *_ = _usage(db)
        outreach_service.draft_outreach_message(db, _scoreable(db).id, "Sample Person", "",
                                                "linkedin_connection", override_reason="test")
        assert _usage(db)[0] == calls + 1


class TestNeverOutsideShowcase:
    def test_personal_mode_records_nothing(self, db, monkeypatch):
        monkeypatch.setenv("APP_MODE", "personal")
        assert demo_usage_service.record_simulated_run(db, "tailor", random.Random(1)) == 0.0
        db.commit()
        assert db.query(models.LlmUsageLog).count() == 0

    def test_cost_uses_the_provider_price_table(self, db, monkeypatch):
        monkeypatch.setenv("APP_MODE", "showcase")
        cost = demo_usage_service.record_simulated_run(db, "score", random.Random(7))
        db.commit()
        row = db.query(models.LlmUsageLog).one()
        assert row.estimated_cost_usd == round(row.input_tokens * 3e-6 + row.output_tokens * 15e-6, 4) == cost


class TestSimulatedIntake:
    def test_runs_go_through_the_real_filters_until_the_pool_is_used_up(self, db, monkeypatch):
        from app.services import demo_intake_service
        monkeypatch.setenv("APP_MODE", "showcase")
        seed_demo_data(db)
        apps_before = db.query(models.JobApplication).count()
        calls_before = _usage(db)[0]
        totals = {"title": 0, "seniority": 0, "location": 0, "duplicate": 0, "added": 0}
        for _ in range(4):
            counts = demo_intake_service.run_simulated_intake(db, rng=random.Random(3))
            for k in totals:
                totals[k] += counts[k]
        # Every pool entry was pulled exactly once, and each trap was stopped by its own filter.
        assert sum(totals.values()) == len(demo_intake_service._POOL)
        assert totals == {"title": 1, "seniority": 2, "location": 2, "duplicate": 1, "added": 10}
        assert db.query(models.JobApplication).count() == apps_before + 10
        new = db.query(models.JobApplication).filter(models.JobApplication.id > apps_before).all()
        assert all(a.match_score is not None for a in new)
        assert _usage(db)[0] == calls_before + 10  # one simulated scoring call per new posting
        assert demo_intake_service.run_simulated_intake(db)["fetched"] == 0

    def test_refused_outside_showcase(self, db, monkeypatch):
        import pytest
        from app.services import demo_intake_service
        monkeypatch.setenv("APP_MODE", "personal")
        with pytest.raises(RuntimeError):
            demo_intake_service.run_simulated_intake(db)


class TestRulePrep:
    def test_prep_quotes_only_the_profile_and_the_posting(self, db, monkeypatch):
        import json as _json
        from app.services import demo_prep_service
        monkeypatch.setenv("APP_MODE", "showcase")
        seed_demo_data(db)
        app = _scoreable(db)
        calls, *_ = _usage(db)
        demo_prep_service.generate_rule_prep(db, app.id)
        assert _usage(db)[0] == calls + 6
        prep = db.query(models.InterviewPrep).filter_by(application_id=app.id, is_active=True).one()
        profile = _json.loads(db.query(models.ProfileVersion).filter_by(is_active=True).one().content_json)
        bullets = {b for e in profile["experience"] for b in e["bullets"]}
        bullets |= {b for p in profile["projects"] for b in p.get("bullets", []) + [p.get("description") or ""] if b}
        rounds = _json.loads(prep.predicted_rounds_json)["rounds"]
        technical = [q for q in rounds[1]["qa_pairs"] if q["category"] == "technical"]
        assert technical and all(any(b in q["draft_answer"] for b in bullets) for q in technical)
        assert rounds[2]["qa_pairs"], "confirmed STAR stories become behavioral answers"
        jd = app.posting.job_description
        for point in _json.loads(prep.company_prep_json)["why_this_company_talking_points"]:
            assert point.removeprefix("From the posting: ") in jd

    def test_seeded_preps_match_their_own_posting(self, db, monkeypatch):
        import json as _json
        monkeypatch.setenv("APP_MODE", "showcase")
        seed_demo_data(db)
        for prep in db.query(models.InterviewPrep):
            context = _json.loads(prep.company_prep_json)["company_context"]
            assert context in prep.application.posting.job_description


class TestRuleMockInterview:
    def _start(self, db, monkeypatch):
        from app.services import demo_prep_service, mock_interview_service as mis
        monkeypatch.setenv("APP_MODE", "showcase")
        seed_demo_data(db)
        app = _scoreable(db)
        demo_prep_service.generate_rule_prep(db, app.id)
        return mis, app, mis.start_session(db, app.id, "Technical: pipeline design", "guided")

    def test_follow_ups_ask_for_what_the_answer_lacks(self, db, monkeypatch):
        from app.services import demo_mock_interview as dm
        mis, app, session = self._start(db, monkeypatch)
        calls, *_ = _usage(db)
        turn = mis.submit_answer(db, session.id, "I used it a lot.")
        assert turn.content == dm.FOLLOW_UPS["short"] and turn.is_followup
        long_no_number = "I built the ingestion layer and owned the schedule and the alerts. " * 6
        turn = mis.submit_answer(db, session.id, long_no_number)
        assert not turn.is_followup  # never two follow-ups in a row
        turn = mis.submit_answer(db, session.id, long_no_number)
        assert turn.content == dm.FOLLOW_UPS["no_number"]
        assert _usage(db)[0] == calls + 3

    def test_debrief_checks_numbers_against_the_profile(self, db, monkeypatch):
        mis, app, session = self._start(db, monkeypatch)
        good = ("I own ingestion pipelines processing roughly 500GB/day across 12 upstream sources in Airflow, and I "
                "cut the nightly batch from 4 hours to 45 minutes by moving it to an incremental model that I designed.")
        made_up = good.replace("500GB", "900GB")
        mis.submit_answer(db, session.id, good)
        mis.submit_answer(db, session.id, made_up)
        done = mis.end_session(db, session.id)
        import json as _json
        d = _json.loads(done.debrief_json)
        assert done.status == "completed" and d["scored_by_rules"]
        assert any("900" in n for n in d["accuracy_notes"]) and not any("500" in n for n in d["accuracy_notes"])
        assert d["scorecard"]["content_accuracy"] == 4
        assert set(d["scorecard"].values()) <= {1, 2, 3, 4, 5}
