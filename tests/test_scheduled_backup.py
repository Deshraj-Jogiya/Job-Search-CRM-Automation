"""Daily automated backup (app/services/backup_service.run_scheduled_backup),
added because export was previously manual-only -- a gap left unattended
for weeks meant zero recent recovery point."""

import pytest
from cryptography.fernet import Fernet

from app.models import get_or_create_settings
from app.services import backup_service


TEST_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", TEST_KEY)


@pytest.fixture()
def backup_dir(tmp_path, monkeypatch):
    d = tmp_path / "scheduled"
    monkeypatch.setattr(backup_service, "SCHEDULED_BACKUP_DIR", d)
    return d


def test_writes_a_backup_file_when_enabled(db, backup_dir):
    settings = get_or_create_settings(db)
    settings.automated_backups_enabled = True
    db.commit()

    backup_service.run_scheduled_backup(db)

    files = list(backup_dir.glob("career_pilot_backup_*.enc"))
    assert len(files) == 1


def test_skips_silently_when_disabled(db, backup_dir):
    settings = get_or_create_settings(db)
    settings.automated_backups_enabled = False
    db.commit()

    backup_service.run_scheduled_backup(db)

    assert not backup_dir.exists()


def test_skips_when_not_configured(db, backup_dir, monkeypatch):
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    settings = get_or_create_settings(db)
    settings.automated_backups_enabled = True
    db.commit()

    backup_service.run_scheduled_backup(db)

    assert not backup_dir.exists()


def test_prunes_down_to_retention_count(db, backup_dir):
    settings = get_or_create_settings(db)
    settings.automated_backups_enabled = True
    settings.backup_retention_count = 2
    db.commit()

    backup_dir.mkdir(parents=True)
    for name in ["career_pilot_backup_20260101_000000.db.enc", "career_pilot_backup_20260102_000000.db.enc"]:
        (backup_dir / name).write_bytes(b"stale")

    backup_service.run_scheduled_backup(db)

    files = sorted(f.name for f in backup_dir.glob("career_pilot_backup_*.enc"))
    assert len(files) == 2
    assert "career_pilot_backup_20260101_000000.db.enc" not in files
