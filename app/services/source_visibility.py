"""
Per-source visibility tagging for the demo/showcase instance. Every
intake source is tagged "prod_only" or "all_instances" -- the demo
instance (APP_MODE=showcase, see app_mode.py) only ever polls/renders
"all_instances" sources, never "prod_only" ones.

Direct-ATS boards (Greenhouse/Lever/Ashby/Recruitee/Personio/Workable/
SmartRecruiters) plus LinkedIn/Adzuna/JobsPipe are all fine on a
public demo -- generic aggregators/boards with no restrictive terms
around republishing.

The 4 Phase 2b remote-board sources (RemoteOK/Remotive/WeWorkRemotely/
Jobspresso) are deliberately kept "prod_only" even though their real,
read terms (confirmed live 2026-09-10, see each source module's own
docstring) don't flatly forbid a personal tracking tool -- Remotive's
specifically warns against "displaying our jobs in order to collect
signups... to show a listing," which a public demo instance (whose
whole purpose is to showcase the platform to attract users) sits too
close to for a default "yes" without a deliberate, separate legal
review. The personal instance is unaffected either way -- this only
gates the public demo.
"""

from ..app_mode import is_showcase_mode as _is_showcase_mode

_ALL_INSTANCES = "all_instances"
_PROD_ONLY = "prod_only"

SOURCE_VISIBILITY = {
    "linkedin": _ALL_INSTANCES,
    "adzuna": _ALL_INSTANCES,
    "greenhouse": _ALL_INSTANCES,
    "lever": _ALL_INSTANCES,
    "ashby": _ALL_INSTANCES,
    "recruitee": _ALL_INSTANCES,
    "personio": _ALL_INSTANCES,
    "workable": _ALL_INSTANCES,
    "smartrecruiters": _ALL_INSTANCES,
    "jobspipe": _ALL_INSTANCES,
    # Phase 2b remote-board aggregators -- prod_only, see module docstring.
    "remoteok": _PROD_ONLY,
    "remotive": _PROD_ONLY,
    "weworkremotely": _PROD_ONLY,
    "jobspresso": _PROD_ONLY,
}


def is_source_visible_here(source_name: str) -> bool:
    """False only when this is the showcase/demo instance AND the
    source isn't explicitly marked "all_instances" -- an unlisted
    source name (a future one someone forgets to tag) defaults to
    prod_only, the safer failure mode, not silently visible."""
    if not _is_showcase_mode():
        return True
    return SOURCE_VISIBILITY.get(source_name) == _ALL_INSTANCES
