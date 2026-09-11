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
from difflib import SequenceMatcher
from pathlib import Path

import yaml

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")

# b1.3's learned-mapping state -- NOT app config (config/*.yaml is
# static, human-authored, validated on load). This file is written by
# the app itself as fuzzy guesses happen, per-deployment, and only
# meaningful against whatever real government data files THIS instance
# has actually loaded -- same "runtime state, not portable config"
# class as backups/ and data/raw/, so it lives under data/ and is
# gitignored, not config/.
_MAPPING_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "column_mappings.yaml"


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


def _load_column_mappings() -> dict:
    if not _MAPPING_FILE.exists():
        return {}
    with open(_MAPPING_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_column_mappings(mappings: dict) -> None:
    _MAPPING_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_MAPPING_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(mappings, f, sort_keys=True, default_flow_style=False)


def _best_fuzzy_match(columns, candidates: list[str]) -> tuple[str, float] | None:
    """Best (real_column, ratio) pair across every (real column,
    candidate) combination -- candidates is the loader's own alias list
    plus any previously user-confirmed real column strings for this
    exact field (b1.3's 'weight future fuzzy matching toward confirmed
    pairs' -- a confirmed real string naturally scores at least as well
    against a similarly-worded new header as a generic alias would,
    without needing an artificial score bonus)."""
    best = None
    for column in columns:
        norm_col = _normalize_header(column)
        for candidate in candidates:
            ratio = SequenceMatcher(None, norm_col, _normalize_header(candidate)).ratio()
            if best is None or ratio > best[1]:
                best = (column, ratio)
    return best


def resolve_column_learned(
    db, columns, aliases: list[str], source: str, field: str,
    required: bool = True, threshold: float | None = None,
) -> str | None:
    """b1.3: resolve_column's exact-alias match first (unchanged, the
    common case whenever a header hasn't drifted) -- only on a miss
    does this fall back to a fuzzy match, recorded in
    data/column_mappings.yaml for the user to review/correct. Detects a
    manual correction there (the saved "column" no longer equals the
    "guessed_column" recorded when the guess was made) and logs it via
    adaptation_service.record_column_mapping_correction so future
    guesses for this (source, field) weight toward the confirmed real
    string. source/field namespace the mapping file per logical column
    (e.g. source="uscis_h1b", field="employer")."""
    exact = resolve_column(columns, aliases, required=False)
    if exact is not None:
        return exact

    from ..services import adaptation_service  # local import -- avoids a circular import at module load

    if threshold is None:
        threshold = adaptation_service.current_column_mapping_fuzzy_threshold(db)

    mappings = _load_column_mappings()
    source_entry = mappings.setdefault(source, {})
    field_entry = source_entry.get(field)

    if field_entry and field_entry.get("column") != field_entry.get("guessed_column"):
        wrong_guess = field_entry.get("guessed_column")
        correct_column = field_entry["column"]
        adaptation_service.record_column_mapping_correction(db, source, field, wrong_guess, correct_column)
        field_entry["guessed_column"] = correct_column  # reconcile -- don't re-log the same correction next run
        _save_column_mappings(mappings)

    if field_entry and field_entry.get("column") in columns:
        return field_entry["column"]

    confirmed = adaptation_service.confirmed_column_names(db, source, field)
    match = _best_fuzzy_match(columns, aliases + confirmed)
    if match and match[1] >= threshold:
        guessed_column, confidence = match
        source_entry[field] = {
            "column": guessed_column, "guessed_column": guessed_column, "confidence": round(confidence, 3),
        }
        _save_column_mappings(mappings)
        return guessed_column

    if required:
        raise ColumnResolutionError(
            f"None of the expected column names {aliases} were found, and no fuzzy match reached the "
            f"{threshold} confidence threshold for '{source}.{field}'. "
            f"Actual columns in this file: {list(columns)}"
        )
    return None
