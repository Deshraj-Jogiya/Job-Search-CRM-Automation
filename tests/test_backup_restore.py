"""Phase 9 restore: stage -> preview -> execute, plus the safety
properties that make it survivable if something goes wrong (safety-net
backup taken first, bad token/wrong key never touches the live DB)."""

import os
import time

import pytest
from cryptography.fernet import Fernet

from app import models
from app.services import backup_service
from tests.conftest import make_company, make_posting


TEST_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", TEST_KEY)


@pytest.fixture()
def staging_dirs(tmp_path, monkeypatch):
    """Keeps restore's local staging/safety-net files inside pytest's own
    tmp dir instead of writing into the real project's backups/ folder."""
    restore_dir = tmp_path / "restore_staging"
    pre_restore_dir = tmp_path / "pre_restore"
    monkeypatch.setattr(backup_service, "RESTORE_STAGING_DIR", restore_dir)
    monkeypatch.setattr(backup_service, "PRE_RESTORE_DIR", pre_restore_dir)
    return restore_dir, pre_restore_dir


def test_stage_preview_execute_round_trip(db, staging_dirs):
    company = make_company(db, name="Original Corp")
    make_posting(db, company)

    encrypted_bytes, _ = backup_service.create_encrypted_backup()

    # Mutate the live DB after taking the backup, so restore has
    # something real to undo.
    db.query(models.JobPosting).delete()
    db.query(models.Company).delete()
    db.commit()
    assert db.query(models.Company).count() == 0

    token = backup_service.stage_uploaded_backup(encrypted_bytes)
    preview = backup_service.preview_staged_backup(token)
    assert preview["format"] == "sqlite"
    assert preview["row_counts"]["companies"] == 1
    assert preview["row_counts"]["job_postings"] == 1

    # Preview must be read-only -- the mutation above should still stand.
    assert db.query(models.Company).count() == 0

    result = backup_service.execute_restore(token)
    assert result["row_counts"]["companies"] == 1
    assert os.path.exists(result["safety_backup_path"])

    db.expire_all()
    restored_companies = db.query(models.Company).all()
    assert len(restored_companies) == 1
    assert restored_companies[0].name == "Original Corp"


def test_execute_restore_takes_safety_net_backup_of_current_state_first(db, staging_dirs):
    make_company(db, name="Backup Snapshot Target")
    encrypted_bytes, _ = backup_service.create_encrypted_backup()
    token = backup_service.stage_uploaded_backup(encrypted_bytes)

    # Current state changes again after the backup being restored was taken --
    # this is exactly what the safety-net snapshot needs to capture.
    make_company(db, name="Present At Restore Time")

    result = backup_service.execute_restore(token)
    safety_bytes = open(result["safety_backup_path"], "rb").read()
    decrypted = backup_service._decrypt(safety_bytes)  # a raw sqlite file, since the test DB is sqlite

    import sqlite3
    import tempfile

    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with open(tmp_path, "wb") as f:
            f.write(decrypted)
        conn = sqlite3.connect(tmp_path)
        names = {row[0] for row in conn.execute("SELECT name FROM companies")}
        conn.close()
    finally:
        os.unlink(tmp_path)

    assert "Present At Restore Time" in names


def test_execute_restore_discards_staged_upload_after_success(db, staging_dirs):
    make_company(db)
    encrypted_bytes, _ = backup_service.create_encrypted_backup()
    token = backup_service.stage_uploaded_backup(encrypted_bytes)
    backup_service.execute_restore(token)

    with pytest.raises(RuntimeError, match="expired or was already used"):
        backup_service.preview_staged_backup(token)


def test_preview_wrong_encryption_key_never_touches_db(db, staging_dirs, monkeypatch):
    make_company(db, name="Should Not Be Touched")
    encrypted_bytes, _ = backup_service.create_encrypted_backup()
    token = backup_service.stage_uploaded_backup(encrypted_bytes)

    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(RuntimeError, match="Could not decrypt"):
        backup_service.preview_staged_backup(token)

    db.expire_all()
    assert db.query(models.Company).filter_by(name="Should Not Be Touched").count() == 1


def test_invalid_token_rejected(staging_dirs):
    with pytest.raises(RuntimeError, match="Invalid or expired"):
        backup_service.preview_staged_backup("../../etc/passwd")

    with pytest.raises(RuntimeError, match="Invalid or expired"):
        backup_service.preview_staged_backup("not-a-real-token")


def test_unknown_token_rejected(staging_dirs):
    with pytest.raises(RuntimeError, match="expired or was already used"):
        backup_service.preview_staged_backup("a" * 32)


def test_dialect_mismatch_refuses_to_restore(db, staging_dirs, monkeypatch):
    make_company(db)
    encrypted_bytes, _ = backup_service.create_encrypted_backup()  # a real sqlite-format backup
    token = backup_service.stage_uploaded_backup(encrypted_bytes)

    monkeypatch.setattr(backup_service, "DATABASE_URL", "postgresql://fake/db")
    with pytest.raises(RuntimeError, match="cross-format restore isn't supported"):
        backup_service.preview_staged_backup(token)


def test_restore_postgres_json_coerces_datetime_columns_and_replaces_all_rows(db):
    """Exercises the Postgres-shaped restore path directly (the real
    deployment's actual dialect) -- the sqlite-backed test DB can still
    run the same table.delete()/table.insert() Core calls _restore_
    postgres_json uses, just without the postgres-only sequence-reset
    step, which is gated on engine.dialect.name == 'postgresql'."""
    import json

    existing_company = make_company(db, name="Will Be Replaced")
    make_posting(db, existing_company)  # a row in a table the restore payload below never mentions
    db.commit()

    payload = {
        "exported_at": "2026-01-01T00:00:00",
        "dialect": "postgresql",
        "tables": {
            "companies": [
                {
                    "id": 1,
                    "name": "Restored From JSON",
                    "normalized_name": "restored from json",
                    "status": "Neutral",
                    "status_reason": None,
                    "ghosted_count": 0,
                    "created_at": "2026-05-01T12:30:00",
                }
            ]
        },
    }
    raw = json.dumps(payload).encode("utf-8")

    result = backup_service._restore_postgres_json(raw)
    assert result["row_counts"]["companies"] == 1

    db.expire_all()
    companies = db.query(models.Company).all()
    assert len(companies) == 1
    assert companies[0].name == "Restored From JSON"
    assert companies[0].created_at.isoformat() == "2026-05-01T12:30:00"

    # Every other table the payload didn't mention should end up empty,
    # not left over from before the restore -- this is the all-or-nothing
    # replace, not a merge.
    assert db.query(models.JobPosting).count() == 0


def test_stale_staged_uploads_are_purged_on_next_stage_call(staging_dirs):
    restore_dir, _ = staging_dirs
    token = backup_service.stage_uploaded_backup(b"stale-fake-upload")
    stale_path = restore_dir / f"{token}.enc"
    assert stale_path.exists()

    old_time = time.time() - backup_service._STAGED_UPLOAD_MAX_AGE_SECONDS - 60
    os.utime(stale_path, (old_time, old_time))

    backup_service.stage_uploaded_backup(b"a-fresh-upload")
    assert not stale_path.exists()
