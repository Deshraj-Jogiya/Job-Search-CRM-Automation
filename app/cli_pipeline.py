"""Standalone entry point for scoring/tailoring the Ingested backlog via
the CLAUDE_CLI subscription-based provider, run as a scheduled job under
the `opc` system user on the VM (the account actually logged in to the
real `claude` CLI) -- deliberately NOT run as part of the main
career-pilot.service/uvicorn process, which runs as the unprivileged
`career-pilot` system user and has no access to opc's login. See
deploy/README.md's "CLI subscription pipeline" section for the full
systemd-timer setup and the reasoning against running the whole app as
opc.

Run manually for a bounded test batch:
    CLI_PIPELINE_MAX_CALLS=20 python -m app.cli_pipeline

Reuses confirmation_service's own, already-debugged score-then-tailor
logic (_score_and_maybe_tailor_one) instead of a second copy of it --
only adds two things on top: a real, free mechanical pre-filter (skips
obviously unscoreable rows without spending a call) and a hard cap on
LLM calls per run, since a subscription's usage allowance is shared with
Deshraj's own live, interactive use of Claude Code and the desktop app --
this must never be able to exhaust that for the rest of the day.
"""

import os
import sys
import time

# LLM_PROVIDER must be set before ANYTHING imports app.services.llm,
# since get_llm_provider() is a functools.lru_cache(maxsize=1) singleton
# -- once resolved to a provider instance, that choice sticks for the
# rest of the process. This process exists for exactly one purpose (run
# the backlog through the subscription CLI), so hardcoding it here
# (rather than trusting whatever's in .env, which the main app also
# reads) is deliberate: this script's behavior can never silently follow
# a change meant for the interactive web app's provider.
os.environ["LLM_PROVIDER"] = "claude_cli"
os.environ.setdefault("CLAUDE_CLI_MODEL_SCORING", "haiku")
os.environ.setdefault("CLAUDE_CLI_MODEL_TAILORING", "sonnet")

# Real threshold from Deshraj's own explicit instruction (2026-09-21):
# tailor only applications scoring 70 or higher. Deliberately NOT read
# from GlobalSettings.min_score_for_auto_tailor -- that field is shared
# with the main app's own scheduler tick (currently disabled via
# automation_enabled=False, but if it's ever re-enabled against the
# metered API again, its threshold is a separate decision this script
# must not silently inherit or override).
TAILOR_THRESHOLD = int(os.getenv("CLI_PIPELINE_TAILOR_THRESHOLD", "70"))

# Hard per-run call cap. Unknown real number for what a Claude Pro plan
# actually allows for this kind of unattended, scripted use in a given
# window -- deliberately conservative until measured live. Raise this
# only after confirming (via the usage log, same as every other cost
# claim in this project) that a given cap didn't come close to
# exhausting the day's real interactive usage too.
MAX_CALLS_PER_RUN = int(os.getenv("CLI_PIPELINE_MAX_CALLS", "60"))

# Sequential by default -- confirmed live the VM has only 1 vCPU and
# ~660MB free RAM with the main app + Xvfb + noVNC already running,
# alongside a real end-to-end tailoring run costing 1259s of wall time
# across several `claude` subprocess invocations. Raise this only after
# confirming (via `free -h` on the VM) there's real memory headroom to
# run more than one `claude` process at once -- this also shares the
# same subscription usage pool Deshraj's own interactive Claude Code
# session draws from, so a wide pool isn't free even where RAM allows it.
CONCURRENCY = int(os.getenv("CLI_PIPELINE_CONCURRENCY", "1"))

# Batch size pulled per pass -- kept small deliberately so the cap is
# checked often, not once per giant batch.
BATCH_SIZE = int(os.getenv("CLI_PIPELINE_BATCH_SIZE", "5"))


def _today_call_count(db) -> int:
    from datetime import datetime, timezone
    from sqlalchemy import func
    from .models import LlmUsageLog

    start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        db.query(func.count(LlmUsageLog.id))
        .filter(LlmUsageLog.provider == "claude_cli_subscription", LlmUsageLog.created_at >= start_of_day)
        .scalar()
        or 0
    )


def _prefilter_mark_unscoreable(db) -> int:
    """Real, free mechanical filter: marks obviously-unscoreable rows
    (too little real JD text to say anything meaningful about, or
    already flagged stale by the existing staleness sweep) with a real
    match_analysis_json marker instead of leaving them NULL -- so
    progress_ingested_applications's own "never scored" query never
    reselects them on a later run, without ever spending an LLM call to
    find that out. This never scores or tailors anything itself; it only
    prevents wasted calls on rows that can't produce a real result."""
    import json
    from .database import utcnow
    from .models import JobApplication, JobPosting

    candidates = (
        db.query(JobApplication, JobPosting)
        .join(JobPosting, JobApplication.posting_id == JobPosting.id)
        .filter(JobApplication.status == "Ingested", JobApplication.match_analysis_json.is_(None))
        .all()
    )
    skipped = 0
    for application, posting in candidates:
        reason = None
        if not posting.job_description or len(posting.job_description.strip()) < 50:
            reason = "prefilter_skip: job description too short to score"
        elif posting.staleness_flag:
            reason = "prefilter_skip: posting flagged stale"
        if reason:
            application.match_analysis_json = json.dumps({"skipped": True, "reason": reason})
            application.updated_at = utcnow()
            skipped += 1
    if skipped:
        db.commit()
    return skipped


def run() -> None:
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import or_
    from .database import SessionLocal
    from .models import JobApplication
    from .services import confirmation_service
    from .services.activity_logger import log_activity

    db = SessionLocal()
    try:
        skipped = _prefilter_mark_unscoreable(db)
        if skipped:
            print(f"Pre-filter: skipped {skipped} unscoreable postings for $0, no LLM call.")

        already_used = _today_call_count(db)
        budget = MAX_CALLS_PER_RUN - already_used
        if budget <= 0:
            print(f"Daily call cap ({MAX_CALLS_PER_RUN}) already reached today ({already_used} calls). Exiting.")
            return
        print(f"Daily cap {MAX_CALLS_PER_RUN}, already used {already_used}, budget this run: {budget}")

        total_scored = total_tailored = total_calls_est = 0
        while total_calls_est < budget:
            application_ids = [
                row[0]
                for row in db.query(JobApplication.id)
                .filter(
                    JobApplication.status == "Ingested",
                    or_(
                        JobApplication.match_analysis_json.is_(None),
                        JobApplication.match_score >= TAILOR_THRESHOLD,
                    ),
                )
                .order_by(JobApplication.id.asc())
                .limit(BATCH_SIZE)
                .all()
            ]
            if not application_ids:
                print("Backlog cleared -- nothing left to score or tailor.")
                break

            with ThreadPoolExecutor(max_workers=min(len(application_ids), CONCURRENCY)) as pool:
                results = list(pool.map(
                    lambda aid: confirmation_service._score_and_maybe_tailor_one(aid, TAILOR_THRESHOLD),
                    application_ids,
                ))
            for aid, outcome in results:
                print(f"  application {aid}: {outcome}")
                if outcome == "tailored":
                    total_tailored += 1
                    total_calls_est += 5  # rough: 1 score + ~4 tailoring passes
                else:
                    total_scored += 1
                    total_calls_est += 1

            used_now = _today_call_count(db)
            if used_now >= MAX_CALLS_PER_RUN:
                print(f"Daily call cap reached mid-run ({used_now}/{MAX_CALLS_PER_RUN}). Stopping for today.")
                break
            time.sleep(2)  # brief pause between batches, not a hard rate need -- just avoids a tight spin loop

        print(f"Run complete. Scored-only: {total_scored}, tailored: {total_tailored}.")
        log_activity(
            db,
            f"CLI pipeline run: scored {total_scored}, tailored {total_tailored}, "
            f"real subscription calls today: {_today_call_count(db)}/{MAX_CALLS_PER_RUN}.",
            "INFO",
        )
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(run() or 0)
