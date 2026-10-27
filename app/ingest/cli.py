"""
CLI entry point for the H-1B ingest pipelines. Run standalone, outside
the FastAPI process -- these files are large (the USCIS Employer Data
Hub CSV and DOL LCA Disclosure XLSX are both tens of MB) and are
expected to be run manually whenever the user drops in a new release,
not on any automatic schedule. See SETUP.md for where to get each file.

Usage (from the repo root, with venv active):
    python -m app.ingest.cli uscis data/raw/uscis/h1b_employer_data_hub.csv
    python -m app.ingest.cli lca data/raw/dol/LCA_Disclosure_Data_FY2024_Q1.xlsx FY2024Q1
    python -m app.ingest.cli oews data/raw/oews/oflc_oews_wages.xlsx 2024
    python -m app.ingest.cli cap-exempt
    python -m app.ingest.cli backfill-signals

`backfill-signals` (Phase 3) is the odd one out here -- it doesn't
parse a file at all, it recomputes Company.tier for every tracked
company and JobPosting sponsorship signals + JobApplication
score_breakdown for everything already stored, using whatever H-1B/
wage/cap-exempt data is currently on record. Run it after any of the
file-loading commands above changes that underlying data (a fresh
USCIS/LCA/OEWS load, or a cap-exempt sweep) so tier/score/signals stay
in sync -- idempotent, safe to re-run any time, no LLM calls.
"""

import argparse
import json
import sys


def backfill_signals(db) -> dict:
    """One-shot, idempotent Phase 3 backfill: (1) recomputes sponsorship
    signal flags on every existing JobPosting from its stored
    job_description -- postings ingested before sponsorship_signals.py
    existed have NULL/default values here, this is the "already-stored
    data" side of that mechanical detector; (2) recomputes Company.tier
    for every company; (3) recomputes score_breakdown for every
    application. No LLM calls anywhere in this path.

    Every import here is lazy, on purpose (see main()'s own comment for
    the full reason): this module must have NO top-level import that
    transitively touches ..database (queue_service/sponsorship_signals
    both do), or a standalone run's env still loads too late -- cli.py's
    own top-level imports run before main()'s load_dotenv() ever gets a
    chance to. A test importing just this function (test_ingest_cli_
    backfill.py) is unaffected either way: conftest.py already bound
    ..database to the test DB before any test module gets imported, so
    these lazy imports just reuse that cached, correctly-bound module."""
    from ..models import JobPosting
    from ..services.queue_service import recompute_all_scores, recompute_all_tiers
    from ..services.sponsorship_signals import detect_sponsorship_signals

    postings = db.query(JobPosting).all()
    for posting in postings:
        signals = detect_sponsorship_signals(posting.job_description)
        posting.sponsorship_blocked = signals["sponsorship_blocked"]
        posting.sponsorship_signal = signals["sponsorship_signal"]
        posting.worksite_ambiguous = signals["worksite_ambiguous"]
        posting.signal_matches = signals["signal_matches"]
    db.commit()

    tier_result = recompute_all_tiers(db)
    score_result = recompute_all_scores(db)

    return {"postings_signal_updated": len(postings), **tier_result, **score_result}


def backfill_fit(db) -> dict:
    """Applies the role-type cap and the rule-based evidence (fit_service.py) to every application
    scored before they existed. No LLM calls. Lazy imports for the same reason as backfill_signals()."""
    from ..models import JobApplication, SearchKeyword
    from ..services.matching_service import MatchingServiceError, refresh_fit_evidence

    target_titles = [k.keyword for k in db.query(SearchKeyword).filter(SearchKeyword.is_active == True).all()]  # noqa: E712
    scored = db.query(JobApplication).filter(JobApplication.match_analysis_json.isnot(None)).all()
    changed = skipped = 0
    for application in scored:
        try:
            changed += refresh_fit_evidence(db, application, target_titles)
        except MatchingServiceError:
            skipped += 1
    return {"applications_refreshed": len(scored) - skipped, "scores_capped_or_changed": changed, "skipped_no_profile": skipped}


def compare_ats_verify(db, limit: int = 20) -> dict:
    """Runs the AI and the rule-based ATS verify side by side on the most recent tailored resumes and
    reports how closely they agree, the evidence for switching ATS_VERIFY_MODE to "rules". Makes one
    paid AI call per resume (counted against the daily spend cap like any other call)."""
    import json as _json

    from ..models import JobApplication, TailoredDocument
    from ..services import llm_usage_service
    from ..services.tailoring_service import TARGET_ATS_SCORE, verify_ats_score_by_ai, verify_ats_score_by_rules

    docs = (db.query(TailoredDocument).filter(TailoredDocument.document_type == "resume")
            .order_by(TailoredDocument.generated_at.desc()).limit(limit).all())
    rows = []
    for doc in docs:
        if llm_usage_service.spend_cap_exceeded(db):
            break
        app = db.query(JobApplication).filter(JobApplication.id == doc.application_id).first()
        if not app or not app.posting:
            continue
        resume = _json.loads(doc.content)
        exp, proj, jd = resume.get("experience", []), resume.get("projects", []), app.posting.job_description
        ai, rules = verify_ats_score_by_ai(exp, proj, jd), verify_ats_score_by_rules(exp, proj, jd)
        ai_missing = {str(k).lower() for k in ai.get("missing_keywords", [])}
        rules_missing = {k.lower() for k in rules["missing_keywords"]}
        rows.append({"application_id": app.id, "ai": int(ai.get("score", 0)), "rules": rules["score"],
                     "missing_both": len(ai_missing & rules_missing), "ai_only": sorted(ai_missing - rules_missing),
                     "rules_only": sorted(rules_missing - ai_missing)})
    diffs = [abs(r["ai"] - r["rules"]) for r in rows]
    # What matters in practice is whether both would send the resume to another refine pass
    same_decision = sum((r["ai"] >= TARGET_ATS_SCORE) == (r["rules"] >= TARGET_ATS_SCORE) for r in rows)
    return {"compared": len(rows), "mean_abs_diff": round(sum(diffs) / len(diffs), 1) if diffs else None,
            "max_abs_diff": max(diffs) if diffs else None, "same_refine_decision": f"{same_decision}/{len(rows)}",
            "rows": rows}


def main(argv=None) -> int:
    # load_dotenv() must run before the VERY FIRST import anywhere in
    # this process that touches ..database (database.py reads
    # DATABASE_URL via os.getenv() at module import time) -- which is
    # why every import below, and every import inside backfill_signals()
    # above, is lazy rather than at module top level. A first attempt at
    # this fix only made THIS function's own SessionLocal import lazy
    # and left queue_service/sponsorship_signals/etc as top-level
    # imports in this file -- those transitively import ..database too,
    # so they were still binding the (wrong, sqlite-fallback) engine
    # before this line ever ran. Caught by actually running
    # backfill-signals for real against the personal instance and
    # getting a sqlite "no such table" error despite the DATABASE_URL
    # env var being real and correct.
    #
    # This is also why the load stays here, not at module scope: an
    # import-time load_dotenv() call loaded .env's real secrets (SMTP
    # creds, API keys) into the test process the moment anything
    # imported this module (test_ingest_cli_backfill.py does), which
    # silently changed unrelated autofill background-thread tests'
    # behavior -- caught via 3 reproducible failures in an unrelated
    # test file, not by inspection. main() is only ever reached by a
    # genuine standalone run (see this file's own module docstring), so
    # gating everything database-adjacent behind it keeps a plain test
    # import of backfill_signals() inert.
    from dotenv import load_dotenv

    load_dotenv()

    from ..database import SessionLocal
    from .cap_exempt import apply_cap_exempt_flags
    from .sponsors import load_dol_lca_data, load_sponsorship_alerts, load_uscis_h1b_data
    from .wages import load_oews_wage_data

    parser = argparse.ArgumentParser(prog="python -m app.ingest.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    uscis_parser = subparsers.add_parser("uscis", help="Load the USCIS H-1B Employer Data Hub CSV")
    uscis_parser.add_argument("csv_path")
    uscis_parser.add_argument("--reset", action="store_true", help="Clear every company's H-1B counts first (when replacing the years on record)")
    subparsers.add_parser("alerts", help="Apply ingest/sponsorship_alerts.json (debarments, PERM suspensions, hiring pauses)")

    lca_parser = subparsers.add_parser("lca", help="Load one DOL OFLC LCA Disclosure quarterly XLSX")
    lca_parser.add_argument("xlsx_path")
    lca_parser.add_argument("fiscal_quarter", help='e.g. "FY2024Q1"')
    lca_parser.add_argument("--reset", action="store_true", help="Clear every company's LCA counts first (when replacing the quarter on record)")

    oews_parser = subparsers.add_parser("oews", help="Load DOL OFLC OEWS wage-level data")
    oews_parser.add_argument("file_path")
    oews_parser.add_argument("source_year", type=int)

    subparsers.add_parser("cap-exempt", help="Sweep tracked companies for cap-exempt status")
    subparsers.add_parser(
        "backfill-signals",
        help="Recompute JobPosting sponsorship signals, Company.tier, and score_breakdown for everything on record",
    )

    subparsers.add_parser("backfill-fit", help="Apply the role-type cap and fit evidence to already-scored applications")
    compare_parser = subparsers.add_parser(
        "compare-ats-verify", help="Compare the AI and rule-based ATS verify on recent tailored resumes (paid AI calls)"
    )
    compare_parser.add_argument("--limit", type=int, default=20)

    # Sponsorship lookups straight from the raw government files (no database): any employer, not only tracked companies.
    subparsers.add_parser("build-lca-slim", help="Extract certified H-1B data-role filings from the LCA XLSX into data/raw/dol/lca_data_roles.csv")
    lookup_parser = subparsers.add_parser("sponsor-lookup", help="H-1B approvals, data-role LCAs, wage levels and cap-exempt status for employers")
    lookup_parser.add_argument("names", nargs="+")
    sponsors_parser = subparsers.add_parser("data-sponsors", help="Employers ranked by certified data-role LCA filings, cap-exempt first")
    sponsors_parser.add_argument("--state", help='worksite state, e.g. "AZ"')
    sponsors_parser.add_argument("--limit", type=int, default=50)
    sponsors_parser.add_argument("--min-filings", type=int, default=3)
    sponsors_parser.add_argument("--cap-exempt", action="store_true", help="cap-exempt employers only")
    sponsors_parser.add_argument("--include-dependent", action="store_true", help="keep H-1B-dependent (mostly staffing) employers")

    args = parser.parse_args(argv)

    if args.command in ("build-lca-slim", "sponsor-lookup", "data-sponsors"):
        from . import sponsor_lookup as sl

        if args.command == "build-lca-slim":
            result = {"kept_filings": sl.build_slim_lca(), "path": str(sl.LCA_SLIM)}
        elif args.command == "sponsor-lookup":
            result = sl.lookup(args.names)
        else:
            result = sl.data_sponsors(args.state, args.limit, args.min_filings, args.cap_exempt, args.include_dependent)
        print(json.dumps(result, indent=2, default=str))
        return 0

    db = SessionLocal()
    try:
        if args.command == "uscis":
            result = load_uscis_h1b_data(db, args.csv_path, reset=args.reset)
        elif args.command == "alerts":
            result = load_sponsorship_alerts(db)
        elif args.command == "lca":
            result = load_dol_lca_data(db, args.xlsx_path, args.fiscal_quarter, reset=args.reset)
        elif args.command == "oews":
            result = load_oews_wage_data(db, args.file_path, args.source_year)
        elif args.command == "cap-exempt":
            result = apply_cap_exempt_flags(db)
        elif args.command == "backfill-signals":
            result = backfill_signals(db)
        elif args.command == "backfill-fit":
            result = backfill_fit(db)
        elif args.command == "compare-ats-verify":
            result = compare_ats_verify(db, args.limit)
        else:
            parser.error(f"Unknown command {args.command!r}")
            return 2
    finally:
        db.close()

    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
