from __future__ import annotations

import os
import pathlib
import sqlite3
import zipfile
from datetime import UTC, datetime

import pytest

from scripts.backup import (
    BackupError,
    build_backup_archive,
    prune_backups,
    validate_backup_archive,
)


def _database(path: pathlib.Path) -> pathlib.Path:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        for table in (
            "active_rule_set",
            "campaign_brief_versions",
            "campaign_context_versions",
            "campaigns",
            "package_versions",
            "rule_proposals",
            "rule_versions",
        ):
            connection.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO active_rule_set VALUES ('active')")
        connection.commit()
    finally:
        connection.close()
    return path


def _archive(tmp_path: pathlib.Path) -> pathlib.Path:
    contract = tmp_path / "contract.json"
    contract.write_text('{"schema_version":1,"redacted":true}\n', encoding="utf-8")
    return build_backup_archive(
        backup_id="backup-unit-001",
        database_path=_database(tmp_path / "factory.db"),
        contract_path=contract,
        evidence_root=tmp_path / "evidence",
        output_root=tmp_path / "backups",
        git_commit="a" * 40,
        created_at=datetime(2026, 7, 12, tzinfo=UTC),
    )


def test_backup_archive_is_checksum_bound_and_restore_readable(tmp_path: pathlib.Path) -> None:
    archive = _archive(tmp_path)

    report = validate_backup_archive(archive)

    assert report["status"] == "PASS"
    assert report["backup_id"] == "backup-unit-001"
    assert report["evidence_count"] == 0
    assert report["database"]["integrity_check"] == "ok"
    assert report["database"]["foreign_key_error_count"] == 0
    assert report["database"]["journal_mode"] == "wal"
    assert report["database"]["rule_counts"] == {
        "active_rule_set": 1,
        "rule_proposals": 0,
        "rule_versions": 0,
    }
    assert archive.stat().st_mode & 0o777 == 0o600


def test_backup_archive_rejects_member_drift(tmp_path: pathlib.Path) -> None:
    archive = _archive(tmp_path)
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(tampered, "w") as target:
        for member in source.infolist():
            content = source.read(member)
            if member.filename == "database/factory.sqlite3":
                content += b"tampered"
            target.writestr(member, content)

    with pytest.raises(BackupError, match="checksum inventory"):
        validate_backup_archive(tampered)


def test_backup_prune_is_dry_run_until_explicitly_applied(tmp_path: pathlib.Path) -> None:
    for index in range(10):
        path = tmp_path / f"backup-{index:02d}.zip"
        path.write_bytes(b"fixture")
        os.utime(path, (index, index))

    candidates = prune_backups(tmp_path, keep=7)

    assert len(candidates) == 3
    assert all(path.exists() for path in candidates)

    assert prune_backups(tmp_path, keep=7, apply=True) == candidates
    assert all(not path.exists() for path in candidates)
