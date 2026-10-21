# Changelog

Grouped by month. Design decisions behind these changes are recorded in [`docs/decisions/`](docs/decisions/README.md).

## v1.0.0 (2026-10-20)

First tagged release: the platform as it runs in production and in the public demo.

### October 2026
- Interview prep page built from the master profile, kept in step with profile versions, with a layout meant to be read before a call.
- Blank interview-prep sections fixed; filler words removed from generated project bullets.
- Migration that removed per-user quotas made safe to re-run on older SQLite versions and after a half-applied first attempt.
- `pdfplumber` added to the requirements so CI runs the PDF tests.

### September 2026
- **Targeting:** direct polling of 7 applicant-tracking systems (Greenhouse, Lever, Ashby, Recruitee, Personio, Workable,
  SmartRecruiters) and 4 remote job boards; board discovery checks the API's own company name before accepting a match.
- **Scoring:** rule-based components (role and title match, recency, location, sponsorship history, wage level, company tier) plus one
  LLM component, run only when it can still change the outcome; daily LLM spend cap; prompt caching for the shared profile block.
- **Queue and metrics:** `/queue` with skip and not-a-fit actions; `/metrics` weekly funnel with conversion rates and segmentation.
- **Tailoring safety:** bounded self-correction loop for fabrication findings, hard versus soft findings, cliche and weak-opener
  checks, and a page-count check on the real rendered PDF.
- **Research agent:** ReAct question answering over the app's own data, with a stop sequence and a gate that rejects answers given
  before any tool result.
- **Kanban board:** drag-and-drop pipeline that uses the same guarded status transitions as the buttons.
- **Browser extension:** form filling in the user's own browser on all 7 applicant-tracking systems, including file attachment,
  radio and checkbox groups, react-select comboboxes, shadow DOM, honeypot exclusion, add-job from any page and submission detection.
- **Saved answers:** recorded answers to application questions reused with conservative matching.
- **Data hygiene:** activity-log retention, location filtering that only excludes on a known foreign location, row-level security on
  every table with a regression test.

### August 2026
- Rebuilt data model, services and routes on FastAPI, SQLAlchemy and Alembic.
- Sign-up, login and password reset; onboarding flow; CSRF protection; security review and fixes.
- Supabase Postgres in production; Oracle Cloud VM behind Caddy HTTPS; health-check watchdog; encrypted daily backups.
- Public demo as a separate instance with its own data and no real keys.
- Interview preparation and mock interviews grounded in the tailored resume, with version history and per-round PDF export.
- Outreach contact discovery with sourced reasons for each contact.
- Target-company sources: applicant-tracking scrapers, a job-board aggregator and the YC company directory.

### June 2026
- First prototype: posting crawler, resume tailoring, Playwright form filling and a Kanban-style dashboard.
