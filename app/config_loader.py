"""
Loads and validates config/*.yaml -- every threshold/weight/limit the
resume-rules engine (tailoring_service.py) and the adaptive layer
(adaptation_service.py) use lives here, never hardcoded in Python.

Each file has its own explicit validate_fn (in resume_rules.py /
adaptation_service.py, not here) rather than a generic recursive
schema engine -- there are exactly two config files with a small, known
shape each; a generic engine would be more code and harder to read for
no real benefit. What IS generic and shared here is the actual
mechanics: reading the YAML, running validate_fn against it, and
re-checking the file's mtime on every access so an edit takes effect
on the next read with no explicit reload() call and no polling thread
(hot-reloadable, per the governing rule).

require() is the single place every validate_fn calls into, so every
config error anywhere in this app has the identical shape: the exact
key, the actual value, and the allowed type/range -- never a bare
"invalid config".
"""

from pathlib import Path
from threading import Lock
from typing import Any, Callable

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class ConfigValidationError(Exception):
    """Names the exact key, the bad value, and the allowed type/range
    -- raised at load time (app startup, or the next hot-reloaded
    access after an edit), never silently swallowed."""


def require(
    data: dict,
    key: str,
    type_: type,
    min: float | None = None,
    max: float | None = None,
    choices: list | None = None,
    required: bool = True,
) -> Any:
    """Validates one key out of an already-loaded YAML dict and
    returns its value. Every validate_fn in this app is built entirely
    out of calls to this -- see resume_rules.py/adaptation_service.py."""
    if key not in data:
        if required:
            raise ConfigValidationError(f"Required config key '{key}' is missing.")
        return None

    value = data[key]
    # bool is a subclass of int in Python -- without this, a stray
    # `true`/`false` in a YAML file would silently pass an int/float
    # type check instead of failing loudly like it should.
    type_mismatch = (isinstance(value, bool) and type_ is not bool) or not isinstance(value, type_)
    if type_mismatch:
        raise ConfigValidationError(
            f"Config key '{key}' = {value!r} has type {type(value).__name__}, expected {type_.__name__}."
        )
    if min is not None and value < min:
        raise ConfigValidationError(f"Config key '{key}' = {value!r} is below the allowed minimum {min}.")
    if max is not None and value > max:
        raise ConfigValidationError(f"Config key '{key}' = {value!r} is above the allowed maximum {max}.")
    if choices is not None and value not in choices:
        raise ConfigValidationError(f"Config key '{key}' = {value!r} is not one of the allowed values {choices}.")
    return value


class HotReloadableYaml:
    """Wraps one config/*.yaml file. .get() returns the validated,
    parsed dict -- re-reading and re-validating from disk only when
    the file's mtime has changed since the last read, so an edit takes
    effect on the very next access with no restart and no background
    polling thread."""

    def __init__(self, filename: str, validate_fn: Callable[[dict], None]):
        self.path = CONFIG_DIR / filename
        self._validate_fn = validate_fn
        self._lock = Lock()
        self._mtime: float | None = None
        self._data: dict | None = None

    def get(self) -> dict:
        with self._lock:
            mtime = self.path.stat().st_mtime
            if self._data is None or mtime != self._mtime:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                self._validate_fn(data)
                self._data = data
                self._mtime = mtime
            return self._data
