from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from typing import Any

from scripts.evidence import validate_evidence_directory
from scripts.security_scan import scan_generated_artifacts

ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKUP_ROOT = ROOT / "runtime" / "backups"
LATEST_REPORT = BACKUP_ROOT / "latest.json"
CONTRACT_PATH = ROOT / "runtime" / "contracts" / "communication_factory.lock.json"
EVIDENCE_ROOT = ROOT / "artifacts" / "evidence"
BACKUP_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
BACKUP_RETENTION_COUNT = 7
MAX_BACKUP_BYTES = 100_000_000
REQUIRED_DATABASE_TABLES = {
    "active_rule_set",
    "campaign_brief_versions",
    "campaign_context_versions",
    "campaigns",
    "package_versions",
    "rule_proposals",
    "rule_versions",
}


class BackupError(RuntimeError):
    pass


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def inspect_database(path: pathlib.Path) -> dict[str, Any]:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise BackupError("backup database cannot be opened read-only") from exc
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_errors = list(connection.execute("PRAGMA foreign_key_check"))
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        }
        missing = sorted(REQUIRED_DATABASE_TABLES - tables)
        if integrity != "ok" or foreign_key_errors or missing:
            raise BackupError("backup database integrity/schema validation failed")
        rule_counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("active_rule_set", "rule_proposals", "rule_versions")
        }
    except sqlite3.Error as exc:
        raise BackupError("backup database inspection failed") from exc
    finally:
        connection.close()
    return {
        "integrity_check": integrity,
        "foreign_key_error_count": len(foreign_key_errors),
        "journal_mode": journal_mode,
        "table_count": len(tables),
        "rule_counts": rule_counts,
    }


def _copy_evidence(source: pathlib.Path, destination: pathlib.Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    candidates = (
        sorted(path for path in source.iterdir() if path.is_dir()) if source.is_dir() else []
    )
    for evidence in candidates:
        manifest = validate_evidence_directory(evidence)
        target = destination / evidence.name
        shutil.copytree(evidence, target, symlinks=False)
        records.append(
            {
                "directory": evidence.name,
                "evaluation_id": manifest["evaluation_id"],
                "manifest_sha256": _sha256_file(evidence / "manifest.json"),
                "immutable_sha256": _sha256_file(evidence / "IMMUTABLE.json"),
            }
        )
    return records


def _write_checksums(root: pathlib.Path) -> None:
    paths = sorted(
        path for path in root.rglob("*") if path.is_file() and path.name != "checksums.sha256"
    )
    (root / "checksums.sha256").write_text(
        "".join(f"{_sha256_file(path)}  {path.relative_to(root).as_posix()}\n" for path in paths),
        encoding="utf-8",
    )


def _write_zip(source: pathlib.Path, destination: pathlib.Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            relative = path.relative_to(source).as_posix()
            info = zipfile.ZipInfo(relative, (2026, 7, 12, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            archive.writestr(info, path.read_bytes())


def build_backup_archive(
    *,
    backup_id: str,
    database_path: pathlib.Path,
    contract_path: pathlib.Path,
    evidence_root: pathlib.Path,
    output_root: pathlib.Path,
    git_commit: str,
    created_at: datetime | None = None,
) -> pathlib.Path:
    if not BACKUP_ID_PATTERN.fullmatch(backup_id):
        raise BackupError("BACKUP_ID is invalid")
    if len(git_commit) != 40 or any(
        character not in "0123456789abcdef" for character in git_commit
    ):
        raise BackupError("backup Git commit is invalid")
    if not database_path.is_file() or not contract_path.is_file():
        raise BackupError("database snapshot or runtime contract is missing")
    destination = output_root / f"backup-{backup_id}.zip"
    if destination.exists():
        raise BackupError("backup destination already exists")
    database = inspect_database(database_path)
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = pathlib.Path(tempfile.mkdtemp(prefix=".backup-", dir=output_root))
    archive_path = temporary.with_suffix(".zip")
    timestamp = (created_at or datetime.now(UTC)).astimezone(UTC)
    try:
        snapshot_target = temporary / "database" / "factory.sqlite3"
        snapshot_target.parent.mkdir(parents=True)
        shutil.copy2(database_path, snapshot_target)
        contract_target = temporary / "contract" / "communication_factory.lock.json"
        contract_target.parent.mkdir(parents=True)
        shutil.copy2(contract_path, contract_target)
        evidence = _copy_evidence(evidence_root, temporary / "evidence")
        manifest = {
            "schema_version": 1,
            "status": "PASS",
            "backup_id": backup_id,
            "created_at": timestamp.isoformat(),
            "git_commit": git_commit,
            "database_sha256": _sha256_file(snapshot_target),
            "database": database,
            "contract_sha256": _sha256_file(contract_target),
            "evidence": evidence,
            "evidence_count": len(evidence),
            "synthetic": True,
            "no_send": True,
            "contains_credentials": False,
            "restore_policy": "validate_then_restore_into_new_project_volume",
        }
        _write_json(temporary / "manifest.json", manifest)
        _write_checksums(temporary)
        _write_zip(temporary, archive_path)
        if archive_path.stat().st_size > MAX_BACKUP_BYTES:
            raise BackupError("backup archive exceeds the project size bound")
        findings = scan_generated_artifacts([archive_path.parent])
        if findings:
            raise BackupError("backup archive failed the artifact security scan")
        os.chmod(archive_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(archive_path, destination)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    try:
        validate_backup_archive(destination)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def _safe_member_names(archive: zipfile.ZipFile) -> list[str]:
    names: list[str] = []
    total_size = 0
    for member in archive.infolist():
        candidate = pathlib.PurePosixPath(member.filename)
        mode = member.external_attr >> 16
        total_size += member.file_size
        if (
            member.is_dir()
            or candidate.is_absolute()
            or ".." in candidate.parts
            or "\\" in member.filename
            or stat.S_ISLNK(mode)
            or member.file_size > MAX_BACKUP_BYTES
            or total_size > MAX_BACKUP_BYTES
        ):
            raise BackupError("backup archive contains an unsafe member")
        names.append(member.filename)
    if len(names) != len(set(names)):
        raise BackupError("backup archive contains duplicate members")
    return names


def validate_backup_archive(path: pathlib.Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > MAX_BACKUP_BYTES:
        raise BackupError("backup archive is missing or exceeds its size bound")
    try:
        with zipfile.ZipFile(path) as archive:
            names = _safe_member_names(archive)
            required = {
                "checksums.sha256",
                "contract/communication_factory.lock.json",
                "database/factory.sqlite3",
                "manifest.json",
            }
            if not required.issubset(names):
                raise BackupError("backup archive inventory is incomplete")
            checksum_lines = archive.read("checksums.sha256").decode("utf-8").splitlines()
            expected: dict[str, str] = {}
            for line in checksum_lines:
                digest, separator, member_name = line.partition("  ")
                if (
                    not separator
                    or member_name in expected
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)
                ):
                    raise BackupError("backup checksum inventory is malformed")
                expected[member_name] = digest
            actual = {
                name: _sha256_bytes(archive.read(name))
                for name in names
                if name != "checksums.sha256"
            }
            if expected != actual:
                raise BackupError("backup checksum inventory does not match")
            manifest = json.loads(archive.read("manifest.json"))
            evidence = manifest.get("evidence") if isinstance(manifest, dict) else None
            if (
                not isinstance(manifest, dict)
                or manifest.get("status") != "PASS"
                or manifest.get("synthetic") is not True
                or manifest.get("no_send") is not True
                or manifest.get("contains_credentials") is not False
                or manifest.get("database_sha256") != actual["database/factory.sqlite3"]
                or manifest.get("contract_sha256")
                != actual["contract/communication_factory.lock.json"]
                or not isinstance(evidence, list)
                or manifest.get("evidence_count") != len(evidence)
                or any(
                    not isinstance(item, dict)
                    or not str(item.get("evaluation_id") or "")
                    or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("manifest_sha256") or ""))
                    or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("immutable_sha256") or ""))
                    for item in evidence
                )
            ):
                raise BackupError("backup manifest is invalid")
            with tempfile.TemporaryDirectory(prefix="cf-backup-validate-") as temporary:
                database_path = pathlib.Path(temporary) / "factory.sqlite3"
                with (
                    archive.open("database/factory.sqlite3") as source,
                    database_path.open("wb") as destination,
                ):
                    shutil.copyfileobj(source, destination)
                database = inspect_database(database_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise BackupError("backup archive cannot be validated") from exc
    return {
        "status": "PASS",
        "backup_id": manifest["backup_id"],
        "git_commit": manifest["git_commit"],
        "evidence_count": manifest["evidence_count"],
        "evidence_evaluation_ids": [item["evaluation_id"] for item in evidence],
        "database": database,
        "archive_sha256": _sha256_file(path),
        "archive_bytes": path.stat().st_size,
    }


def _run(
    command: list[str],
    *,
    timeout: int = 60,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and process.returncode != 0:
        raise BackupError("backup container command failed")
    return process


def snapshot_live_database(destination: pathlib.Path, backup_id: str) -> None:
    container_path = f"/data/.cf-backup-{backup_id}.sqlite3"
    program = """
import pathlib
import sqlite3
import sys
source = sqlite3.connect('file:/data/factory.db?mode=ro', uri=True, timeout=5)
target = sqlite3.connect(sys.argv[1], timeout=5)
try:
    source.backup(target)
    target.execute('PRAGMA journal_mode=WAL')
    target.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise SystemExit(2)
finally:
    target.close()
    source.close()
pathlib.Path(sys.argv[1] + '-wal').unlink(missing_ok=True)
pathlib.Path(sys.argv[1] + '-shm').unlink(missing_ok=True)
""".strip()
    try:
        _run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "app",
                "python",
                "-c",
                program,
                container_path,
            ]
        )
        _run(["docker", "compose", "cp", f"app:{container_path}", str(destination)])
    finally:
        _run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "app",
                "python",
                "-c",
                "import pathlib,sys; pathlib.Path(sys.argv[1]).unlink(missing_ok=True)",
                container_path,
            ],
            check=False,
        )


def _git_identity() -> str:
    commit = _run(["git", "rev-parse", "HEAD"], timeout=30).stdout.strip()
    if _run(["git", "status", "--porcelain=v1"], timeout=30).stdout.strip():
        raise BackupError("release backup requires a clean Git worktree")
    return commit


def run_backup(backup_id: str) -> dict[str, Any]:
    commit = _git_identity()
    with tempfile.TemporaryDirectory(prefix="cf-live-backup-") as temporary:
        database_path = pathlib.Path(temporary) / "factory.sqlite3"
        snapshot_live_database(database_path, backup_id)
        archive = build_backup_archive(
            backup_id=backup_id,
            database_path=database_path,
            contract_path=CONTRACT_PATH,
            evidence_root=EVIDENCE_ROOT,
            output_root=BACKUP_ROOT,
            git_commit=commit,
        )
    report = validate_backup_archive(archive)
    report.update(
        {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "archive_path": archive.relative_to(ROOT).as_posix(),
            "retention_count": BACKUP_RETENTION_COUNT,
            "provider_calls": 0,
        }
    )
    _write_json(LATEST_REPORT, report)
    return report


def prune_backups(
    root: pathlib.Path = BACKUP_ROOT,
    *,
    keep: int = BACKUP_RETENTION_COUNT,
    apply: bool = False,
) -> list[pathlib.Path]:
    if keep < 1:
        raise BackupError("backup retention count must be positive")
    archives = sorted(
        root.glob("backup-*.zip"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    candidates = archives[keep:]
    if apply:
        for path in candidates:
            path.unlink()
    return candidates


def main(argv: list[str] | None = None) -> int:
    arguments = argv or []
    try:
        if arguments == ["--validate"]:
            raw_path = str(os.environ.get("BACKUP_PATH") or "").strip()
            if raw_path:
                path = pathlib.Path(raw_path).resolve()
            else:
                latest = json.loads(LATEST_REPORT.read_text(encoding="utf-8"))
                path = ROOT / str(latest.get("archive_path") or "")
            report = validate_backup_archive(path)
            print(
                "backup-check: PASS "
                f"id={report['backup_id']} evidence={report['evidence_count']} integrity=ok"
            )
            return 0
        if arguments == ["--prune"]:
            apply = os.environ.get("ALLOW_BACKUP_PRUNE", "").casefold() == "true"
            candidates = prune_backups(apply=apply)
            state = "APPLIED" if apply else "DRY-RUN"
            print(
                f"backup-prune: {state} candidates={len(candidates)} keep={BACKUP_RETENTION_COUNT}"
            )
            return 0
        if arguments:
            raise BackupError("unknown backup command")
        backup_id = str(os.environ.get("BACKUP_ID") or "").strip()
        if not backup_id:
            raise BackupError("BACKUP_ID is required")
        report = run_backup(backup_id)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"backup: FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        "backup: PASS "
        f"id={report['backup_id']} evidence={report['evidence_count']} provider_calls=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
