"""Sample job-search data for the public showcase instance.

The demo runs the same code as a personal instance; only the data differs.
A demo that holds nothing but a profile shows empty pages and teaches a
visitor nothing, and with no API keys a visitor cannot generate anything
either. This module fills the demo database with a fictional but realistic
search: companies, postings from the real source types, applications at
every pipeline stage with real-shaped score breakdowns, tailored resumes
and cover letters, interview prep, a finished mock interview, outreach
drafts and usage history. Every company, person and address in it is
made up (example.com addresses only).

Visitors share one database (the demo has no login wall), so
reset_demo_data() puts everything back to this starting point once a day.
Both entry points refuse to run outside showcase mode, and the reset also
refuses any database that is not SQLite, so it can never touch a personal
instance's data.
"""

import json
import os
from datetime import timedelta

from sqlalchemy.orm import Session

from ..app_mode import is_showcase_mode
from ..models import (
    ActivityLog,
    AdminAccount,
    AdaptationLog,
    AdaptiveParameterValue,
    BehavioralStory,
    Company,
    InterviewPrep,
    JobApplication,
    JobPosting,
    LlmUsageLog,
    MockInterviewSession,
    MockInterviewTurn,
    OutreachMessage,
    ProfileInterviewPrep,
    ProfileVariant,
    ProfileVersion,
    ResearchAgentQuery,
    SavedAnswer,
    TailoredDocument,
    get_or_create_settings,
    utcnow,
)
from . import profile_service
from .company_utils import normalize_company_name

_FIXTURE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures", "demo_profile.json")


class DemoSeedRefused(RuntimeError):
    pass


def _guard(db: Session) -> None:
    if not is_showcase_mode():
        raise DemoSeedRefused("Demo data is only written in showcase mode (APP_MODE=showcase).")
    if db.get_bind().dialect.name != "sqlite":
        raise DemoSeedRefused("Demo data is only written to the demo's own SQLite database.")


# (name, ats field, slug, tier, h1b approvals)
_COMPANIES = [
    ("Northwind Logistics", "greenhouse_slug", "northwindlogistics", "A", 412),
    ("Brightline Health", "lever_slug", "brightlinehealth", "A", 238),
    ("Cobalt Payments", "ashby_slug", "cobaltpayments", "A", 655),
    ("Fernway Energy", "workable_slug", "fernway-energy", "B", 41),
    ("Larkspur Analytics", "greenhouse_slug", "larkspuranalytics", "B", 57),
    ("Meridian Freight", None, None, "C", 6),
    ("Quarry Labs", "ashby_slug", "quarrylabs", "B", 33),
    ("Tidewater Insurance", "smartrecruiters_slug", "TidewaterInsurance", "A", 189),
    ("Halcyon Retail", "lever_slug", "halcyonretail", "B", 72),
    ("Orbital Maps", "greenhouse_slug", "orbitalmaps", "A", 301),
    ("Pinecrest Bank", None, None, "A", 520),
    ("Saltmarsh Media", "recruitee_slug", "saltmarshmedia", "C", 3),
    ("Juniper Robotics", "ashby_slug", "juniperrobotics", "B", 64),
    ("Vantage Clinics", "workable_slug", "vantage-clinics", "X", 0),
    ("Lakeshore State University", "workable_slug", "lakeshore-state", "A", 140),
]

_CAP_EXEMPT = {"Lakeshore State University": "University (cap-exempt employer)"}

_JD_TEMPLATE = (
    "About the team\n{team}\n\n"
    "What you'll do\n"
    "- Design, build and run batch and streaming pipelines that feed {consumers}.\n"
    "- Model data in the warehouse with dbt and keep it tested, documented and fresh.\n"
    "- Own data quality: schema checks, freshness alerts and clear incident follow-up.\n"
    "- Work with analysts and product engineers to turn questions into reliable datasets.\n\n"
    "What we're looking for\n"
    "- {years}+ years building production data pipelines in Python and SQL.\n"
    "- Hands-on experience with {stack}.\n"
    "- Comfort with cloud infrastructure ({cloud}) and infrastructure as code.\n"
    "- Clear written communication; you write the runbook before you're asked.\n\n"
    "Nice to have\n- {nice}\n\n"
    "Compensation\nThe base salary range for this role is ${lo:,} - ${hi:,}. {sponsor}"
)

# company, title, source, location, team, consumers, years, stack, cloud, nice, lo, hi, sponsor sentence
_POSTINGS = [
    ("Northwind Logistics", "Senior Data Engineer", "greenhouse", "Austin, TX (Hybrid)",
     "The platform data team moves 40M shipment events a day from carriers into the systems dispatchers rely on.",
     "dispatch dashboards and ETA models", 5, "Airflow, Spark and Kafka", "AWS", "Experience with geospatial data.",
     150000, 182000, "We sponsor H-1B visas for this role."),
    ("Brightline Health", "Data Engineer", "lever", "Remote (US)",
     "We build the claims and eligibility data that care teams and actuaries use every day.",
     "care-gap reports and actuarial models", 3, "dbt, Airflow and Snowflake", "AWS", "HIPAA or healthcare data experience.",
     128000, 152000, "Visa sponsorship is available."),
    ("Cobalt Payments", "Data Engineer, Risk", "ashby", "New York, NY",
     "Risk engineering keeps fraud losses low without blocking good customers.",
     "real-time fraud scoring", 3, "Kafka, Spark Structured Streaming and Python", "AWS", "Feature stores.",
     145000, 175000, "We sponsor and transfer H-1B visas."),
    ("Fernway Energy", "Analytics Engineer", "workable", "Austin, TX",
     "Fernway runs solar and battery sites across Texas; the data team turns meter readings into forecasts.",
     "grid forecasting and finance reporting", 2, "dbt and SQL", "GCP", "Time-series data.",
     110000, 130000, ""),
    ("Larkspur Analytics", "Data Platform Engineer", "greenhouse", "Remote (US)",
     "Larkspur hosts analytics workloads for 300 mid-size retailers.",
     "customer-facing analytics", 4, "Spark, Terraform and Airflow", "AWS", "Multi-tenant data platforms.",
     140000, 165000, "Sponsorship considered case by case."),
    ("Meridian Freight", "ETL Developer", "linkedin", "Dallas, TX",
     "A small BI team supporting freight brokerage operations.",
     "operations reporting", 2, "SQL Server and SSIS", "Azure", "Power BI.",
     85000, 98000, ""),
    ("Quarry Labs", "Data Engineer", "ashby", "San Francisco, CA",
     "Quarry builds developer tools; the data team measures how millions of builds behave.",
     "product analytics and billing", 3, "dbt, BigQuery and Python", "GCP", "Experience with event pipelines.",
     150000, 180000, "We sponsor visas."),
    ("Tidewater Insurance", "Data Engineer II", "smartrecruiters", "Charlotte, NC",
     "Tidewater's data office modernises 30 years of policy data onto a cloud warehouse.",
     "underwriting and claims analytics", 3, "Airflow, dbt and Redshift", "AWS", "Insurance domain knowledge.",
     118000, 140000, "H-1B sponsorship available."),
    ("Halcyon Retail", "Data Engineer", "lever", "Austin, TX",
     "Halcyon runs 220 stores; the data team owns inventory and demand data.",
     "inventory replenishment and sales dashboards", 3, "dbt, Airflow and Snowflake", "AWS", "Retail or supply chain data.",
     120000, 142000, ""),
    ("Orbital Maps", "Senior Data Engineer, Streaming", "greenhouse", "Seattle, WA",
     "Orbital processes location updates from 9M devices to keep maps current.",
     "map freshness and traffic models", 5, "Kafka, Flink and Spark", "AWS", "Geospatial indexing (H3, S2).",
     165000, 198000, "We sponsor H-1B visas."),
    ("Pinecrest Bank", "Data Engineer - Regulatory Reporting", "adzuna", "Austin, TX",
     "The regulatory data team produces the filings Pinecrest sends to its regulators.",
     "regulatory filings", 3, "SQL, Python and Airflow", "Azure", "Banking regulatory reporting.",
     115000, 138000, ""),
    ("Saltmarsh Media", "Junior Data Engineer", "recruitee", "Remote (US)",
     "A podcast network measuring 50M monthly downloads.",
     "audience analytics", 1, "Python and SQL", "AWS", "Media analytics.",
     78000, 92000, ""),
    ("Juniper Robotics", "Data Engineer, Fleet Telemetry", "ashby", "Austin, TX",
     "Juniper's warehouse robots send telemetry every second; the data team keeps the fleet healthy.",
     "fleet health dashboards and failure prediction", 3, "Kafka, Spark and Python", "AWS", "IoT or telemetry data.",
     135000, 160000, "Sponsorship available."),
    ("Vantage Clinics", "Data Analyst / Engineer", "manual", "Phoenix, AZ (On-site)",
     "A regional clinic group building its first data warehouse.",
     "clinic operations reports", 2, "SQL and Power BI", "Azure", "Healthcare scheduling data.",
     72000, 85000, "This role is not eligible for visa sponsorship."),
    ("Northwind Logistics", "Data Engineer, Carrier Integrations", "greenhouse", "Austin, TX (Hybrid)",
     "The carrier integrations team connects 600 carriers' feeds into Northwind's shipment model.",
     "shipment tracking", 3, "Python, Airflow and Postgres", "AWS", "EDI formats.",
     125000, 148000, "We sponsor H-1B visas for this role."),
    ("Brightline Health", "Senior Analytics Engineer", "lever", "Remote (US)",
     "Analytics engineering at Brightline owns the semantic layer for clinical and financial metrics.",
     "executive and clinical dashboards", 5, "dbt, Looker and Snowflake", "AWS", "Metric layer design.",
     150000, 172000, "Visa sponsorship is available."),
    ("Quarry Labs", "Machine Learning Data Engineer", "ashby", "Remote (US)",
     "The ML platform team prepares training data for build-failure prediction.",
     "model training and evaluation", 3, "Spark, Python and feature pipelines", "GCP", "MLOps tooling.",
     155000, 185000, "We sponsor visas."),
    ("Tidewater Insurance", "Data Quality Engineer", "smartrecruiters", "Remote (US)",
     "A new team owning data contracts between policy systems and the warehouse.",
     "data contracts and quality monitoring", 3, "Great Expectations, dbt and Airflow", "AWS", "Data contracts.",
     112000, 134000, "H-1B sponsorship available."),
    ("Lakeshore State University", "Research Data Engineer", "workable", "Austin, TX (Hybrid)",
     "The research computing group runs the data platform behind the university's public-health studies.",
     "research datasets and study dashboards", 3, "Python, Airflow and Postgres", "AWS", "Research or public-health data.",
     98000, 118000, "This position is eligible for H-1B sponsorship."),
]

# posting index -> (status, match score, days since created, attention reason, recruiter)
_APPLICATIONS = {
    0: ("Interviewing", 88, 34, None, ("Priya Raman", "priya.raman@example.com")),
    1: ("Offer", 84, 41, None, ("Marcus Webb", "marcus.webb@example.com")),
    2: ("Interviewing", 81, 22, None, ("Elena Ortiz", "elena.ortiz@example.com")),
    3: ("Applied", 76, 12, None, None),
    4: ("Applied", 79, 16, None, ("Sam Okafor", "sam.okafor@example.com")),
    5: ("Not Selected", 58, 39, None, None),
    6: ("Applied", 74, 9, None, None),
    7: ("Applied", 72, 6, None, None),
    8: ("Approved", 78, 3, None, None),
    9: ("Needs Review", 83, 2,
        "Posting asks for Flink; the profile shows Kafka and Spark streaming but no Flink. Confirm before applying.", None),
    10: ("Pending Confirmation", 71, 1, None, None),
    11: ("Rejected", 49, 18, None, None),
    12: ("Approved", 80, 2, None, None),
    13: ("Needs Review", 54, 1, "Posting says no visa sponsorship; the profile needs sponsorship in the future.", None),
    14: ("Pending Confirmation", 73, 0, None, None),
    15: ("Not Selected", 69, 30, None, None),
    16: ("Ingested", 0, 0, None, None),
    17: ("Ingested", 0, 0, None, None),
    18: ("Approved", 77, 4, None, None),
}


def _score_breakdown(score: int, tier: str, signal: bool, days: int) -> dict:
    ai = round(score * 0.30)
    sponsor = {"A": 30, "B": 20, "C": 8, "X": 0}[tier]
    wage, title, site = 14, 10, 4
    recency = 2 if days < 7 else 1
    bonus = 10 if signal else 0
    comps = {
        "ai_profile_fit": {"raw": score, "weight": 30, "contribution": ai},
        "sponsorship_history": {"raw": tier, "weight": 30, "contribution": sponsor},
        "wage_level_fit": {"raw": "II", "weight": 20, "contribution": wage},
        "role_title_match": {"raw": None, "weight": 12, "contribution": title},
        "worksite_clarity": {"raw": None, "weight": 6, "contribution": site},
        "posting_recency": {"raw": round(1 / (days + 1), 2), "weight": 2, "contribution": recency},
        "sponsorship_signal_bonus": {"raw": signal, "weight": 10, "contribution": bonus},
    }
    return {"components": comps, "total": min(100, sum(c["contribution"] for c in comps.values()))}


def _match_analysis(score: int, posting: tuple) -> dict:
    stack = [s.strip() for s in posting[7].replace(" and ", ", ").split(",")]
    have = {"Airflow", "Spark", "dbt", "Kafka", "Python", "SQL", "Terraform", "Spark Structured Streaming"}
    matching = [s for s in stack if s in have] + ["Data quality frameworks", "Python", "SQL"]
    missing = [s for s in stack if s not in have]
    return {
        "match_score": score,
        "matching_skills": list(dict.fromkeys(matching)),
        "missing_keywords": missing,
        "visa_sponsorship": "Yes" if posting[12] and "not" not in posting[12] else ("No" if "not" in posting[12] else "Unknown"),
        "strengths": "Owns ingestion end to end at scale (500GB/day, 12 sources) and cut a nightly batch from 4 hours to 45 "
                     "minutes, which maps directly to this team's reliability and freshness goals.",
        "gaps_analysis": ("No production experience with " + ", ".join(missing) + " is shown in the profile; the streaming "
                          "learning project is the closest evidence.") if missing else
                         "No hard gaps; the posting's domain is new to the profile, so lead with transferable pipeline work.",
    }


def _tailored_resume(profile: dict, posting: tuple) -> dict:
    company, title = posting[0], posting[1]
    resume = json.loads(json.dumps(profile))
    resume["title"] = title
    resume["summary"] = (
        f"Data engineer with 4 years running production pipelines, including ingestion of roughly 500GB a day from 12 "
        f"sources and a batch-to-incremental migration that cut nightly runtime from 4 hours to 45 minutes. "
        f"Ready to bring the same ownership of freshness and data quality to {posting[5]} at {company}."
    )
    return resume


def _cover_letter(posting: tuple) -> str:
    company, title = posting[0], posting[1]
    return (
        f"Dear {company} Hiring Team,\n\n"
        f"I am applying for the {title} role. {posting[4]} That kind of work only holds up when the pipelines "
        f"underneath it are boring in the best way: on time, tested, and easy to debug at 2 a.m.\n\n"
        f"At Fictional Analytics Co I own ingestion for about 500GB a day across 12 upstream sources in Airflow. "
        f"When our nightly reporting batch kept running past the start of the business day, I moved it to an "
        f"incremental model and brought it from 4 hours to 45 minutes. I also built the schema and freshness checks "
        f"that now catch upstream breakages before analysts see them.\n\n"
        f"I would like to bring that same ownership to {posting[5]}. Thank you for your time; I would welcome the "
        f"chance to talk.\n\nSincerely,\nJordan Ellis"
    )


def _interview_prep(posting: tuple) -> dict:
    company = posting[0]
    return {
        "general_prep_json": {
            "strengths_to_emphasize": [
                "Ingestion ownership at 500GB/day across 12 sources, with Airflow orchestration.",
                "Batch-to-incremental migration: 4 hours down to 45 minutes.",
                "A data quality framework that catches upstream breakages before analysts do.",
            ],
            "potential_gaps_to_address": [
                "Streaming experience is from a learning project, not production. Say so plainly and show what you learned.",
                f"No prior {company.split()[1].lower()} domain experience; connect it to the shipment-like event volumes you already handle.",
            ],
        },
        "company_prep_json": {
            "company_context": f"{company} (fictional, for this demo). {posting[4]}",
            "recent_publications": [],
            "why_this_company_talking_points": [
                f"The team's data directly drives {posting[5]}, so reliability is visible to the business every day.",
                "The stack matches what you run today, so you can contribute in the first month.",
            ],
            "questions_to_ask_them": [
                "What does the on-call rotation look like for the data platform?",
                "Which pipeline causes the most pages today, and why?",
                "How do analysts report a data issue, and how fast is it usually fixed?",
            ],
            "grounded_in_real_research": False,
        },
        "process_research_json": {
            "summary": "Demo data: no real interview-process research is run for fictional companies.",
            "sources": [],
        },
        "predicted_rounds_json": {
            "rounds": [
                {
                    "round_name": "Recruiter screen",
                    "what_it_tests": "Motivation, role fit, salary range and work authorisation.",
                    "likely_interviewer": "Technical recruiter",
                    "prep_focus": ["A 60-second story of your current role", "Why this team, specifically"],
                    "qa_pairs": [
                        {
                            "question": "Walk me through your current role.",
                            "category": "background",
                            "draft_answer": "I'm a data engineer at Fictional Analytics Co, where I own ingestion for 12 upstream "
                                            "sources, about 500GB a day, orchestrated in Airflow. The project I'm proudest of "
                                            "moved our nightly reporting batch to an incremental model, from 4 hours to 45 minutes.",
                            "quick_reference": "12 sources, 500GB/day, 4h to 45m",
                            "possible_follow_ups": ["What made the migration hard?", "Who uses that data?"],
                        },
                    ],
                    "other_possible_questions": ["What are you looking for in your next role?"],
                    "questions_to_ask_them": ["What does success look like in the first 90 days?"],
                },
                {
                    "round_name": "Technical: pipeline design",
                    "what_it_tests": "Designing a reliable pipeline end to end, including failure handling.",
                    "likely_interviewer": "Senior data engineer",
                    "prep_focus": ["Idempotent loads", "Late-arriving data", "Backfills", "Data quality checks"],
                    "qa_pairs": [
                        {
                            "question": "Design a pipeline that ingests carrier tracking events and keeps an ETA table fresh.",
                            "category": "system design",
                            "draft_answer": "Land raw events in object storage partitioned by event date, stream them through "
                                            "Kafka into an incremental model keyed on shipment ID, and merge late events with "
                                            "a watermark. Add freshness and volume checks that page before dispatchers notice.",
                            "quick_reference": "raw zone, Kafka, incremental merge, watermark, freshness alerts",
                            "possible_follow_ups": ["How do you backfill a week of bad data?", "What if a carrier sends duplicates?"],
                        },
                        {
                            "question": "Tell me about a time a pipeline broke in production.",
                            "category": "behavioral",
                            "draft_answer": "An upstream vendor renamed a column without notice and our dashboards went blank. "
                                            "I added schema checks at ingestion so the next change stopped the load and alerted "
                                            "us instead of silently loading nulls.",
                            "quick_reference": "vendor schema change, schema checks at ingestion",
                            "possible_follow_ups": ["How did you communicate it to analysts?"],
                        },
                    ],
                    "other_possible_questions": ["How do you test dbt models?", "Batch or streaming for this use case?"],
                    "questions_to_ask_them": ["How do you handle schema changes from upstream teams today?"],
                },
                {
                    "round_name": "Hiring manager",
                    "what_it_tests": "Ownership, prioritisation and how you work with analysts.",
                    "likely_interviewer": "Data engineering manager",
                    "prep_focus": ["Prioritising requests", "Saying no with a reason"],
                    "qa_pairs": [
                        {
                            "question": "How do you decide what to work on when every team says their request is urgent?",
                            "category": "behavioral",
                            "draft_answer": "I ask what decision the data feeds and when it's made. A dashboard read in Monday's "
                                            "ops meeting beats a nice-to-have report, and I write the trade-off down so the "
                                            "requester sees why.",
                            "quick_reference": "decision + deadline, write the trade-off down",
                            "possible_follow_ups": ["Give an example where you pushed back."],
                        },
                    ],
                    "other_possible_questions": ["Where do you want to be in two years?"],
                    "questions_to_ask_them": ["What would you want this hire to fix first?"],
                },
            ],
            "answering_method_guidance": "Use STAR for behavioral questions: one concrete situation, your own actions, and a number in the result.",
            "grounded_in_real_research": False,
            "grounding_warnings": [],
        },
    }


_PROFILE_PREP = {
    "pitch_30_seconds": "I'm a data engineer with four years of building pipelines people rely on. At Fictional Analytics Co I "
                        "own ingestion for 12 sources, about 500GB a day, and I cut our nightly batch from 4 hours to 45 "
                        "minutes. I'm looking for a team where data reliability is visible to the business.",
    "pitch_90_seconds": "I started on a retail analytics team, building dbt models and dashboards for six regional teams, "
                        "which taught me that the hard part is agreeing on one number. At Fictional Analytics Co I own "
                        "ingestion end to end: 12 upstream sources, roughly 500GB a day, orchestrated in Airflow. The work I'm "
                        "proudest of is moving our nightly reporting to incremental models, from 4 hours to 45 minutes, and "
                        "adding schema and freshness checks so upstream changes stop the load instead of breaking dashboards. "
                        "Next I want to go deeper on streaming in production.",
    "answers": [
        {"question": "Tell me about yourself.",
         "draft_answer": "Four years in data engineering, most recently owning ingestion for 12 sources at Fictional Analytics "
                         "Co. I like being the person who knows why a number changed.",
         "quick_reference": "4 yrs, 12 sources, 500GB/day"},
        {"question": "What's your biggest technical achievement?",
         "draft_answer": "Moving the nightly reporting batch to incremental models, cutting runtime from 4 hours to 45 minutes "
                         "without new hardware, with a weekly full rebuild as a safety net.",
         "quick_reference": "4h to 45m, incremental + weekly rebuild"},
        {"question": "Why are you looking to move?",
         "draft_answer": "I've built the platform I was hired to build. I want a larger data volume and production streaming work.",
         "quick_reference": "scale + streaming"},
    ],
    "strengths_to_steer_toward": ["End-to-end pipeline ownership", "Data quality as a habit, not a project",
                                  "Clear communication with analysts"],
    "honest_gaps": [
        {"gap": "Production streaming", "honest_answer": "My streaming work is a learning project with Kafka and Spark. I know "
                                                         "the trade-offs and I'm looking for a team where I can run it for real."},
    ],
    "authored_note": "Sample prep for the demo candidate.",
    "grounding_warnings": [],
    "missing_questions": [],
}


def _clear(db: Session) -> None:
    for model in (MockInterviewTurn, MockInterviewSession, InterviewPrep, OutreachMessage, TailoredDocument,
                  JobApplication, JobPosting, Company, LlmUsageLog, ActivityLog, BehavioralStory, SavedAnswer,
                  ResearchAgentQuery, AdaptationLog, AdaptiveParameterValue, ProfileInterviewPrep, AdminAccount):
        db.query(model).delete(synchronize_session=False)
    db.query(ProfileVersion).delete(synchronize_session=False)
    db.query(ProfileVariant).delete(synchronize_session=False)
    db.commit()


def _seed_profile(db: Session) -> tuple[int, dict]:
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as f:
        text = f.read()
    variant = profile_service.create_variant(db, "Demo Profile", is_default=True)
    profile_service.create_manual_version(db, variant.id, text)
    return variant.id, json.loads(text)


def seed_demo_data(db: Session) -> bool:
    """Fill an empty demo database. Returns False (and writes nothing) when applications already exist."""
    _guard(db)
    if db.query(JobApplication).first():
        return False
    now = utcnow()

    variant = db.query(ProfileVariant).first()
    if variant is None:
        variant_id, profile = _seed_profile(db)
    else:
        variant_id = variant.id
        with open(_FIXTURE_PATH, "r", encoding="utf-8") as f:
            profile = json.load(f)

    settings = get_or_create_settings(db)
    settings.automation_enabled = False

    companies = {}
    for name, field, slug, tier, approvals in _COMPANIES:
        c = Company(name=name, normalized_name=normalize_company_name(name), tier=tier, tier_computed_at=now,
                    h1b_approvals_total=approvals, h1b_denials_total=max(0, approvals // 25),
                    h1b_last_fiscal_year=2025 if approvals else None, created_at=now - timedelta(days=45))
        if field:
            setattr(c, field, slug)
        if name in _CAP_EXEMPT:
            c.is_cap_exempt, c.cap_exempt_reason = True, _CAP_EXEMPT[name]
        db.add(c)
        companies[name] = c
    db.flush()

    apps = {}
    for i, p in enumerate(_POSTINGS):
        status, score, days, attention, recruiter = _APPLICATIONS[i]
        company = companies[p[0]]
        created = now - timedelta(days=days, hours=3 + i % 5)
        signal = bool(p[12]) and "not" not in p[12]
        posting = JobPosting(
            company_id=company.id, company_name_raw=p[0], job_title=p[1], location=p[3], source=p[2],
            job_url=None,  # fictional employers have no real posting to link to
            external_id=f"demo-{i + 1}",
            job_description=_JD_TEMPLATE.format(team=p[4], consumers=p[5], years=p[6], stack=p[7], cloud=p[8],
                                                 nice=p[9], lo=p[10], hi=p[11], sponsor=p[12]),
            first_seen_at=created, last_seen_at=now, created_at=created,
            sponsorship_signal=signal, sponsorship_blocked="not eligible" in p[12],
            eligibility_flag_reason="Posting states no visa sponsorship" if "not eligible" in p[12] else None,
            offered_salary_min=p[10], offered_salary_max=p[11], offered_salary_source="parsed",
            wage_level_per_posting="III" if p[10] >= 140000 else ("II" if p[10] >= 100000 else "I"),
        )
        db.add(posting)
        db.flush()

        app = JobApplication(posting_id=posting.id, status=status, profile_variant_id=variant_id, created_at=created,
                             visa_sponsorship="Yes" if signal else ("No" if posting.sponsorship_blocked else "Unknown"),
                             attention_reason=attention)
        if status != "Ingested":
            app.match_score = score
            app.match_analysis_json = json.dumps(_match_analysis(score, p))
            app.score_breakdown = _score_breakdown(score, company.tier, signal, days)
            app.cover_letter_score = min(95, score + 4)
        if recruiter:
            app.recruiter_name, app.recruiter_email = recruiter
        if status in ("Approved", "Applied", "Interviewing", "Offer", "Not Selected"):
            app.confirmed_by_user = True
        if status == "Pending Confirmation":
            app.confirmation_deadline = now + timedelta(hours=20 - i)
        if status in ("Applied", "Interviewing", "Offer", "Not Selected"):
            app.applied_at = created + timedelta(days=1)
        if status in ("Interviewing", "Offer"):
            app.replied_at = created + timedelta(days=6)
            app.interviewing_at = created + timedelta(days=8)
        if status == "Offer":
            app.offer_at = now - timedelta(days=2)
            app.notes = "Offer call on Tuesday. Base $146k, 10% bonus. Asked for the written offer and benefits summary."
        if status == "Not Selected":
            app.not_selected_at = now - timedelta(days=max(1, days - 14))
        if status == "Rejected":
            app.rejected_at = created + timedelta(hours=6)
            app.notes = "Skipped: junior role, well below current level."
        db.add(app)
        db.flush()
        apps[i] = app

        if status != "Ingested":
            db.add(TailoredDocument(application_id=app.id, document_type="resume", ats_score=min(96, score + 7),
                                    content=json.dumps(_tailored_resume(profile, p)), generated_at=created + timedelta(minutes=4)))
            db.add(TailoredDocument(application_id=app.id, document_type="cover_letter", ats_score=app.cover_letter_score,
                                    content=_cover_letter(p), generated_at=created + timedelta(minutes=5)))

    for i in (0, 2):
        prep = _interview_prep(_POSTINGS[i])
        db.add(InterviewPrep(application_id=apps[i].id, is_active=True, used_tailored_resume=True,
                             generated_at=now - timedelta(days=3 + i),
                             **{k: json.dumps(v) for k, v in prep.items()}))

    session = MockInterviewSession(
        application_id=apps[0].id, round_name="Technical: pipeline design", tier="guided", status="completed",
        used_tailored_resume=True, started_at=now - timedelta(days=2, minutes=25), ended_at=now - timedelta(days=2),
        debrief_json=json.dumps({
            "accuracy_notes": ["Every number you used (500GB/day, 12 sources, 4h to 45m) matches your profile."],
            "strengths": ["Opened with the data consumer before the tools.", "Named a concrete failure mode and how you'd alert on it."],
            "areas_to_improve": [{
                "issue": "Backfill answer stayed abstract",
                "what_you_said": "I'd just rerun the DAG for those days.",
                "why_it_matters": "Interviewers listen for idempotency and how you protect downstream tables during a rerun.",
                "example_better_answer": "Because each load overwrites its own date partition, I'd rerun the affected dates in "
                                         "a staging schema, compare row counts and key metrics, then swap the partitions in.",
            }],
            "delivery_feedback": "Clear pace. Two long pauses while choosing between Kafka and batch; say the trade-off out loud instead.",
            "scorecard": {"communication_clarity": 4, "content_accuracy": 5, "structure": 3, "confidence_and_directness": 4},
            "comparison": {"has_previous": False, "trend": "first_session", "note": "", "warning": ""},
            "overall_summary": "A solid design answer grounded in real experience. Practise one concrete backfill story before the real round.",
        }),
    )
    db.add(session)
    db.flush()
    turns = [
        ("interviewer", "Design a pipeline that ingests carrier tracking events and keeps an ETA table fresh. Where would you start?", False),
        ("candidate", "I'd start with who reads the ETA table and how fresh it has to be. Dispatchers need minutes, so events go "
                      "through Kafka into an incremental model keyed on shipment ID, with the raw events also landed in S3.", False),
        ("interviewer", "A carrier resends a week of events with corrections. How do you backfill?", True),
        ("candidate", "I'd just rerun the DAG for those days, and check the row counts afterwards.", False),
    ]
    for idx, (speaker, content, followup) in enumerate(turns):
        db.add(MockInterviewTurn(session_id=session.id, turn_index=idx, speaker=speaker, content=content,
                                 is_followup=followup, created_at=session.started_at + timedelta(minutes=3 * idx),
                                 recording_duration_seconds=42.0 if speaker == "candidate" else None,
                                 pause_count=2 if speaker == "candidate" else None,
                                 longest_pause_seconds=3.1 if speaker == "candidate" else None))

    db.add(OutreachMessage(
        application_id=apps[4].id, channel="linkedin_connection", recipient_name="Sam Okafor",
        recipient_address="https://www.linkedin.com/in/example-sam-okafor", status="Draft", created_at=now - timedelta(days=1),
        body="Hi Sam, I applied for the Data Platform Engineer role at Larkspur. I run ingestion for 12 sources at about 500GB "
             "a day and would value 10 minutes to hear what the team is focused on this quarter. Thanks, Jordan"))
    db.add(OutreachMessage(
        application_id=apps[0].id, channel="email", recipient_name="Priya Raman", recipient_address="priya.raman@example.com",
        subject="Thank you - Senior Data Engineer interview", status="Pending Confirmation", email_verified=True,
        confirmation_deadline=now + timedelta(hours=12), created_at=now - timedelta(hours=5),
        body="Hi Priya,\n\nThank you for setting up today's pipeline design round. I enjoyed the carrier backfill question and "
             "have been thinking about how I'd validate the swapped partitions. Happy to share that write-up if it helps.\n\nBest,\nJordan"))
    db.add(OutreachMessage(
        application_id=apps[2].id, channel="email", recipient_name="Elena Ortiz", recipient_address="elena.ortiz@example.com",
        subject="Following up - Data Engineer, Risk", status="Sent", email_verified=True,
        sent_at=now - timedelta(days=9), created_at=now - timedelta(days=9, hours=2),
        body="Hi Elena,\n\nI applied for the Data Engineer, Risk role last week and wanted to share one relevant detail: I built "
             "the schema and freshness checks that keep our fraud-adjacent reporting accurate. I'd be glad to talk.\n\nBest,\nJordan"))

    for title, s, t, a, r, traits, src in [
        ("Cutting the nightly batch from 4 hours to 45 minutes",
         "Nightly reporting regularly finished after the business day had started.",
         "Make the reports ready before 7 a.m. without new hardware.",
         "Rebuilt the full-refresh models as incremental models keyed on update time and added a weekly full rebuild as a safety net.",
         "Runtime fell from 4 hours to 45 minutes, and reports were ready on time every day of the next quarter.",
         ["ownership", "technical depth"], "Data Engineer, Fictional Analytics Co"),
        ("Catching a vendor schema change before analysts did",
         "A vendor renamed a column without notice and dashboards silently showed blanks.",
         "Stop silent breakages from upstream schema changes.",
         "Added schema and freshness checks at ingestion that stop the load and alert the team.",
         "The next three upstream changes were caught at ingestion, with zero blank dashboards.",
         ["reliability", "communication"], "Data Engineer, Fictional Analytics Co"),
        ("Giving six regional teams one version of sales",
         "Six regional teams each kept their own sales spreadsheet with different numbers.",
         "Give every region the same weekly numbers.",
         "Built shared dbt models and dashboards, and walked each regional lead through the definitions.",
         "One weekly sales dashboard replaced six spreadsheets.",
         ["collaboration"], "Junior Data Engineer, Sample Retail Group"),
    ]:
        db.add(BehavioralStory(variant_id=variant_id, title=title, situation=s, task=t, action=a, result=r,
                               traits_json=json.dumps(traits), source_reference=src, status="confirmed"))

    for q, ans, n in [
        ("Are you legally authorized to work in the United States?", "Yes", 9),
        ("Will you now or in the future require visa sponsorship?", "Yes, in the future", 9),
        ("How did you hear about this position?", "Company careers page", 6),
        ("What are your salary expectations?", "$130,000 - $150,000 base, flexible for the full package", 4),
    ]:
        db.add(SavedAnswer(question_text=q, question_normalized=q.lower().rstrip("?"), answer_text=ans, times_reused=n,
                           created_at=now - timedelta(days=30), last_used_at=now - timedelta(days=n % 5)))

    for day in range(42):
        calls = 2 + (day * 7) % 5
        for k in range(calls):
            inp, out = 3200 + 400 * k, 900 + 150 * k
            db.add(LlmUsageLog(provider="anthropic", model="claude-haiku (demo record)", input_tokens=inp, output_tokens=out,
                               estimated_cost_usd=round(inp * 1e-6 + out * 5e-6, 4),
                               created_at=now - timedelta(days=day, hours=k)))

    for i, app in apps.items():
        p = _POSTINGS[i]
        db.add(ActivityLog(message=f"Intake: new posting from {p[2]} -- {p[1]} at {p[0]}", level="INFO", timestamp=app.created_at))
        if app.match_score:
            db.add(ActivityLog(message=f"Scored application {app.id} ({p[0]}): {app.match_score}%", level="INFO",
                               timestamp=app.created_at + timedelta(minutes=2)))
        if app.attention_reason:
            db.add(ActivityLog(message=f"Application {app.id} needs review: {app.attention_reason}", level="WARNING",
                               timestamp=app.created_at + timedelta(minutes=6)))

    version = db.query(ProfileVersion).filter(ProfileVersion.variant_id == variant_id,
                                              ProfileVersion.is_active == True).first()  # noqa: E712
    db.add(ProfileInterviewPrep(variant_id=variant_id, profile_version_id=version.id if version else None,
                                is_active=True, trigger="initial", generated_at=now - timedelta(days=5),
                                content_json=json.dumps(_PROFILE_PREP)))

    db.add(ResearchAgentQuery(
        question="Which of my applications are waiting on a reply for more than a week?",
        answer="Two: Fernway Energy (Analytics Engineer, applied 11 days ago) and Larkspur Analytics (Data Platform Engineer, "
               "applied 15 days ago). Larkspur has a recruiter on file, so a short follow-up there is the better first move.",
        steps_json=json.dumps([{"thought": "I need applications in Applied status and their dates.", "action": "count_applications",
                                "action_input": "Applied", "observation": "4 applications in Applied"}]),
        created_at=now - timedelta(hours=7)))

    db.commit()
    return True


def reset_demo_data(db: Session) -> None:
    """Put the shared demo back to its starting point (profile included)."""
    _guard(db)
    _clear(db)
    seed_demo_data(db)
