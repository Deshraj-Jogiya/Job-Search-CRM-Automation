"""
Offline-first ingest pipelines for real government H-1B/wage data
(USCIS H-1B Employer Data Hub, DOL OFLC LCA Disclosure, DOL OFLC OEWS
wage data). Every loader here reads a file the user has manually
dropped into data/raw/ -- see SETUP.md -- there is no live scraping or
API polling in this package (that's board_discovery.py and the
Phase 2a ats/board pollers, a separate concern). A missing or
unparseable file must never crash the app or block any other feature;
each loader is meant to be run standalone via its own CLI entry point,
independent of the FastAPI process.
"""
