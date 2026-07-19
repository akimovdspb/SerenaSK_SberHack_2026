from __future__ import annotations

import hashlib
import json
import pathlib
import re
import stat
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from typing import Any

from scripts.architecture_scan import scan_backend
from scripts.compose_contract import load_rendered_compose, validate_compose, validate_static_files

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "runtime" / "security" / "latest.json"
SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{32,}", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\+7|8)[\s()-]*\d{3}[\s()-]*\d{3}"
    r"[\s()-]*\d{2}[\s()-]*\d{2}(?![A-Za-z0-9])"
)
URL_RE = re.compile(r"https://[^\s\"']+")
RESERVED_URL_RE = re.compile(r"^https://[^/]+\.(?:test|invalid)(?:/|$)")
BASIC_AUTH_RE = re.compile(r"\bBasic\s+[A-Za-z0-9+/]{8,}={0,2}\b", re.IGNORECASE)
SECRET_FILENAMES = {
    "OPENAI_API_KEY.txt",
    "OPENROUTER_API_KEY.txt",
    "operator-limits.yaml",
    "operator-limits.local.yaml",
    ".env",
}
TEXT_SUFFIXES = {
    ".csv",
    ".html",
    ".json",
    ".jsonl",
    ".md",
    ".py",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}


class SecurityScanError(RuntimeError):
    pass


def _git(args: list[str], *, text: bool = False) -> str | bytes:
    process = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=text,
        timeout=60,
        check=False,
    )
    if process.returncode != 0:
        raise SecurityScanError("Git security scan command failed; output withheld")
    if not isinstance(process.stdout, (str, bytes)):
        raise SecurityScanError("Git security scan output type is invalid")
    return process.stdout


def tracked_paths() -> list[pathlib.Path]:
    raw = _git(["ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    if not isinstance(raw, bytes):
        raise SecurityScanError("Git tracked path output type is invalid")
    return [ROOT / item.decode("utf-8") for item in raw.split(b"\0") if item]


def secret_value_present(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_VALUE_PATTERNS)


def _read_text(path: pathlib.Path) -> str | None:
    if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
        return None
    if path.stat().st_size > 5_000_000:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def scan_tracked_tree(paths: list[pathlib.Path] | None = None) -> list[str]:
    errors: list[str] = []
    for path in paths or tracked_paths():
        relative = path.relative_to(ROOT).as_posix()
        if path.name in SECRET_FILENAMES or any(part == "secrets" for part in path.parts):
            errors.append(f"tracked_secret_file:{relative}")
            continue
        text = _read_text(path)
        if text is not None and secret_value_present(text):
            errors.append(f"tracked_secret_value:{relative}")
    return errors


def scan_git_history() -> list[str]:
    raw = _git(["log", "--all", "--no-color", "-p", "--format=commit:%H"], text=True)
    if not isinstance(raw, str):
        raise SecurityScanError("Git history output type is invalid")
    return ["history_secret_value"] if secret_value_present(raw) else []


def scan_synthetic_data(data_root: pathlib.Path | None = None) -> list[str]:
    errors: list[str] = []
    root = data_root or ROOT / "data" / "synthetic"
    for path in sorted(root.rglob("*.json")):
        text = path.read_text(encoding="utf-8")
        if EMAIL_RE.search(text) or PHONE_RE.search(text) or secret_value_present(text):
            errors.append(f"synthetic_pii_or_secret:{path.name}")
        for url in URL_RE.findall(text):
            if not RESERVED_URL_RE.match(url.rstrip(".,;:!?")):
                errors.append(f"synthetic_non_reserved_url:{path.name}")
    return sorted(set(errors))


def scan_generated_artifacts(roots: list[pathlib.Path] | None = None) -> list[str]:
    errors: list[str] = []
    targets = roots or [
        ROOT / "artifacts",
        ROOT / "runtime" / "evaluation",
        ROOT / "runtime" / "backups",
    ]
    for root in targets:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() == ".zip":
                errors.extend(_scan_archive(path))
            text = _read_text(path)
            if text is None:
                continue
            if secret_value_present(text):
                errors.append(f"artifact_secret_value:{path.name}")
            if EMAIL_RE.search(text) or PHONE_RE.search(text):
                errors.append(f"artifact_pii:{path.name}")
            if re.search(r"https?://[^/\s]+\.local\b", text, re.IGNORECASE):
                errors.append(f"artifact_internal_url:{path.name}")
    return sorted(set(errors))


def _scan_archive(path: pathlib.Path) -> list[str]:
    errors: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > 5_000:
                return [f"artifact_archive_too_many_members:{path.name}"]
            total_size = 0
            for member in members:
                candidate = pathlib.PurePosixPath(member.filename)
                mode = member.external_attr >> 16
                total_size += member.file_size
                if (
                    candidate.is_absolute()
                    or ".." in candidate.parts
                    or "\\" in member.filename
                    or stat.S_ISLNK(mode)
                ):
                    errors.append(f"artifact_unsafe_archive_path:{path.name}")
                    continue
                if total_size > 100_000_000 or member.file_size > 20_000_000:
                    errors.append(f"artifact_archive_size_limit:{path.name}")
                    continue
                if member.is_dir() or member.file_size > 5_000_000:
                    continue
                data = archive.read(member)
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if secret_value_present(text):
                    errors.append(f"artifact_archive_secret_value:{path.name}")
                if BASIC_AUTH_RE.search(text):
                    errors.append(f"artifact_archive_basic_auth:{path.name}")
                if _archive_pii_present(member.filename, text):
                    errors.append(f"artifact_archive_pii:{path.name}")
                if re.search(r"https?://[^/\s]+\.local\b", text, re.IGNORECASE):
                    errors.append(f"artifact_archive_internal_url:{path.name}")
    except (OSError, zipfile.BadZipFile, RuntimeError):
        errors.append(f"artifact_invalid_archive:{path.name}")
    return sorted(set(errors))


def _archive_pii_present(member_name: str, text: str) -> bool:
    if not member_name.endswith((".trace", ".network")):
        return bool(EMAIL_RE.search(text) or PHONE_RE.search(text))
    parsed_any = False

    def contains(value: object, *, key: str = "") -> bool:
        if key in {"sha1", "sha256", "snapshotId", "resourceId"}:
            return False
        if isinstance(value, dict):
            return any(contains(item, key=str(name)) for name, item in value.items())
        if isinstance(value, list):
            return any(contains(item) for item in value)
        return isinstance(value, str) and bool(EMAIL_RE.search(value) or PHONE_RE.search(value))

    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return bool(EMAIL_RE.search(text) or PHONE_RE.search(text))
        parsed_any = True
        if contains(value):
            return True
    return not parsed_any and bool(EMAIL_RE.search(text) or PHONE_RE.search(text))


def scan_no_send_and_archive_boundaries() -> list[str]:
    errors: list[str] = []
    app_root = ROOT / "apps" / "api" / "app"
    banned_send_fragments = (
        "import smtplib",
        "from smtplib",
        "import sendgrid",
        "import twilio",
        "api.sendsay",
        "send_message(",
    )
    for path in sorted(app_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8").lower()
        if any(fragment in source for fragment in banned_send_fragments):
            errors.append(f"send_adapter_present:{path.name}")
        if "extractall(" in source or "extract(" in source:
            errors.append(f"unsafe_archive_extraction:{path.name}")
    return errors


def scan_dependency_and_license_files() -> list[str]:
    return scan_dependency_and_license_files_at(ROOT)


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_dependency_and_license_files_at(root: pathlib.Path) -> list[str]:
    input_paths = (
        root / "uv.lock",
        root / "package-lock.json",
        root / "apps" / "requirements.lock",
        root / "ouroboros" / "requirements.lock",
        root / "data" / "dependency_license_overrides.json",
    )
    required = (
        root / "LICENSE",
        root / "THIRD_PARTY_NOTICES.md",
        *input_paths,
        root / "runtime" / "security" / "dependencies.json",
        root / "runtime" / "security" / "THIRD_PARTY_NOTICES.generated.md",
    )
    errors = [
        f"missing_dependency_or_license_file:{path.name}" for path in required if not path.is_file()
    ]
    if errors:
        return errors

    report_path = root / "runtime" / "security" / "dependencies.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ["dependency_report_invalid_json"]
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        return ["dependency_report_invalid_schema"]
    if (
        report.get("status") != "PASS"
        or report.get("failure_count") != 0
        or not isinstance(report.get("component_count"), int)
        or report["component_count"] <= 0
    ):
        errors.append("dependency_report_not_green")
    raw_inputs = report.get("input_files")
    if not isinstance(raw_inputs, list):
        errors.append("dependency_report_inputs_missing")
        return errors
    recorded = {
        str(item.get("path")): str(item.get("sha256"))
        for item in raw_inputs
        if isinstance(item, dict)
    }
    expected = {path.relative_to(root).as_posix(): _sha256(path) for path in input_paths}
    if recorded != expected:
        errors.append("dependency_report_inputs_stale")
    return errors


def run_security_scan() -> dict[str, Any]:
    categories: dict[str, list[str]] = {
        "tracked_tree": scan_tracked_tree(),
        "git_history": scan_git_history(),
        "synthetic_data": scan_synthetic_data(),
        "generated_artifacts": scan_generated_artifacts(),
        "no_send_archive": scan_no_send_and_archive_boundaries(),
        "dependencies_licenses": scan_dependency_and_license_files(),
        "architecture": scan_backend(ROOT),
        "compose": validate_compose(load_rendered_compose()) + validate_static_files(),
    }
    checks = {key: not value for key, value in categories.items()}
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "finding_counts": {key: len(value) for key, value in categories.items()},
        "findings": categories,
        "secret_values_in_report": False,
    }


def _atomic_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    try:
        report = run_security_scan()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"security-scan: FAIL: {type(exc).__name__}", file=sys.stderr)
        return 1
    _atomic_json(REPORT_PATH, report)
    if report["status"] != "PASS":
        for category, count in report["finding_counts"].items():
            if count:
                print(f"security-scan: FAIL: {category} findings={count}", file=sys.stderr)
        return 1
    print(
        "security-scan: PASS tree=clean history=clean pii=clean artifacts=clean "
        "no_send=true network=loopback licenses=present"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
