# H-1B data ingest setup

`app/ingest/` loads three real government data files to layer H-1B
sponsorship/wage signal onto companies this app already tracks. None
of this is downloaded automatically -- these are large, infrequently
-updated files you drop in by hand, then load with one CLI command
each. Nothing here blocks any other feature if you skip it entirely.

## 1. USCIS H-1B Employer Data Hub (sponsorship history)

- Source: <https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub>. The archive page only has yearly CSVs up to
  FY2023; newer years come from the hub's Tableau view, which exports one fiscal year per request in a long format (one row per
  employer per measure):
  ```bash
  curl -sA "Mozilla/5.0" -o data/raw/uscis/uscis_hub_long_2025.csv     "https://bigdataanalyticspub-sb.uscis.dhs.gov/views/H1BEmployerDataHub-Final/H1BPublic.csv?Fiscal%20Year%20%20%20=2025"
  ```
  (the filter name really has three trailing spaces; without the filter you get the current year). Pivot those files to the
  `Fiscal Year, Employer, Initial Approval, Initial Denial, Continuing Approval, Continuing Denial` columns: Initial = New Employment +
  New Concurrent + Change of Employer, Continuing = Continuation + Change with Same Employer + Amended.
- Load it, with `--reset` when it replaces the years on record (so a company missing from the new file loses its old count):
  ```bash
  python -m app.ingest.cli uscis data/raw/uscis/h1b_hub_fy2024_2026.csv --reset
  ```
- Only updates companies already in your `companies` table -- it does not create new ones. Names match exactly, through
  `app/ingest/sponsor_name_aliases.json`, with spaces ignored, or through a unique "dba" entity.

## 2. DOL OFLC LCA Disclosure Data (quarterly, sponsorship intent + wage)

- Source: <https://www.dol.gov/agencies/eta/foreign-labor/performance>
  ("LCA Programs (H-1B, H-1B1, E-3)" quarterly disclosure files).
- Download the quarterly XLSX for the period you want.
- Save it to `data/raw/dol/`.
- Load it, naming the fiscal quarter yourself (the file itself doesn't
  reliably encode this in a parseable way):
  ```bash
  python -m app.ingest.cli lca data/raw/dol/LCA_Disclosure_Data_FY2024_Q1.xlsx FY2024Q1
  ```
- Only counts CERTIFIED H-1B filings (withdrawn/denied rows and other
  visa classes in the same file are ignored). Re-running with the same
  `fiscal_quarter` argument overwrites, it doesn't double-count.

## 3. DOL OFLC OEWS wage-level data (prevailing wage benchmarks)

- Source: <https://flag.dol.gov/wage-data/wage-search> or the OFLC
  Wage Library download for the year you want.
- Save it to `data/raw/oews/`.
- Load it with the release year:
  ```bash
  python -m app.ingest.cli oews data/raw/oews/oflc_oews_wages.xlsx 2024
  ```

## 3b. Sponsorship alerts (debarments, PERM suspensions, hiring pauses)

`app/ingest/sponsorship_alerts.json` is a hand-checked list of current government actions: the DOL Wage and Hour H-1B debarment list
(<https://www.dol.gov/agencies/whd/immigration/h1b/debarment>), the OFLC program debarment list, announced PERM suspensions, and
hiring pauses a company has confirmed. A debarred employer is tier X until its ban ends; the others are shown next to the tier. Check
both DOL lists and the news monthly, edit the file, then:

```bash
python -m app.ingest.cli alerts
python -m app.ingest.cli backfill-signals
```

New companies on the list are flagged as soon as intake creates them.

## 4. Cap-exempt sweep (no file needed)

Flags companies matching `app/ingest/cap_exempt_seeds.json` (a curated
list of national labs/nonprofit research orgs + a university-name
pattern) -- best-effort, name-based, not authoritative. Run any time:

```bash
python -m app.ingest.cli cap-exempt
```

## 5. Backfill signals/tier/scores (Phase 3, no file needed)

After loading any of the above (or re-running one with fresh data),
recompute everything downstream so `/queue` and `/metrics` reflect it
-- mechanical only, no LLM calls, safe to re-run any time:

```bash
python -m app.ingest.cli backfill-signals
```

This (1) recomputes `JobPosting` sponsorship-signal flags for every
posting on record from its stored `job_description` (postings ingested
before `sponsorship_signals.py` existed have stale/default values
otherwise), (2) recomputes `Company.tier` from current H-1B/LCA/
cap-exempt data, (3) recomputes `JobApplication.score_breakdown` for
every application. See `WEIGHTING.md` for what the score actually
means.

## If a loader errors with "None of the expected column names were found"

DOL/USCIS change header spelling across releases. The error message
lists the real columns in your file -- add the actual header as a new
alias in the matching `_*_ALIASES` list in `app/ingest/sponsors.py` or
`app/ingest/wages.py` (see `app/ingest/column_utils.py` for how
resolution works), then re-run. This is expected maintenance, not a
sign anything else is broken.

## What this does NOT do

- No live scraping/API polling of USCIS or DOL -- everything here is a
  manual file drop, by design (their public data isn't offered as a
  clean API, and these files update quarterly/annually at most).
- No new companies get created from this data -- see each loader's
  docstring in `app/ingest/sponsors.py`.

## Deploying to the live VM -- 4 pending migrations, apply together

As of this writing, `git log alembic/versions` has **four migrations
not yet applied to production**, in this order:

1. `16e1cf02645f` -- H-1B sponsorship fields + `oews_wages` table (Phase 1)
2. `48244f109545` -- Workable/SmartRecruiters board slug columns (Phase 2a)
3. `faf2c7b665bf` -- sponsorship signals, `Company.tier`, score_breakdown,
   outreach hygiene caps, `/queue` triage fields (Phase 3/4)
4. `c1c1c6f2ee70` -- remote-board poll interval setting (Phase 2b)

All three were verified via a real SQLite upgrade/downgrade round-trip
rebuilt from the pre-Phase-1 schema, plus an offline PostgreSQL DDL
review (no live Postgres was available to test against directly in
the session that wrote them -- see project memory for the full
verification record). On the next VM deploy, run the normal flow:

```bash
git pull
source venv/bin/activate  # or your usual activation
alembic upgrade head
sudo systemctl restart career-pilot
```

Then run `python -m app.ingest.cli backfill-signals` once to populate
`Company.tier`/score_breakdown for whatever's already in the live
database (harmless no-op on an empty/fresh instance).
