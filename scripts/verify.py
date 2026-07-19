from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from scripts.evidence import (
    validate_evidence_directory,
    validate_frozen_live_directory,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
VERIFY_ROOT = ROOT / "runtime" / "verification"
LIVE_ROOT = ROOT / "runtime" / "evaluation" / "live"
EVIDENCE_ROOT = ROOT / "artifacts" / "evidence"
BACKUP_ROOT = ROOT / "runtime" / "backups"
PROTECTED_PATHS = (
    ROOT / "runtime" / "budget" / "usage.jsonl",
    ROOT / "runtime" / "budget" / "runs",
    ROOT / "runtime" / "live-probes",
    ROOT / "runtime" / "live-campaigns",
    LIVE_ROOT,
    EVIDENCE_ROOT,
    BACKUP_ROOT,
)
PROVIDER_ENV_NAMES = (
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
)
LIVE_CONTROL_ENV_NAMES = (
    "ALLOW_LIVE_EVAL",
    "ALLOW_LIVE_PROBE",
    "ALLOW_GATE2_LIVE",
    "ALLOW_BOOTSTRAP_REVIEW",
    "FORCE_BOOTSTRAP_REVIEW",
    "EVALUATION_ID",
    "PREVIOUS_EVALUATION_ID",
    "EVALUATION_RETRY_REASON",
    "COMPARATOR_RUN_ID",
    "ALLOW_GPT54_COMPARATOR",
    "OPENROUTER_ENABLED",
)
REQUIRED_DOCS = (
    "README.md",
    "docs/ARCHITECTURE.md",
    "docs/SECURITY.md",
    "docs/EVALUATION.md",
    "docs/DATA_PROVENANCE.md",
    "docs/ASSUMPTIONS.md",
    "docs/TRACEABILITY.md",
    "docs/LEGACY_REUSE.md",
    "docs/DEPENDENCIES.md",
    "docs/DEMO_SCRIPT.md",
    "docs/RUNBOOK.md",
    "docs/SUBMISSION_CHECKLIST.md",
    "DECISIONS.md",
    "STATUS.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
)
RELEASE_STATUSES = (
    "IMPLEMENTATION_COMPLETE",
    "WAITING_FOR_OPERATOR",
    "WAITING_FOR_OPERATOR_SKILL_APPROVAL",
    "WAITING_FOR_OPERATOR_RUNTIME_PATCH",
    "SUBMISSION_READY",
)


class VerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class GateCommand:
    gate_id: str
    command: tuple[str, ...]


CORE_COMMANDS = (
    GateCommand("skill_contract", ("uv", "run", "python", "-m", "scripts.skill_contract")),
    GateCommand("spec_drift", ("uv", "run", "python", "-m", "scripts.spec_drift")),
    GateCommand("architecture", ("uv", "run", "python", "-m", "scripts.architecture_scan")),
    GateCommand("source_audit", ("uv", "run", "python", "-m", "scripts.source_audit")),
    GateCommand("budget_status", ("uv", "run", "python", "-m", "scripts.budget_control", "status")),
    GateCommand("budget_tests", ("make", "test-budget")),
    GateCommand("gate1", ("uv", "run", "python", "-m", "scripts.gate1")),
    GateCommand("gate3", ("uv", "run", "python", "-m", "scripts.gate3")),
    GateCommand("lint", ("make", "lint")),
    GateCommand("format", ("make", "format-check")),
    GateCommand("typecheck", ("make", "typecheck")),
    GateCommand("tests", ("make", "test")),
    GateCommand("contract_tests", ("make", "test-contract")),
    GateCommand("build", ("make", "build")),
    GateCommand("image_security", ("uv", "run", "python", "-m", "scripts.image_security")),
    GateCommand("compose_smoke", ("make", "smoke")),
    GateCommand("playwright", ("make", "e2e")),
    GateCommand("controlled_retry_playwright", ("make", "e2e-controlled-retry")),
    GateCommand("evaluation_replay", ("make", "eval-replay")),
    GateCommand("chaos", ("make", "test-chaos")),
    GateCommand("security_license", ("make", "security")),
)

CommandRunner = Callable[[Sequence[str], dict[str, str]], int]


def safe_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    environment = dict(source if source is not None else os.environ)
    for name in (*PROVIDER_ENV_NAMES, *LIVE_CONTROL_ENV_NAMES):
        environment.pop(name, None)
    environment.update(
        {
            "ALLOW_LIVE_EVAL": "false",
            "ALLOW_LIVE_PROBE": "false",
            "ALLOW_GATE2_LIVE": "false",
            "ALLOW_BOOTSTRAP_REVIEW": "false",
            "FORCE_BOOTSTRAP_REVIEW": "false",
            "OPENROUTER_ENABLED": "false",
            "CONTROLLED_PROVIDER_RETRY_ENABLED": "false",
            "CONTROLLED_PROVIDER_RETRY_FAULT_PROFILE": "none",
        }
    )
    return environment


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def path_state(path: pathlib.Path) -> str:
    if not path.exists():
        return "missing"
    if path.is_file():
        return f"file:{path.stat().st_size}:{_sha256(path)}"
    digest = hashlib.sha256()
    for candidate in sorted(item for item in path.rglob("*") if item.is_file()):
        relative = candidate.relative_to(path).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(_sha256(candidate)))
    return f"tree:{digest.hexdigest()}"


def protected_state(paths: Sequence[pathlib.Path] = PROTECTED_PATHS) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in paths:
        try:
            label = path.relative_to(ROOT).as_posix()
        except ValueError:
            label = path.as_posix()
        result[label] = path_state(path)
    return result


def validate_readme_release_status(readme: str) -> str:
    anchor = "Текущий release status:"
    if readme.count(anchor) != 1:
        raise VerificationError("README must contain one canonical release status")
    status_block = readme.split(anchor, 1)[1].split("\n\n", 1)[0]
    matches = [status for status in RELEASE_STATUSES if f"`{status}`" in status_block]
    if len(matches) != 1:
        raise VerificationError("README release status must be exactly one allowed value")
    return matches[0]


def validate_documentation(root: pathlib.Path = ROOT) -> dict[str, Any]:
    missing = [relative for relative in REQUIRED_DOCS if not (root / relative).is_file()]
    if missing:
        raise VerificationError(f"required documentation is missing: {missing[0]}")
    readme = (root / "README.md").read_text(encoding="utf-8")
    marker = "## Запуск — четыре шага"
    if readme.count(marker) != 1:
        raise VerificationError("README must contain one canonical four-step startup section")
    section = readme.split(marker, 1)[1].split("\n## ", 1)[0]
    numbered = [line for line in section.splitlines() if line[:2] in {"1.", "2.", "3.", "4."}]
    if [line[:2] for line in numbered] != ["1.", "2.", "3.", "4."]:
        raise VerificationError("README startup section is not exactly four ordered steps")
    required_readme_terms = (
        "synthetic-only/no-send",
        "Ouroboros",
        "make init",
        "make up",
        "127.0.0.1:8080",
        "P1/P2",
        "LICENSE",
    )
    if any(term not in readme for term in required_readme_terms):
        raise VerificationError("README omits a required product/start/security/status topic")
    demo = (root / "docs" / "DEMO_SCRIPT.md").read_text(encoding="utf-8")
    if "Target: 168 seconds" not in demo or "<180" not in demo or "| 2 |  |  |" not in demo:
        raise VerificationError("demo script/timing/rehearsal template is incomplete")
    return {
        "required_file_count": len(REQUIRED_DOCS),
        "startup_step_count": 4,
        "release_status": validate_readme_release_status(readme),
    }


def _subprocess_runner(command: Sequence[str], environment: dict[str, str]) -> int:
    process = subprocess.run(
        list(command),
        cwd=ROOT,
        env=environment,
        timeout=900,
        check=False,
    )
    return process.returncode


def _write_report(path: pathlib.Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def run_core(
    *,
    command_runner: CommandRunner = _subprocess_runner,
    report_path: pathlib.Path = VERIFY_ROOT / "core" / "latest.json",
    protected_paths: Sequence[pathlib.Path] = PROTECTED_PATHS,
) -> dict[str, Any]:
    before = protected_state(protected_paths)
    environment = safe_environment()
    results: list[dict[str, Any]] = []
    failure: str | None = None
    started = time.monotonic()
    try:
        documentation = validate_documentation()
    except VerificationError as exc:
        documentation = {"error": str(exc)}
        failure = "documentation"
    if failure is None:
        for gate in CORE_COMMANDS:
            gate_started = time.monotonic()
            returncode = command_runner(gate.command, environment)
            results.append(
                {
                    "gate_id": gate.gate_id,
                    "command": list(gate.command),
                    "returncode": returncode,
                    "duration_seconds": round(time.monotonic() - gate_started, 3),
                    "status": "PASS" if returncode == 0 else "FAIL",
                }
            )
            if returncode != 0:
                failure = gate.gate_id
                break
    after = protected_state(protected_paths)
    protected_unchanged = before == after
    if not protected_unchanged and failure is None:
        failure = "provider_or_evidence_state_mutated"
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "PASS" if failure is None else "FAIL",
        "verification_level": "core",
        "provider_calls_started": 0,
        "live_runs_created": 0,
        "evidence_directories_created": 0,
        "protected_state_unchanged": protected_unchanged,
        "protected_state_before": before,
        "protected_state_after": after,
        "documentation": documentation,
        "completed_gate_count": sum(row["status"] == "PASS" for row in results),
        "required_gate_count": len(CORE_COMMANDS),
        "failed_gate": failure,
        "gates": results,
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    _write_report(report_path, report)
    if failure is not None:
        raise VerificationError(f"verify-core failed at {failure}")
    return report


def _git(arguments: list[str]) -> str:
    process = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if process.returncode != 0:
        raise VerificationError("Git verification command failed")
    return process.stdout.strip()


def _current_commit() -> str:
    commit = _git(["rev-parse", "HEAD"])
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise VerificationError("current Git commit is invalid")
    if _git(["status", "--porcelain=v1"]):
        raise VerificationError("implementation verification requires a clean frozen worktree")
    return commit


def _select_frozen_live(evaluation_id: str | None) -> pathlib.Path:
    if evaluation_id:
        candidate = LIVE_ROOT / evaluation_id
        if not candidate.is_dir():
            raise VerificationError("selected frozen live evaluation does not exist")
        return candidate
    candidates = [
        path
        for path in sorted(LIVE_ROOT.iterdir() if LIVE_ROOT.is_dir() else [])
        if path.is_dir() and (path / "FROZEN.json").is_file()
    ]
    if len(candidates) != 1:
        raise VerificationError("select exactly one frozen live evaluation with EVALUATION_ID")
    return candidates[0]


def _select_evidence(evaluation_id: str) -> pathlib.Path:
    matches: list[pathlib.Path] = []
    for path in sorted(EVIDENCE_ROOT.iterdir() if EVIDENCE_ROOT.is_dir() else []):
        if not path.is_dir() or not (path / "manifest.json").is_file():
            continue
        try:
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(manifest, dict) and manifest.get("evaluation_id") == evaluation_id:
            matches.append(path)
    if len(matches) != 1:
        raise VerificationError("exactly one implementation evidence directory is required")
    return matches[0]


def _load_green_report(
    path: pathlib.Path, label: str, *, commit: str | None = None
) -> dict[str, Any]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"{label} report is missing or malformed") from exc
    if not isinstance(report, dict) or report.get("status") != "PASS":
        raise VerificationError(f"{label} report is not green")
    if commit is not None and report.get("git_commit") != commit:
        raise VerificationError(f"{label} report is not bound to the current commit")
    return {str(key): value for key, value in report.items()}


def validate_backup_binding(
    report: dict[str, Any],
    validated: dict[str, Any],
    *,
    commit: str,
    evaluation_id: str,
) -> None:
    if (
        report.get("git_commit") != commit
        or validated.get("git_commit") != commit
        or report.get("archive_sha256") != validated.get("archive_sha256")
        or evaluation_id not in (validated.get("evidence_evaluation_ids") or [])
        or report.get("provider_calls") != 0
    ):
        raise VerificationError("backup is not bound to current commit and frozen evidence")


def validate_release_backup(*, commit: str, evaluation_id: str) -> dict[str, Any]:
    from scripts.backup import validate_backup_archive

    report = _load_green_report(BACKUP_ROOT / "latest.json", "backup", commit=commit)
    archive = (ROOT / str(report.get("archive_path") or "")).resolve()
    try:
        archive.relative_to(BACKUP_ROOT.resolve())
    except ValueError as exc:
        raise VerificationError("backup archive path escapes the project backup root") from exc
    validated = validate_backup_archive(archive)
    validate_backup_binding(report, validated, commit=commit, evaluation_id=evaluation_id)
    return {**report, "validated": validated}


def run_implementation() -> dict[str, Any]:
    protected_before = protected_state()
    core = run_core()
    commit = _current_commit()
    source = _select_frozen_live(str(os.environ.get("EVALUATION_ID") or "").strip() or None)
    live = validate_frozen_live_directory(source)
    if live.get("app_commit") != commit or live.get("git_dirty") is not False:
        raise VerificationError("frozen live evaluation does not match the current commit")
    evaluation_id = str(live.get("evaluation_id") or "")
    evidence_root = _select_evidence(evaluation_id)
    evidence = validate_evidence_directory(evidence_root)
    if evidence.get("app_commit") != commit:
        raise VerificationError("implementation evidence does not match the current commit")
    backup = validate_release_backup(commit=commit, evaluation_id=evaluation_id)
    clean_clone = _load_green_report(
        VERIFY_ROOT / "clean-clone" / "latest.json",
        "clean clone",
        commit=commit,
    )
    demo = _load_green_report(ROOT / "runtime" / "demo" / "latest.json", "demo")
    if (
        demo.get("action") != "check"
        or demo.get("release_ready") is not True
        or demo.get("b01_ready") is not True
        or demo.get("b03_ready") is not True
    ):
        raise VerificationError("demo readiness does not prove B01/B03")
    from scripts.package_submission import validate_pipeline_readiness

    pipeline = validate_pipeline_readiness()
    protected_after = protected_state()
    if protected_before != protected_after:
        raise VerificationError("verify-implementation mutated live/provider/evidence state")
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "PASS",
        "verification_level": "implementation",
        "git_commit": commit,
        "evaluation_id": evaluation_id,
        "live_case_count": live["live_case_count"],
        "business_case_count": live["business_case_count"],
        "evidence_path": evidence_root.relative_to(ROOT).as_posix(),
        "human_gate_status": evidence["human_gate_status"],
        "core_duration_seconds": core["duration_seconds"],
        "clean_clone": clean_clone,
        "backup": backup,
        "demo": demo,
        "packaging_pipeline": pipeline,
        "provider_calls_started": 0,
        "live_runs_created": 0,
        "evidence_directories_created": 0,
        "protected_state_unchanged": True,
    }
    _write_report(VERIFY_ROOT / "implementation" / "latest.json", report)
    return report


def run_submission() -> dict[str, Any]:
    implementation = run_implementation()
    from scripts.package_submission import validate_operator_artifacts

    submission_id = str(os.environ.get("SUBMISSION_ID") or "").strip()
    if not submission_id:
        raise VerificationError("SUBMISSION_ID is required for human-gate validation")
    operator = validate_operator_artifacts(
        ROOT / "artifacts" / "operator" / submission_id,
        expected_evaluation_id=str(implementation["evaluation_id"]),
        evidence_root=ROOT / str(implementation["evidence_path"]),
    )
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "PASS",
        "verification_level": "submission",
        "git_commit": implementation["git_commit"],
        "evaluation_id": implementation["evaluation_id"],
        "submission_id": submission_id,
        "operator_artifacts": operator,
        "provider_calls_started": 0,
    }
    _write_report(VERIFY_ROOT / "submission" / "latest.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("level", choices=("core", "implementation", "submission"))
    args = parser.parse_args(argv)
    try:
        report = (
            run_core()
            if args.level == "core"
            else run_implementation()
            if args.level == "implementation"
            else run_submission()
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"verify-{args.level}: FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        f"verify-{args.level}: PASS gates={report.get('completed_gate_count', 'all')} "
        "provider_calls=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
