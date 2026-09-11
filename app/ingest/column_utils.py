"""
Shared column-name resolution for the government data files this
package parses. Real-world USCIS/DOL exports change header spelling,
casing, and spacing across fiscal-year releases (e.g. "Employer" vs
"Petitioner Name", "SOC_CODE" vs "SOC Code") -- these files are
manually dropped in by the user (see SETUP.md), so there's no chance
to test against next quarter's release before it exists. Rather than
hard-coding one exact header per field and breaking silently (or
worse, silently mis-mapping) the moment a header changes, every field
is resolved against a list of known aliases, matched case/whitespace/
punctuation-insensitively, and a missing required field raises a
clear, actionable error naming what WAS found in the file -- so fixing
a schema drift is "add one alias to this list," not "debug a
silent parsing bug three steps downstream."
"""

import re

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _normalize_header(header: str) -> str:
    return _NORMALIZE_RE.sub("", str(header).strip().lower())


class ColumnResolutionError(Exception):
    """Raised when a required column can't be matched against any of
    its known aliases -- always includes the real columns found, so
    the fix is obvious without a debugger."""


def resolve_column(columns, aliases: list[str], required: bool = True) -> str | None:
    """Given a DataFrame's real column names and a list of acceptable
    aliases for one logical field, return the actual matching column
    name (preserving its real casing) or None if not found and not
    required."""
    normalized_lookup = {_normalize_header(c): c for c in columns}
    for alias in aliases:
        match = normalized_lookup.get(_normalize_header(alias))
        if match is not None:
            return match
    if required:
        raise ColumnResolutionError(
            f"None of the expected column names {aliases} were found. "
            f"Actual columns in this file: {list(columns)}"
        )
    return None
