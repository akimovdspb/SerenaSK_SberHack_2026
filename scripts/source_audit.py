from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys
import tomllib
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
FORBIDDEN_TRACKED_BASENAMES = {
    ".env",
    "OPENAI_API_KEY.txt",
    "OPENROUTER_API_KEY.txt",
    "operator-limits.local.yaml",
    "operator-limits.yaml",
}


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_pinned_source(root: pathlib.Path, lock: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    tag = str(lock.get("tag") or "")
    commit = str(lock.get("commit") or "")
    if tag != "v6.61.4" or commit != "a00d51dd414f794d830cacf7da760061e442fa88":
        errors.append("pinned Ouroboros tag or commit is not canonical")
    archive = root / "runtime" / "upstream" / "ouroboros-v6.61.4.tar.gz"
    requirements = root / "ouroboros" / "requirements.lock"
    try:
        if sha256_file(archive) != lock.get("source_archive_sha256"):
            errors.append("pinned Ouroboros source archive hash drifted")
        if sha256_file(requirements) != lock.get("requirements_lock_sha256"):
            errors.append("pinned Ouroboros requirements lock hash drifted")
    except OSError:
        errors.append("pinned Ouroboros source or requirements lock is unavailable")
    source = root / "runtime" / "upstream" / "ouroboros"
    try:
        if (source / "VERSION").read_text(encoding="utf-8").strip() != tag.removeprefix("v"):
            errors.append("pinned Ouroboros VERSION readback drifted")
        project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
        project_metadata = project.get("project") or {}
        declared_license = str((project_metadata.get("license") or {}).get("text") or "")
        if declared_license != "MIT" or lock.get("license_declared_by_upstream") != "MIT":
            errors.append("upstream license declaration is not MIT")
        if (source / "LICENSE").exists():
            errors.append("upstream license-file discrepancy changed and requires notice review")
    except (OSError, tomllib.TOMLDecodeError, AttributeError):
        errors.append("pinned Ouroboros source metadata is unreadable")
    notices = (root / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for required in (tag, commit, "Declared license: MIT", "does not contain the `LICENSE` file"):
        if required not in notices:
            errors.append("third-party notice does not match the pinned source discrepancy")
            break
    return errors


def tracked_boundary_errors(root: pathlib.Path) -> list[str]:
    process = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if process.returncode != 0:
        return ["Git tracked-file audit failed"]
    errors: list[str] = []
    for raw in process.stdout.split(b"\0"):
        if not raw:
            continue
        path = pathlib.PurePosixPath(raw.decode("utf-8", errors="strict"))
        if path.parts and path.parts[0] in {"private_sources", "runtime"}:
            errors.append("private or runtime material is tracked by Git")
        if path.name in FORBIDDEN_TRACKED_BASENAMES:
            errors.append("credential or operator file is tracked by Git")
    for candidate in (
        "private_sources/probe",
        "runtime/probe",
        "OPENAI_API_KEY.txt",
        "operator-limits.local.yaml",
    ):
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", candidate],
            cwd=root,
            timeout=10,
            check=False,
        )
        if ignored.returncode != 0:
            errors.append(f"required private path is not ignored: {candidate}")
    return errors


def audit_source(root: pathlib.Path = ROOT) -> list[str]:
    try:
        lock = json.loads((root / "ouroboros" / "ouroboros.lock").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["pinned Ouroboros lock is unreadable"]
    if not isinstance(lock, dict):
        return ["pinned Ouroboros lock is invalid"]
    return validate_pinned_source(root, lock) + tracked_boundary_errors(root)


def main() -> int:
    try:
        errors = audit_source()
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"source-audit: FAIL: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"source-audit: FAIL: {error}", file=sys.stderr)
        return 1
    print(
        "source-audit: PASS runtime=v6.61.4 license=MIT-discrepancy-documented "
        "private_sources=untracked secrets=untracked"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
