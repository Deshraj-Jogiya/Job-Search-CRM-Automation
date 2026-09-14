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
import sys

from ..services.queue_service import recompute_all_scores, recompute_all_tiers
from ..services.sponsorship_signals import detect_sponsorship_signals
from .cap_exempt import apply_cap_exempt_flags
from .sponsors import load_dol_lca_data, load_uscis_h1b_data
from .wages import load_oews_wage_data


def backfill_signals(db) -> dict:
    """One-shot, idempotent Phase 3 backfill: (1) recomputes sponsorship
    signal flags on every existing JobPosting from its stored
    job_description -- postings ingested before sponsorship_signals.py
    existed have NULL/default values here, this is the "already-stored
    data" side of that mechanical detector; (2) recomputes Company.tier
    for every company; (3) recomputes score_breakdown for every
    application. No LLM calls anywhere in this path."""
    from ..models import JobPosting

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


def main(argv=None) -> int:
    # Loaded lazily, here rather than at module import time, so this
    # never fires on a plain `from app.ingest.cli import backfill_signals`
    # test import (test_ingest_cli_backfill.py does exactly that) -- an
    # earlier version of this fix called load_dotenv() at module scope,
    # which loaded .env's real secrets (SMTP creds, API keys) into the
    # test process the moment anything imported this module, silently
    # changing unrelated tests' behavior. Caught via 3 reproducible test
    # failures in an unrelated file after that change, not by inspection.
    # Must still run before `..database` is imported anywhere in this
    # process, since database.py reads DATABASE_URL via os.getenv() at
    # module import time -- true here since main() is only ever reached
    # by a genuine standalone run (this file's own module docstring),
    # where nothing else has imported ..database yet.
    from dotenv import load_dotenv

    load_dotenv()
    from ..database import SessionLocal

    parser = argparse.ArgumentParser(prog="python -m app.ingest.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    uscis_parser = subparsers.add_parser("uscis", help="Load the USCIS H-1B Employer Data Hub CSV")
    uscis_parser.add_argument("csv_path")

    lca_parser = subparsers.add_parser("lca", help="Load one DOL OFLC LCA Disclosure quarterly XLSX")
    lca_parser.add_argument("xlsx_path")
    lca_parser.add_argument("fiscal_quarter", help='e.g. "FY2024Q1"')

    oews_parser = subparsers.add_parser("oews", help="Load DOL OFLC OEWS wage-level data")
    oews_parser.add_argument("file_path")
    oews_parser.add_argument("source_year", type=int)

    subparsers.add_parser("cap-exempt", help="Sweep tracked companies for cap-exempt status")
    subparsers.add_parser(
        "backfill-signals",
        help="Recompute JobPosting sponsorship signals, Company.tier, and score_breakdown for everything on record",
    )

    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        if args.command == "uscis":
            result = load_uscis_h1b_data(db, args.csv_path)
        elif args.command == "lca":
            result = load_dol_lca_data(db, args.xlsx_path, args.fiscal_quarter)
        elif args.command == "oews":
            result = load_oews_wage_data(db, args.file_path, args.source_year)
        elif args.command == "cap-exempt":
            result = apply_cap_exempt_flags(db)
        elif args.command == "backfill-signals":
            result = backfill_signals(db)
        else:
            parser.error(f"Unknown command {args.command!r}")
            return 2
    finally:
        db.close()

    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
