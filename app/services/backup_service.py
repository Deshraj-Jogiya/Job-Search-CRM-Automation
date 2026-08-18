"""
Phase 9: encrypted local data backup/export. First real use of the
Fernet key introduced back in Phase 0 (app/services/crypto_utils.py,
CREDENTIAL_ENCRYPTION_KEY) -- exists so a copy of your data (profile,
applications, tailored documents, outreach messages) can safely leave
this machine (e.g. to a personal cloud drive) without exposing that
content in plaintext if the copy is ever lost, synced somewhere
unexpected, or seen by someone else.

Export/download only in this pass. Restore is deliberately NOT built
here -- overwriting a live database is a destructive action (could
wipe real, current data) that deserves its own careful design (which
backup to pick, merge vs. replace, a confirmation step) rather than
being a rushed side effect of "let's add backups." See CLAUDE.md.
"""

import os
import sqlite3
import tempfile
from datetime import datetime

from cryptography.fernet import Fernet

from ..database import DATABASE_URL


def is_configured() -> bool:
    return bool(os.getenv("CREDENTIAL_ENCRYPTION_KEY"))


def _sqlite_path() -> str:
    if not DATABASE_URL.startswith("sqlite"):
        raise RuntimeError(
            "Backup currently only supports the default SQLite database -- "
            f"DATABASE_URL is set to a non-SQLite value ({DATABASE_URL})."
        )
    return DATABASE_URL.split("///", 1)[1]


def _consistent_sqlite_snapshot(source_path: str) -> bytes:
    """Uses sqlite3's own online backup API rather than a raw file
    copy, so this stays safe even while the background scheduler
    thread might be mid-write -- a plain file copy could otherwise
    capture a torn, inconsistent snapshot."""
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


def create_encrypted_backup() -> tuple[bytes, str]:
    """Returns (encrypted_bytes, suggested_filename)."""
    if not is_configured():
        raise RuntimeError(
            "CREDENTIAL_ENCRYPTION_KEY is not set. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
            "and add it to .env."
        )

    db_bytes = _consistent_sqlite_snapshot(_sqlite_path())

    key = os.getenv("CREDENTIAL_ENCRYPTION_KEY").encode()
    fernet = Fernet(key)
    encrypted = fernet.encrypt(db_bytes)

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"career_pilot_backup_{timestamp}.db.enc"
    return encrypted, filename
