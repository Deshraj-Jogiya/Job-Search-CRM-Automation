"""
Phase 9: encrypted data backup/export. First real use of the Fernet key
introduced back in Phase 0 (CREDENTIAL_ENCRYPTION_KEY) -- exists so a
copy of your data (profile, applications, tailored documents, outreach
messages) can safely leave this machine (e.g. to a personal cloud
drive) without exposing that content in plaintext if the copy is ever
lost, synced somewhere unexpected, or seen by someone else.

Restore (Phase 9 continuation) is built alongside export now, with the
careful design export's original docstring deferred it for: an
automatic safety-net backup of the CURRENT database taken right before
any restore actually runs (so a bad restore is itself undoable), gated
behind ADMIN_PASSWORD (checked by the caller in app/main.py, not here),
a two-step upload-preview-then-confirm flow (see stage_uploaded_backup/
preview_staged_backup/execute_restore below) rather than one-click, and
all-or-nothing replacement -- no selective/partial restore, which would
add real FK-consistency complexity for what's meant to be a rare
emergency tool, not a routine one.

Restore only supports same-dialect backups (a SQLite backup can only
restore into a SQLite deployment, a Postgres export only into Postgres)
-- cross-format restore would need real data-mapping work for a
scenario (restoring a pre-migration SQLite backup onto today's Postgres
deployment) this project doesn't actually need to support.

Two real snapshot mechanisms, dispatched on DATABASE_URL's scheme:
- SQLite: sqlite3's own online backup API -- a real binary .db file,
  safe even mid-write from the background scheduler thread.
- Postgres (this project's own real deployment, via Supabase): a
  dialect-agnostic row-level JSON export via SQLAlchemy's own
  reflected Base.metadata, not pg_dump. Deliberately not shelling out
  to pg_dump -- it isn't installed on this dev machine and can't be
  assumed present on a fresh Oracle Cloud VM either without extra setup
  this project's own $0/minimal-setup philosophy argues against.
  Iterates Base.metadata.sorted_tables, which only contains tables this
  app's own models.py actually defines -- correctly excludes the
  portfolio site's separate tables that happen to live in the same
  Supabase project (found during the Phase 21 Alembic setup) without
  needing any manual exclusion list here.
"""

import json
import os
import sqlite3
import tempfile
import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import DateTime, select, text

from ..database import DATABASE_URL, engine, utcnow
from ..models import Base

# Local-only staging areas for the restore flow below -- never committed
# (see .gitignore). RESTORE_STAGING_DIR holds an uploaded backup between
# the preview step and the confirm step (still Fernet-encrypted at rest,
# so a token leak alone doesn't expose data) so the browser doesn't need
# to re-upload a possibly-large file for the second click.
# PRE_RESTORE_DIR holds the automatic safety-net snapshot taken of the
# CURRENT database immediately before every restore actually runs.
RESTORE_STAGING_DIR = Path("backups") / "restore_staging"
PRE_RESTORE_DIR = Path("backups") / "pre_restore"

_STAGED_UPLOAD_MAX_AGE_SECONDS = 30 * 60  # abandoned uploads don't accumulate forever

SQLITE_MAGIC = b"SQLite format 3\x00"


def is_configured() -> bool:
    return bool(os.getenv("CREDENTIAL_ENCRYPTION_KEY"))


def _sqlite_path() -> str:
    return DATABASE_URL.split("///", 1)[1]


def _consistent_sqlite_snapshot() -> bytes:
    """Uses sqlite3's own online backup API rather than a raw file
    copy, so this stays safe even while the background scheduler
    thread might be mid-write -- a plain file copy could otherwise
    capture a torn, inconsistent snapshot."""
    source_path = _sqlite_path()
    if not os.path.exists(source_path):
        raise RuntimeError(f"Database file not found at {source_path}.")

    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        source_conn = sqlite3.connect(source_path)
        dest_conn = sqlite3.connect(tmp_path)
        with dest_conn:
            source_conn.backup(dest_conn)
        source_conn.close()
        dest_conn.close()

        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        os.unlink(tmp_path)


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        import base64
        return base64.b64encode(value).decode("ascii")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _relational_snapshot() -> bytes:
    """Row-level export of every table this app's own models.py
    defines, via a single connection/transaction for a consistent
    snapshot across tables (Postgres's default READ COMMITTED
    isolation on one connection is enough here -- this is a best-effort
    backup, not a point-in-time database-level guarantee the way
    sqlite3's own backup API or pg_dump would provide)."""
    tables_data = {}
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            result = conn.execute(select(table))
            tables_data[table.name] = [dict(row._mapping) for row in result]

    payload = {
        "exported_at": utcnow().isoformat(),
        "dialect": engine.dialect.name,
        "tables": tables_data,
    }
    return json.dumps(payload, default=_json_default).encode("utf-8")


def create_encrypted_backup() -> tuple[bytes, str]:
    """Returns (encrypted_bytes, suggested_filename)."""
    if not is_configured():
        raise RuntimeError(
            "CREDENTIAL_ENCRYPTION_KEY is not set. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
            "and add it to .env."
        )

    is_sqlite = DATABASE_URL.startswith("sqlite")
    if is_sqlite:
        raw_bytes = _consistent_sqlite_snapshot()
        extension = "db"
    else:
        raw_bytes = _relational_snapshot()
        extension = "json"

    key = os.getenv("CREDENTIAL_ENCRYPTION_KEY").encode()
    fernet = Fernet(key)
    encrypted = fernet.encrypt(raw_bytes)

    timestamp = utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"career_pilot_backup_{timestamp}.{extension}.enc"
    return encrypted, filename


def _decrypt(encrypted_bytes: bytes) -> bytes:
    if not is_configured():
        raise RuntimeError("CREDENTIAL_ENCRYPTION_KEY is not set -- can't decrypt a backup without it.")
    key = os.getenv("CREDENTIAL_ENCRYPTION_KEY").encode()
    try:
        return Fernet(key).decrypt(encrypted_bytes)
    except InvalidToken:
        raise RuntimeError(
            "Could not decrypt this backup file -- either it was encrypted with a different "
            "CREDENTIAL_ENCRYPTION_KEY, or the file is corrupted/not a real Career Pilot backup."
        )


def _decrypted_format(raw: bytes) -> str:
    if raw[:16] == SQLITE_MAGIC:
        return "sqlite"
    try:
        json.loads(raw)
        return "postgres_json"
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError("Unrecognized backup contents -- the file may be corrupted.")


def _check_dialect_match(fmt: str):
    live_is_sqlite = DATABASE_URL.startswith("sqlite")
    if fmt == "sqlite" and not live_is_sqlite:
        raise RuntimeError(
            "This backup is a SQLite snapshot, but the live database is Postgres -- "
            "cross-format restore isn't supported."
        )
    if fmt == "postgres_json" and live_is_sqlite:
        raise RuntimeError(
            "This backup is a Postgres export, but the live database is SQLite -- "
            "cross-format restore isn't supported."
        )


def _purge_stale_staged_uploads():
    """Best-effort cleanup of abandoned uploads (previewed but never
    confirmed or cancelled) -- runs on every new stage call rather than
    needing its own scheduled job for what should be a rare occurrence."""
    if not RESTORE_STAGING_DIR.exists():
        return
    cutoff = time.time() - _STAGED_UPLOAD_MAX_AGE_SECONDS
    for path in RESTORE_STAGING_DIR.glob("*.enc"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def stage_uploaded_backup(encrypted_bytes: bytes) -> str:
    """Persists an uploaded encrypted backup to a local staging file
    keyed by a random token, so the confirm step doesn't require
    re-uploading a possibly-large file. Stays encrypted at rest --
    decrypted only transiently, here and again at actual restore time,
    never written to disk in plaintext."""
    RESTORE_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    _purge_stale_staged_uploads()
    token = uuid.uuid4().hex
    (RESTORE_STAGING_DIR / f"{token}.enc").write_bytes(encrypted_bytes)
    return token


def _staged_path(token: str) -> Path:
    # Tokens are always our own uuid4().hex output -- reject anything
    # that isn't, so a malformed/crafted value can't be used to build a
    # path outside RESTORE_STAGING_DIR.
    if not token or len(token) != 32 or not all(c in "0123456789abcdef" for c in token):
        raise RuntimeError("Invalid or expired restore token -- please upload the backup file again.")
    return RESTORE_STAGING_DIR / f"{token}.enc"


def discard_staged_backup(token: str):
    path = _staged_path(token)
    if path.exists():
        path.unlink()


def preview_staged_backup(token: str) -> dict:
    """Read-only: decrypts the staged upload just long enough to report
    what it contains (row counts per table, when it was taken) so the
    user can make an informed decision before anything is touched.
    Never modifies the live database."""
    path = _staged_path(token)
    if not path.exists():
        raise RuntimeError("This restore upload has expired or was already used -- please upload the backup file again.")

    raw = _decrypt(path.read_bytes())
    fmt = _decrypted_format(raw)
    _check_dialect_match(fmt)

    if fmt == "sqlite":
        fd, tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            with open(tmp_path, "wb") as f:
                f.write(raw)
            conn = sqlite3.connect(tmp_path)
            row_counts = {}
            for table in Base.metadata.sorted_tables:
                try:
                    row_counts[table.name] = conn.execute(f'SELECT COUNT(*) FROM "{table.name}"').fetchone()[0]
                except sqlite3.OperationalError:
                    row_counts[table.name] = 0  # table doesn't exist in this backup -- fine, restore leaves it empty
            conn.close()
        finally:
            os.unlink(tmp_path)
        return {
            "format": "sqlite",
            "exported_at": None,  # not embedded in the raw sqlite file itself
            "row_counts": row_counts,
            "total_rows": sum(row_counts.values()),
        }

    payload = json.loads(raw)
    tables = payload.get("tables", {})
    row_counts = {name: len(rows) for name, rows in tables.items()}
    return {
        "format": "postgres_json",
        "exported_at": payload.get("exported_at"),
        "row_counts": row_counts,
        "total_rows": sum(row_counts.values()),
    }


def _restore_sqlite(raw: bytes) -> dict:
    """Mirrors _consistent_sqlite_snapshot's direction, reusing the same
    sqlite3 online backup API -- copies the uploaded snapshot INTO the
    live database file instead of out of it."""
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with open(tmp_path, "wb") as f:
            f.write(raw)

        check_conn = sqlite3.connect(tmp_path)
        integrity = check_conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            check_conn.close()
            raise RuntimeError(f"Backup file failed SQLite integrity check ({integrity}) -- refusing to restore from it.")

        row_counts = {}
        for table in Base.metadata.sorted_tables:
            try:
                row_counts[table.name] = check_conn.execute(f'SELECT COUNT(*) FROM "{table.name}"').fetchone()[0]
            except sqlite3.OperationalError:
                row_counts[table.name] = 0
        check_conn.close()

        # Dispose the live engine's pooled connections first so they
        # aren't holding the destination file open/mid-transaction while
        # the backup API overwrites it.
        engine.dispose()
        source_conn = sqlite3.connect(tmp_path)
        dest_conn = sqlite3.connect(_sqlite_path())
        with dest_conn:
            source_conn.backup(dest_conn)
        source_conn.close()
        dest_conn.close()
    finally:
        os.unlink(tmp_path)

    return {"format": "sqlite", "row_counts": row_counts, "total_rows": sum(row_counts.values())}


def _coerce_row_for_insert(table, row: dict) -> dict:
    """Reverses _json_default's encoding for the one column type this
    schema actually needs it for -- DateTime columns were serialized to
    ISO strings for JSON export and need converting back to real
    datetime objects for the insert. Every other column type in this
    schema (String/Text/Integer/Boolean/Float) round-trips through JSON
    natively with no coercion needed."""
    coerced = {}
    for col in table.columns:
        if col.name not in row:
            continue
        value = row[col.name]
        if value is not None and isinstance(col.type, DateTime):
            value = datetime.fromisoformat(value)
        coerced[col.name] = value
    return coerced


def _restore_postgres_json(raw: bytes) -> dict:
    """All-or-nothing: deletes every row from every table this app's own
    models.py defines (children-first, so FK constraints never trip),
    then re-inserts everything from the backup (parents-first), all in
    one transaction -- if anything fails partway, the whole thing rolls
    back and the live database is untouched. Postgres sequences are
    reset afterward so the next real insert doesn't collide with a
    restored explicit id."""
    payload = json.loads(raw)
    tables_data = payload.get("tables", {})

    row_counts = {}
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())

        for table in Base.metadata.sorted_tables:
            rows = tables_data.get(table.name, [])
            row_counts[table.name] = len(rows)
            if not rows:
                continue
            conn.execute(table.insert(), [_coerce_row_for_insert(table, row) for row in rows])

        if engine.dialect.name == "postgresql":
            for table in Base.metadata.sorted_tables:
                if "id" not in table.columns:
                    continue
                conn.execute(
                    text(
                        "SELECT setval(pg_get_serial_sequence(:tbl, 'id'), "
                        f'COALESCE((SELECT MAX(id) FROM "{table.name}"), 0) + 1, false)'
                    ),
                    {"tbl": table.name},
                )

    return {"format": "postgres_json", "row_counts": row_counts, "total_rows": sum(row_counts.values())}


def execute_restore(token: str) -> dict:
    """The actual destructive step -- takes an automatic safety-net
    backup of the CURRENT database first (so a bad restore is itself
    undoable), then replaces everything with the staged upload's
    contents. Raises RuntimeError (leaving the live database completely
    untouched) if the staged upload is missing/expired, undecryptable,
    or a different dialect than the live database."""
    path = _staged_path(token)
    if not path.exists():
        raise RuntimeError("This restore upload has expired or was already used -- please upload the backup file again.")

    raw = _decrypt(path.read_bytes())
    fmt = _decrypted_format(raw)
    _check_dialect_match(fmt)

    safety_bytes, safety_filename = create_encrypted_backup()
    PRE_RESTORE_DIR.mkdir(parents=True, exist_ok=True)
    safety_path = PRE_RESTORE_DIR / safety_filename
    safety_path.write_bytes(safety_bytes)

    if fmt == "sqlite":
        result = _restore_sqlite(raw)
    else:
        result = _restore_postgres_json(raw)

    discard_staged_backup(token)
    result["safety_backup_path"] = str(safety_path)
    return result
