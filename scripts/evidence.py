# ruff: noqa: RUF001 -- Russian report copy intentionally coexists with Latin identifiers.
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import math
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from scripts.evaluation import expected_cases, review_packet_case_ids
from scripts.security_scan import scan_generated_artifacts

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIVE_ROOT = ROOT / "runtime" / "evaluation" / "live"
CHAOS_PATH = ROOT / "runtime" / "evaluation" / "chaos" / "latest.json"
SECURITY_PATH = ROOT / "runtime" / "security" / "latest.json"
BROWSER_RESULTS_ROOT = ROOT / "runtime" / "playwright" / "results"
CONTRACT_LOCK_PATH = ROOT / "runtime" / "contracts" / "communication_factory.lock.json"
EVIDENCE_ROOT = ROOT / "artifacts" / "evidence"
CASE_FIXTURE_PATH = ROOT / "data" / "synthetic" / "cases" / "gate1.json"
POLICY_ROOT = ROOT / "data" / "synthetic" / "policies"
ALLOWED_TOOLS = ("mcp_factory__cf_context_get", "mcp_factory__cf_draft_save")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
BASIC_AUTH_BYTES = re.compile(rb"\bBasic\s+[A-Za-z0-9+/]{8,}={0,2}", re.IGNORECASE)
RUBRIC = (
    ("clarity", "Ясность"),
    ("natural_russian", "Естественность русского языка"),
    ("brief_fit", "Соответствие сегменту и брифу"),
    ("personalization", "Полезность персонализации"),
    ("persuasion_without_pressure", "Убедительность без давления"),
    ("non_template_quality", "Отсутствие шаблонности и странностей"),
    ("channel_consistency", "Согласованность SMS и e-mail"),
    ("demo_readiness", "Пригодность для демонстрации после минимальной правки"),
)
REQUIRED_TOP_LEVEL_FILES = {
    "report.pdf",
    "report.jpg",
    "report.html",
    "metrics.json",
    "business-results.csv",
    "business-results.jsonl",
    "qualitative-review.json",
    "security-report.json",
    "stability-report.json",
    "manifest.json",
    "checksums.sha256",
    "IMMUTABLE.json",
}

Renderer = Callable[[pathlib.Path, pathlib.Path, pathlib.Path], None]


class EvidenceError(RuntimeError):
    pass


def _load_object(path: pathlib.Path, label: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{label} is unreadable") from exc
    if not isinstance(raw, dict):
        raise EvidenceError(f"{label} must be a JSON object")
    return {str(key): value for key, value in raw.items()}


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n"
    ).encode("utf-8")


def _write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(value))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: pathlib.Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _tree_hash(root: pathlib.Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(_sha256_file(path)))
    return digest.hexdigest()


def _git(args: list[str]) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if process.returncode != 0:
        raise EvidenceError("Git identity check failed")
    return process.stdout.strip()


def _current_git_identity() -> tuple[str, bool]:
    commit = _git(["rev-parse", "HEAD"])
    if not GIT_COMMIT_PATTERN.fullmatch(commit):
        raise EvidenceError("current Git commit is invalid")
    return commit, not bool(_git(["status", "--porcelain=v1"]))


def _checksum_rows(root: pathlib.Path) -> list[tuple[str, str]]:
    excluded = {"checksums.sha256", "FROZEN.json", "IMMUTABLE.json"}
    return [
        (_sha256_file(path), path.relative_to(root).as_posix())
        for path in sorted(item for item in root.rglob("*") if item.is_file())
        if path.name not in excluded
    ]


def _write_checksums(root: pathlib.Path) -> None:
    rows = _checksum_rows(root)
    (root / "checksums.sha256").write_text(
        "".join(f"{digest}  {relative}\n" for digest, relative in rows),
        encoding="utf-8",
    )


def _parse_checksums(path: pathlib.Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EvidenceError("checksums file is unreadable") from exc
    result: dict[str, str] = {}
    for line in lines:
        if "  " not in line:
            raise EvidenceError("checksums file has an invalid row")
        digest, relative = line.split("  ", 1)
        if not SHA256_PATTERN.fullmatch(digest) or not relative or relative in result:
            raise EvidenceError("checksums file has an invalid identity")
        candidate = pathlib.PurePosixPath(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise EvidenceError("checksums file contains an unsafe path")
        result[relative] = digest
    if not result:
        raise EvidenceError("checksums file is empty")
    return result


def validate_checksums(root: pathlib.Path) -> None:
    declared = _parse_checksums(root / "checksums.sha256")
    actual = {relative: digest for digest, relative in _checksum_rows(root)}
    if declared != actual:
        raise EvidenceError("artifact checksum inventory does not match")


def _validate_frozen_source(source_root: pathlib.Path, report: dict[str, Any]) -> None:
    if not (source_root / "FROZEN.json").is_file():
        raise EvidenceError("live evaluation is missing its immutable marker")
    validate_checksums(source_root)
    marker = _load_object(source_root / "FROZEN.json", "live immutable marker")
    if marker.get("evaluation_id") != report.get("evaluation_id"):
        raise EvidenceError("live immutable marker identity does not match")
    if marker.get("report_sha256") != _sha256_file(source_root / "report.json"):
        raise EvidenceError("live immutable marker does not bind report.json")
    if marker.get("checksums_sha256") != _sha256_file(source_root / "checksums.sha256"):
        raise EvidenceError("live immutable marker does not bind checksums")


def _case_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_cases = report.get("cases")
    if not isinstance(raw_cases, list):
        raise EvidenceError("live evaluation cases must be a list")
    cases: dict[str, dict[str, Any]] = {}
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise EvidenceError("live evaluation case row must be an object")
        row = {str(key): value for key, value in raw.items()}
        case_id = str(row.get("case_id") or "")
        if not case_id or case_id in cases:
            raise EvidenceError("live evaluation case identity is invalid")
        cases[case_id] = row
    expected_ids = {f"B{ordinal:02d}" for ordinal in range(1, 16)}
    if set(cases) != expected_ids:
        raise EvidenceError("live evaluation must contain exactly B01-B15")
    return cases


def _case_metrics(case: dict[str, Any]) -> dict[str, Any]:
    raw = case.get("metrics")
    if not isinstance(raw, dict):
        raise EvidenceError(f"{case.get('case_id')} has no metrics object")
    return {str(key): value for key, value in raw.items()}


def _validate_live_case(case: dict[str, Any]) -> None:
    case_id = str(case.get("case_id") or "unknown")
    package = case.get("package")
    run = case.get("run")
    context = case.get("context")
    if not isinstance(package, dict) or package.get("mode") != "live_ouroboros":
        raise EvidenceError(f"{case_id} live package is missing or mislabelled")
    if not isinstance(run, dict):
        raise EvidenceError(f"{case_id} live run is missing")
    if run.get("status") != "COMPLETED" or run.get("mode") != "live_ouroboros":
        raise EvidenceError(f"{case_id} live run is not a successful terminal run")
    tools = run.get("tool_receipts")
    if not isinstance(tools, list) or sorted(str(item) for item in tools) != sorted(ALLOWED_TOOLS):
        raise EvidenceError(f"{case_id} does not show the exact two-tool receipt")
    if not isinstance(context, dict) or not context.get("context_version"):
        raise EvidenceError(f"{case_id} live context is missing")
    metrics = _case_metrics(case)
    if metrics.get("usage_complete") is not True:
        raise EvidenceError(f"{case_id} provider usage is incomplete")
    for field in ("user_visible_terminal_ms", "full_worker_occupancy_ms"):
        value = metrics.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise EvidenceError(f"{case_id} {field} is invalid")
    if int(metrics["user_visible_terminal_ms"]) >= 30_000:
        raise EvidenceError(f"{case_id} exceeded the terminal latency limit")
    calls = metrics.get("provider_calls")
    if not isinstance(calls, int) or isinstance(calls, bool) or calls <= 0:
        raise EvidenceError(f"{case_id} provider-call count is invalid")
    usage = metrics.get("usage_by_category")
    if not isinstance(usage, dict) or not usage:
        raise EvidenceError(f"{case_id} category usage is missing")


def validate_live_report(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if report.get("schema_version") != 1:
        raise EvidenceError("live evaluation schema version is invalid")
    if report.get("execution_kind") != "live_evaluation":
        raise EvidenceError("only a live evaluation may become implementation evidence")
    if report.get("frozen") is not True or report.get("status") != "PASS":
        raise EvidenceError("live evaluation is not frozen and green")
    if report.get("release_targets_passed") is not True or report.get("release_blockers") not in (
        [],
        (),
    ):
        raise EvidenceError("live evaluation still has release blockers")
    cases = _case_map(report)
    if not all(case.get("passed") is True for case in cases.values()):
        raise EvidenceError("not every business case passed its expected assertions")
    live_cases = [case for case in cases.values() if case.get("mode") == "live_ouroboros"]
    if len(live_cases) < 10:
        raise EvidenceError("live evaluation has fewer than ten live business cases")
    if any(cases[case_id].get("mode") != "live_ouroboros" for case_id in ("B01", "B03")):
        raise EvidenceError("B01 and B03 must both be live")
    if report.get("live_case_count") != len(live_cases):
        raise EvidenceError("reported live count does not match case modes")
    if report.get("business_case_count") != 15 or report.get("passed_case_count") != 15:
        raise EvidenceError("reported business counts are invalid")
    for case in live_cases:
        _validate_live_case(case)
    selected = review_packet_case_ids()
    for case_id in selected:
        if cases[case_id].get("mode") != "live_ouroboros":
            raise EvidenceError(f"preselected review packet {case_id} is not live")
    stability = report.get("stability")
    required_zero = (
        "crash_count",
        "stuck_run_count",
        "timeout_over_30s_count",
        "unsupported_approved_claim_count",
        "prompt_injection_success_count",
        "blocker_approval_success_count",
        "duplicate_paid_generation_count",
    )
    if not isinstance(stability, dict) or any(stability.get(field) != 0 for field in required_zero):
        raise EvidenceError("live stability/release invariants are not all zero")
    return cases


def validate_frozen_live_directory(root: pathlib.Path) -> dict[str, Any]:
    report = _load_object(root / "report.json", "live evaluation report")
    _validate_frozen_source(root, report)
    validate_live_report(report)
    return report


def _validate_chaos(report: dict[str, Any]) -> list[dict[str, Any]]:
    raw_cases = report.get("cases")
    if (
        report.get("status") != "PASS"
        or report.get("chaos_case_count") != 5
        or report.get("passed_case_count") != 5
        or report.get("provider_calls") != 0
        or report.get("normal_metrics_included") is not False
        or not isinstance(raw_cases, list)
    ):
        raise EvidenceError("chaos evidence is not a green isolated X01-X05 run")
    cases = [dict(item) for item in raw_cases if isinstance(item, dict)]
    if {item.get("case_id") for item in cases} != {f"X{ordinal:02d}" for ordinal in range(1, 6)}:
        raise EvidenceError("chaos evidence does not contain exact X01-X05")
    if not all(
        item.get("passed") is True and item.get("under_30_seconds") is True for item in cases
    ):
        raise EvidenceError("one or more chaos cases failed")
    return cases


def _validate_security(report: dict[str, Any]) -> None:
    counts = report.get("finding_counts")
    if (
        report.get("status") != "PASS"
        or report.get("secret_values_in_report") is not False
        or not isinstance(counts, dict)
        or any(value != 0 for value in counts.values())
    ):
        raise EvidenceError("security report is not green")


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        raise EvidenceError("normal live latency sample is empty")
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _aggregate_usage(cases: dict[str, dict[str, Any]]) -> dict[str, Any]:
    categories: dict[str, dict[str, int | float]] = {}
    totals: dict[str, int | float] = {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "cost_usd": 0.0,
    }
    for case in cases.values():
        if case.get("mode") != "live_ouroboros":
            continue
        metrics = _case_metrics(case)
        raw_usage = metrics.get("usage_by_category")
        if not isinstance(raw_usage, dict):
            raise EvidenceError("live category usage is malformed")
        for raw_name, raw_row in raw_usage.items():
            if not isinstance(raw_row, dict):
                raise EvidenceError("live category usage row is malformed")
            name = str(raw_name)
            row = categories.setdefault(
                name,
                {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cached_tokens": 0,
                    "cost_usd": 0.0,
                },
            )
            for field in ("calls", "prompt_tokens", "completion_tokens", "cached_tokens"):
                value = raw_row.get(field)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise EvidenceError(f"provider usage {name}.{field} is invalid")
                row[field] = int(row[field]) + value
                totals[field] = int(totals[field]) + value
            cost = raw_row.get("cost_usd")
            if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0:
                raise EvidenceError(f"provider usage {name}.cost_usd is invalid")
            row["cost_usd"] = round(float(row["cost_usd"]) + float(cost), 8)
            totals["cost_usd"] = round(float(totals["cost_usd"]) + float(cost), 8)
    return {"totals": totals, "by_category": categories, "account_remaining": "unknown"}


def _metrics(
    report: dict[str, Any],
    cases: dict[str, dict[str, Any]],
    chaos_cases: list[dict[str, Any]],
) -> dict[str, Any]:
    live = [case for case in cases.values() if case.get("mode") == "live_ouroboros"]
    user_visible = [int(_case_metrics(case)["user_visible_terminal_ms"]) for case in live]
    full_worker = [int(_case_metrics(case)["full_worker_occupancy_ms"]) for case in live]
    mode_counts: dict[str, int] = {}
    for case in cases.values():
        mode = str(case.get("mode") or "unknown")
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
    return {
        "schema_version": 1,
        "evaluation_id": report["evaluation_id"],
        "business": {
            "case_count": 15,
            "passed_count": 15,
            "expected_assertion_pass_rate": 1.0,
            "live_case_count": len(live),
            "live_case_rate": len(live) / 15,
            "mode_counts": mode_counts,
        },
        "normal_live_latency_ms": {
            "sample_count": len(live),
            "user_visible_terminal": {
                "p50": _percentile(user_visible, 0.50),
                "p95": _percentile(user_visible, 0.95),
                "max": max(user_visible),
            },
            "full_worker_occupancy": {
                "p50": _percentile(full_worker, 0.50),
                "p95": _percentile(full_worker, 0.95),
                "max": max(full_worker),
            },
        },
        "provider_usage": _aggregate_usage(cases),
        "chaos": {
            "case_count": len(chaos_cases),
            "passed_count": sum(item.get("passed") is True for item in chaos_cases),
            "normal_metrics_included": False,
        },
        "stability": report["stability"],
        "qualitative_review": {
            "status": "WAITING_FOR_OPERATOR",
            "manual_measured": False,
            "packet_count": 6,
            "aggregate": None,
        },
        "synthetic": True,
        "no_send": True,
    }


def _business_rows(cases: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ordinal in range(1, 16):
        case = cases[f"B{ordinal:02d}"]
        metrics = _case_metrics(case) if isinstance(case.get("metrics"), dict) else {}
        raw_run = case.get("run")
        run: dict[str, Any] = dict(raw_run) if isinstance(raw_run, dict) else {}
        rows.append(
            {
                "case_id": case["case_id"],
                "mode": case["mode"],
                "passed": bool(case["passed"]),
                "expected_initial": case.get("expected_initial"),
                "actual_initial": case.get("actual_initial"),
                "expected_terminal": case.get("expected_terminal"),
                "actual_terminal": case.get("actual_terminal"),
                "sms": (case.get("actual_channels") or {}).get("sms"),
                "email": (case.get("actual_channels") or {}).get("email"),
                "live_target": bool(case.get("live_target")),
                "run_id": run.get("run_id"),
                "package_hash": (case.get("package") or {}).get("package_hash"),
                "user_visible_terminal_ms": metrics.get("user_visible_terminal_ms"),
                "full_worker_occupancy_ms": metrics.get("full_worker_occupancy_ms"),
                "provider_calls": metrics.get("provider_calls", 0),
                "prompt_tokens": metrics.get("prompt_tokens", 0),
                "completion_tokens": metrics.get("completion_tokens", 0),
                "cached_tokens": metrics.get("cached_tokens", 0),
                "cost_usd": metrics.get("cost_usd", 0.0),
            }
        )
    return rows


def _write_business_formats(root: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    (root / "business-results.csv").write_text(buffer.getvalue(), encoding="utf-8")
    (root / "business-results.jsonl").write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n" for row in rows
        ),
        encoding="utf-8",
    )


def _case_fixture_map() -> dict[str, dict[str, Any]]:
    document = _load_object(CASE_FIXTURE_PATH, "business case fixture")
    raw = document.get("cases")
    if not isinstance(raw, list):
        raise EvidenceError("business case fixture has no case list")
    result = {
        str(item.get("case_id")): dict(item)
        for item in raw
        if isinstance(item, dict) and item.get("case_id")
    }
    if set(result) != {f"B{ordinal:02d}" for ordinal in range(1, 16)}:
        raise EvidenceError("business case input fixture does not contain B01-B15")
    return result


def _context_manifest(case: dict[str, Any]) -> dict[str, Any]:
    raw = case.get("context")
    context = raw if isinstance(raw, dict) else {}
    return {
        "context_version": context.get("context_version"),
        "source_manifest": context.get("source_manifest", []),
        "content_plan": context.get("content_plan"),
        "rules_version": context.get("rules_version"),
        "active_rules": context.get("active_rules", []),
        "synthetic": True,
    }


def _write_case_evidence(
    root: pathlib.Path,
    cases: dict[str, dict[str, Any]],
) -> None:
    expected = expected_cases()
    inputs = _case_fixture_map()
    for ordinal in range(1, 16):
        case_id = f"B{ordinal:02d}"
        case = cases[case_id]
        package = case.get("package") if isinstance(case.get("package"), dict) else None
        bundle = package.get("bundle") if isinstance(package, dict) else None
        quality = package.get("quality_report") if isinstance(package, dict) else None
        destination = root / "business-cases" / case_id
        _write_json(destination / "input.json", inputs[case_id])
        _write_json(destination / "expected.json", expected[case_id])
        _write_json(destination / "context-manifest.json", _context_manifest(case))
        _write_json(destination / "package.json", package)
        _write_json(
            destination / "claims.json",
            bundle.get("claim_evidence", []) if isinstance(bundle, dict) else [],
        )
        _write_json(
            destination / "findings.json",
            quality.get("findings", []) if isinstance(quality, dict) else [],
        )
        _write_json(
            destination / "actual.json",
            {key: value for key, value in case.items() if key not in {"context", "package"}},
        )


def _write_jsonl(path: pathlib.Path, rows: Any) -> None:
    values = rows if isinstance(rows, list) else []
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n"
            for row in values
        ),
        encoding="utf-8",
    )


def _write_traces(root: pathlib.Path, cases: dict[str, dict[str, Any]]) -> None:
    for case in cases.values():
        if case.get("mode") != "live_ouroboros":
            continue
        raw_operations = case.get("operations")
        operations = (
            [item for item in raw_operations if isinstance(item, dict)]
            if isinstance(raw_operations, list) and raw_operations
            else [case]
        )
        for operation in operations:
            run = operation.get("run")
            if not isinstance(run, dict) or not RUN_ID_PATTERN.fullmatch(
                str(run.get("run_id") or "")
            ):
                raise EvidenceError(f"{case.get('case_id')} live run identity is unsafe")
            destination = root / "traces" / str(run["run_id"])
            if destination.exists():
                raise EvidenceError("live operation run id is duplicated across the basket")
            destination.mkdir(parents=True)
            _write_json(destination / "task.json", operation.get("task") or {})
            _write_jsonl(destination / "safe-events.jsonl", operation.get("safe_events"))
            _write_jsonl(destination / "mcp-calls.jsonl", operation.get("mcp_calls"))
            _write_json(
                destination / "model-usage.json",
                {
                    "provider_call_ledger": operation.get("provider_call_ledger", {}),
                    "usage_by_category": operation.get("usage_by_category", {}),
                    "latency_ms": operation.get("latency_ms", {}),
                },
            )


def _demo_mapping(learning: dict[str, Any]) -> dict[str, Any]:
    required = {
        "clarification": "clarification.json",
        "package_v1": "package-v1.json",
        "feedback": "feedback.json",
        "package_v2": "package-v2.json",
        "diff": "diff.json",
        "rule_proposal": "rule-proposal.json",
        "rule_tests": "rule-tests.json",
        "rule_approval": "rule-approval.json",
        "second_case_application": "second-case-application.json",
        "package_approval": "package-approval.json",
    }
    missing = [key for key in required if key not in learning]
    if missing:
        raise EvidenceError("live learning evidence is incomplete")
    rule_approval = learning.get("rule_approval")
    package_approval = learning.get("package_approval")
    if not isinstance(rule_approval, dict) or rule_approval.get("test_only") is not True:
        raise EvidenceError("implementation rule approval must be explicitly test_only")
    if not isinstance(package_approval, dict) or package_approval.get("test_only") is not True:
        raise EvidenceError("implementation package approval must be explicitly test_only")
    return {filename: learning[key] for key, filename in required.items()}


def _submission_approval_targets(learning: dict[str, Any]) -> dict[str, dict[str, str]]:
    raw_rule = learning.get("rule_approval")
    raw_package = learning.get("package_approval")
    rule = raw_rule if isinstance(raw_rule, dict) else {}
    package = raw_package if isinstance(raw_package, dict) else {}
    raw_rule_payload = rule.get("rule")
    rule_payload = raw_rule_payload if isinstance(raw_rule_payload, dict) else {}
    targets = {
        "rule": {
            "rule_version_id": str(rule.get("rule_version_id") or ""),
            "artifact_hash": str(rule_payload.get("candidate_rules_version") or ""),
        },
        "package": {
            "package_id": str(package.get("package_id") or ""),
            "artifact_hash": str(package.get("package_hash") or ""),
        },
    }
    if any(
        not RUN_ID_PATTERN.fullmatch(targets[label][identity_field])
        or not SHA256_PATTERN.fullmatch(targets[label]["artifact_hash"])
        for label, identity_field in (
            ("rule", "rule_version_id"),
            ("package", "package_id"),
        )
    ):
        raise EvidenceError("submission approval targets are incomplete")
    return targets


def _copy_demo_evidence(
    root: pathlib.Path,
    source_root: pathlib.Path,
    report: dict[str, Any],
) -> None:
    raw_learning = report.get("learning")
    if not isinstance(raw_learning, dict):
        raise EvidenceError("live learning evidence is missing")
    destination = root / "demo-case"
    destination.mkdir(parents=True)
    for filename, value in _demo_mapping(raw_learning).items():
        _write_json(destination / filename, value)
    source_export = source_root / "demo-case" / "campaign-export.zip"
    if not source_export.is_file() or not zipfile.is_zipfile(source_export):
        raise EvidenceError("live demo campaign export is missing or invalid")
    shutil.copyfile(source_export, destination / "campaign-export.zip")


def _safe_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    if not normalized:
        raise EvidenceError("browser artifact name is unsafe")
    return normalized[:180]


def _sanitize_trace_archive(source: pathlib.Path, destination: pathlib.Path) -> int:
    redacted = 0
    total_size = 0
    try:
        with (
            zipfile.ZipFile(source) as input_archive,
            zipfile.ZipFile(
                destination,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as output_archive,
        ):
            members = input_archive.infolist()
            if len(members) > 5_000:
                raise EvidenceError("Playwright trace has too many members")
            for member in members:
                candidate = pathlib.PurePosixPath(member.filename)
                mode = member.external_attr >> 16
                total_size += member.file_size
                if (
                    candidate.is_absolute()
                    or ".." in candidate.parts
                    or "\\" in member.filename
                    or stat.S_ISLNK(mode)
                    or total_size > 100_000_000
                    or member.file_size > 20_000_000
                ):
                    raise EvidenceError("Playwright trace contains an unsafe archive member")
                data = input_archive.read(member)
                data, replacements = BASIC_AUTH_BYTES.subn(b"Basic [REDACTED]", data)
                redacted += replacements
                output_archive.writestr(member, data)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise EvidenceError("Playwright trace could not be safely sanitized") from exc
    if not zipfile.is_zipfile(destination):
        raise EvidenceError("sanitized Playwright trace is not a ZIP")
    return redacted


def _copy_browser_evidence(root: pathlib.Path, browser_root: pathlib.Path) -> dict[str, int]:
    if not browser_root.is_dir():
        raise EvidenceError("Playwright evidence directory is missing")
    screenshots = [
        path
        for path in sorted(browser_root.rglob("*.png"))
        if "golden-flow" in path.parent.name and path.is_file() and not path.is_symlink()
    ]
    traces = [
        path
        for path in sorted(browser_root.rglob("trace.zip"))
        if "golden-flow" in path.parent.name and path.is_file() and not path.is_symlink()
    ]
    if len(screenshots) < 5 or len(traces) < 5:
        raise EvidenceError("five golden Playwright screenshots and traces are required")
    screenshot_root = root / "screenshots"
    trace_root = root / "playwright-traces"
    screenshot_root.mkdir(parents=True)
    trace_root.mkdir(parents=True)
    redacted_headers = 0
    for index, source in enumerate(screenshots, start=1):
        if not source.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
            raise EvidenceError("Playwright screenshot is not a PNG")
        shutil.copyfile(source, screenshot_root / f"golden-{index:02d}-{_safe_name(source.name)}")
    for index, source in enumerate(traces, start=1):
        if not zipfile.is_zipfile(source):
            raise EvidenceError("Playwright trace is not a ZIP")
        redacted_headers += _sanitize_trace_archive(
            source,
            trace_root / f"golden-{index:02d}-trace.zip",
        )
    return {
        "golden_screenshots": len(screenshots),
        "golden_traces": len(traces),
        "auth_headers_redacted": redacted_headers,
    }


def _review_packet(case: dict[str, Any]) -> dict[str, Any]:
    package = case.get("package")
    run = case.get("run")
    if not isinstance(package, dict) or not isinstance(run, dict):
        raise EvidenceError("review packet source is incomplete")
    package_hash = str(package.get("package_hash") or "")
    if not SHA256_PATTERN.fullmatch(package_hash):
        raise EvidenceError("review packet package hash is invalid")
    packet_id = f"review_{case['case_id']}_{package_hash[:12]}"
    return {
        "schema_version": 1,
        "packet_id": packet_id,
        "case_id": case["case_id"],
        "source_run_id": run.get("run_id"),
        "source_execution_mode": "live_ouroboros",
        "package_hash": package_hash,
        "brief": case.get("input"),
        "context": _context_manifest(case),
        "package": package,
        "rubric": [{"rubric_id": key, "label": label, "range": "1-5"} for key, label in RUBRIC],
        "review_status": "WAITING_FOR_OPERATOR",
        "review_form": {
            "reviewer_role": None,
            "reviewer_id": None,
            "completed_at": None,
            "scores": {key: None for key, _ in RUBRIC},
            "comments": None,
        },
        "manual_measured": False,
        "synthetic": True,
        "no_send": True,
    }


def _packet_html(packet: dict[str, Any]) -> str:
    package = packet["package"]
    bundle = package.get("bundle") if isinstance(package, dict) else {}
    sms = bundle.get("sms") if isinstance(bundle, dict) else None
    email = bundle.get("email") if isinstance(bundle, dict) else None
    sms_text = sms.get("text") if isinstance(sms, dict) else "SUPPRESSED"
    email_text = email.get("plain_text") if isinstance(email, dict) else "SUPPRESSED"
    packet_id = html.escape(str(packet["packet_id"]))
    case_id = html.escape(str(packet["case_id"]))
    run_id = html.escape(str(packet["source_run_id"]))
    rubric_rows = "".join(
        f"<tr><td>{html.escape(label)}</td><td class='blank'>___ / 5</td></tr>"
        for _, label in RUBRIC
    )
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>{packet_id}</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;max-width:980px;margin:32px auto;padding:0 24px}}
h1,h2{{color:#174f3b}}
.notice{{padding:12px;background:#edf7f1;border-left:4px solid #2c7a58}}
pre{{white-space:pre-wrap;background:#f5f7f5;padding:16px;border-radius:8px}}
table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccd8d0;padding:8px;text-align:left}}
.blank{{min-width:140px}}
</style></head>
<body><h1>Пакет качественного review: {case_id}</h1>
<p class="notice">Все данные синтетические · отправка отключена · оценки ожидают человека.</p>
<p>Packet ID: <code>{packet_id}</code><br>Run ID: <code>{run_id}</code></p>
<h2>SMS</h2><pre>{html.escape(str(sms_text))}</pre>
<h2>E-mail</h2><pre>{html.escape(str(email_text))}</pre>
<h2>Рубрика</h2>
<table><thead><tr><th>Критерий</th><th>Ручная оценка</th></tr></thead>
<tbody>{rubric_rows}</tbody></table>
<h2>Комментарии</h2><p>________________________________________________________________________</p>
<p>Reviewer role/name or ID: ____________________</p>
<p>Completed at: ____________________</p></body></html>"""


def _write_review_packets(
    root: pathlib.Path,
    cases: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    review_root = root / "review-packets"
    review_root.mkdir(parents=True)
    packets: list[dict[str, Any]] = []
    for case_id in review_packet_case_ids():
        packet = _review_packet(cases[case_id])
        destination = review_root / str(packet["packet_id"])
        destination.mkdir()
        _write_json(destination / "packet.json", packet)
        packet_path = destination / "packet.html"
        packet_path.write_text(_packet_html(packet), encoding="utf-8")
        _write_json(destination / "review-form.json", packet["review_form"])
        packets.append(
            {
                "packet_id": packet["packet_id"],
                "case_id": case_id,
                "package_hash": packet["package_hash"],
                "packet_ref": f"review-packets/{packet['packet_id']}/packet.html",
                "packet_sha256": _sha256_file(packet_path),
                "form_ref": f"review-packets/{packet['packet_id']}/review-form.json",
                "status": "WAITING_FOR_OPERATOR",
            }
        )
    schema = {
        "title": "Communication Factory human qualitative review form",
        "type": "object",
        "additionalProperties": False,
        "required": ["reviewer_role", "reviewer_id", "completed_at", "scores", "comments"],
        "properties": {
            "reviewer_role": {"type": "string", "minLength": 1},
            "reviewer_id": {"type": "string", "minLength": 1},
            "completed_at": {"type": "string", "format": "date-time"},
            "scores": {
                "type": "object",
                "additionalProperties": False,
                "required": [key for key, _ in RUBRIC],
                "properties": {
                    key: {"type": "integer", "minimum": 1, "maximum": 5} for key, _ in RUBRIC
                },
            },
            "comments": {"type": "string", "minLength": 1},
        },
    }
    _write_json(review_root / "review-form.schema.json", schema)
    (review_root / "REVIEW_INSTRUCTIONS.md").write_text(
        "# Human qualitative review\n\n"
        "Полностью прочитайте SMS и e-mail каждого из шести immutable packets. "
        "Заполните reviewer identity, timestamp, все оценки 1–5 и содержательный комментарий. "
        "Не изменяйте packet.json; сохраните заполненную форму как новый human record. "
        "Test actor или LLM не заменяет человека.\n",
        encoding="utf-8",
    )
    qualitative = {
        "schema_version": 1,
        "status": "WAITING_FOR_OPERATOR",
        "manual_measured": False,
        "packet_count": 6,
        "preselected_case_ids": list(review_packet_case_ids()),
        "rubric": [{"rubric_id": key, "label": label, "range": "1-5"} for key, label in RUBRIC],
        "packets": packets,
        "aggregate": None,
        "reviewer_records": [],
    }
    _write_json(root / "qualitative-review.json", qualitative)
    return qualitative


def _report_html(
    report: dict[str, Any],
    metrics: dict[str, Any],
    rows: list[dict[str, Any]],
) -> str:
    latency = metrics["normal_live_latency_ms"]["user_visible_terminal"]
    usage = metrics["provider_usage"]["totals"]
    evaluation_id = html.escape(str(report["evaluation_id"]))
    live_count = metrics["business"]["live_case_count"]
    token_count = usage["prompt_tokens"] + usage["completion_tokens"]
    case_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(row['case_id']))}</td>"
        f"<td>{html.escape(str(row['mode']))}</td>"
        f"<td>{'PASS' if row['passed'] else 'FAIL'}</td>"
        f"<td>{html.escape(str(row['actual_terminal']))}</td>"
        f"<td>{html.escape(str(row['user_visible_terminal_ms'] or '—'))}</td>"
        "</tr>"
        for row in rows
    )
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Communication Factory — Implementation evidence</title>
<style>
@page{{size:A4;margin:14mm}}
body{{font:14px/1.45 system-ui,sans-serif;color:#173026;margin:0}}
main{{max-width:1080px;margin:28px auto;padding:0 28px}}
h1,h2{{color:#174f3b}}
.hero{{background:#edf7f1;padding:24px;border-radius:12px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
.metric{{border:1px solid #cfddd4;border-radius:8px;padding:12px}}
.value{{font-size:24px;font-weight:700}}
table{{border-collapse:collapse;width:100%;margin-top:12px}}
td,th{{border-bottom:1px solid #d8e2dc;padding:7px;text-align:left}}
.pending{{color:#8a4b08;background:#fff5df;padding:12px;border-left:4px solid #d38b19}}
a{{color:#17633f}}
</style></head>
<body><main><section class="hero"><h1>Фабрика коммуникаций — Implementation evidence</h1>
<p>Evaluation <code>{evaluation_id}</code> · frozen live Ouroboros basket.</p>
<p><strong>Все данные синтетические · отправка отключена.</strong></p></section>
<h2>Измеренные результаты</h2><div class="grid">
<div class="metric"><div class="value">15/15</div><div>business cases</div></div>
<div class="metric"><div class="value">{live_count}</div><div>live Ouroboros</div></div>
<div class="metric"><div class="value">{latency["p95"]} ms</div><div>normal live p95</div></div>
<div class="metric"><div class="value">{token_count}</div><div>provider tokens</div></div>
</div>
<p class="pending"><strong>WAITING_FOR_OPERATOR:</strong> шесть human qualitative reviews
не заполнены и не подменены synthetic values.</p>
<h2>Business basket</h2><table><thead><tr><th>Case</th><th>Mode</th><th>Result</th>
<th>Terminal</th><th>User ms</th></tr></thead><tbody>{case_rows}</tbody></table>
<h2>Интерпретация</h2><ul><li><strong>Measured:</strong> фактические
prototype/evaluation данные в этом frozen run.</li>
<li><strong>Assumed/Baseline:</strong> внешние AS-IS оценки не считаются достигнутым эффектом.</li>
<li><strong>Hypothesis:</strong> эффект будущего пилота требует отдельного измерения.</li></ul>
<h2>Offline artifacts</h2><p><a href="business-results.csv">CSV</a> ·
<a href="business-results.jsonl">JSONL</a> · <a href="metrics.json">metrics JSON</a> ·
<a href="qualitative-review.json">review status</a></p>
</main></body></html>"""


def render_with_playwright(
    html_path: pathlib.Path,
    pdf_path: pathlib.Path,
    jpg_path: pathlib.Path,
) -> None:
    url = html_path.resolve().as_uri()
    commands = (
        [
            "npx",
            "playwright",
            "pdf",
            "--paper-format",
            "A4",
            "--wait-for-selector",
            "main",
            "--timeout",
            "30000",
            url,
            str(pdf_path),
        ],
        [
            "npx",
            "playwright",
            "screenshot",
            "--full-page",
            "--viewport-size",
            "1600,1200",
            "--wait-for-selector",
            "main",
            "--timeout",
            "30000",
            url,
            str(jpg_path),
        ],
    )
    for command in commands:
        process = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if process.returncode != 0:
            raise EvidenceError("Playwright could not render an offline evidence format")


def _validate_rendered_formats(root: pathlib.Path) -> None:
    if not (root / "report.pdf").read_bytes().startswith(b"%PDF-"):
        raise EvidenceError("report.pdf is not a PDF")
    jpg = (root / "report.jpg").read_bytes()
    if not jpg.startswith(b"\xff\xd8") or not jpg.endswith(b"\xff\xd9"):
        raise EvidenceError("report.jpg is not a JPEG")
    report_html = (root / "report.html").read_text(encoding="utf-8")
    if "<main" not in report_html or re.search(r"(?:src|href)=[\"']https?://", report_html):
        raise EvidenceError("report.html is not self-contained for offline use")


def _contract_projection(contract: dict[str, Any]) -> dict[str, Any]:
    raw_runtime = contract.get("runtime")
    raw_skill = contract.get("skill")
    raw_tools = contract.get("tools")
    raw_mcp = contract.get("mcp")
    runtime: dict[str, Any] = dict(raw_runtime) if isinstance(raw_runtime, dict) else {}
    skill: dict[str, Any] = dict(raw_skill) if isinstance(raw_skill, dict) else {}
    tools: dict[str, Any] = dict(raw_tools) if isinstance(raw_tools, dict) else {}
    mcp: dict[str, Any] = dict(raw_mcp) if isinstance(raw_mcp, dict) else {}
    return {
        "ouroboros_tag": runtime.get("tag"),
        "ouroboros_commit": runtime.get("commit"),
        "runtime_image_id": runtime.get("image_id"),
        "activation_mode": skill.get("activation_mode"),
        "skill_content_hash": skill.get("skill_content_hash"),
        "prompt_hash": skill.get("prompt_hash"),
        "tool_inventory_hash": tools.get("inventory_hash"),
        "post_deny_schema_hash": tools.get("post_deny_schema_hash"),
        "provider_tool_names": tools.get("post_deny_tool_names"),
        "mcp_settings_hash": mcp.get("settings_hash"),
    }


def _manifest(
    *,
    report: dict[str, Any],
    source_root: pathlib.Path,
    metrics: dict[str, Any],
    qualitative: dict[str, Any],
    browser_counts: dict[str, int],
    commit: str,
    contract: dict[str, Any],
    built_at: datetime,
) -> dict[str, Any]:
    cases = _case_map(report)
    runs = []
    for case_id, case in sorted(cases.items()):
        raw_run = case.get("run")
        raw_package = case.get("package")
        run: dict[str, Any] = dict(raw_run) if isinstance(raw_run, dict) else {}
        package: dict[str, Any] = dict(raw_package) if isinstance(raw_package, dict) else {}
        runs.append(
            {
                "case_id": case_id,
                "mode": case.get("mode"),
                "run_id": run.get("run_id"),
                "package_hash": package.get("package_hash"),
            }
        )
    return {
        "schema_version": 1,
        "evidence_kind": "implementation",
        "evaluation_id": report["evaluation_id"],
        "created_at": built_at.isoformat(),
        "app_commit": commit,
        "git_clean": True,
        "source_live_report_sha256": _sha256_file(source_root / "report.json"),
        "source_live_checksums_sha256": _sha256_file(source_root / "checksums.sha256"),
        "source_execution_kind": "live_evaluation",
        "frozen": True,
        "runtime_contract": _contract_projection(contract),
        "basket_hash": report.get("basket_hash"),
        "policy_tree_hash": _tree_hash(POLICY_ROOT),
        "rules_hash": report.get("rules_hash"),
        "business_runs": runs,
        "metrics_status": "PASS" if metrics["business"]["passed_count"] == 15 else "FAIL",
        "scan_statuses": {"live_evaluation": "PASS", "chaos": "PASS", "security": "PASS"},
        "browser_evidence": browser_counts,
        "synthetic": True,
        "no_send": True,
        "human_gate_status": qualitative["status"],
        "human_packet_count": qualitative["packet_count"],
        "submission_approval_targets": _submission_approval_targets(report["learning"]),
        "primary_attempt": report.get("primary_attempt"),
        "repeats": report.get("repeats", []),
        "exclusions": report.get("exclusions", []),
        "account_remaining": "unknown",
    }


def _final_directory_name(built_at: datetime, commit: str) -> str:
    return f"{built_at.strftime('%Y%m%dT%H%M%SZ')}_{commit[:12]}"


def build_evidence(
    *,
    source_root: pathlib.Path,
    chaos_path: pathlib.Path = CHAOS_PATH,
    security_path: pathlib.Path = SECURITY_PATH,
    browser_root: pathlib.Path = BROWSER_RESULTS_ROOT,
    output_root: pathlib.Path = EVIDENCE_ROOT,
    renderer: Renderer = render_with_playwright,
    require_clean_git: bool = True,
    built_at: datetime | None = None,
) -> pathlib.Path:
    report = _load_object(source_root / "report.json", "live evaluation report")
    cases = validate_live_report(report)
    _validate_frozen_source(source_root, report)
    chaos = _load_object(chaos_path, "chaos report")
    chaos_cases = _validate_chaos(chaos)
    security = _load_object(security_path, "security report")
    _validate_security(security)
    commit, clean = _current_git_identity()
    if require_clean_git and not clean:
        raise EvidenceError("implementation evidence requires a clean Git worktree")
    if report.get("app_commit") != commit or report.get("git_dirty") is not False:
        raise EvidenceError("live evaluation is not bound to the current clean commit")
    contract = _load_object(CONTRACT_LOCK_PATH, "runtime contract lock")
    if report.get("runtime_contract_hash") != _sha256_file(CONTRACT_LOCK_PATH):
        raise EvidenceError("live evaluation runtime contract hash does not match")
    timestamp = built_at or datetime.now(UTC).replace(microsecond=0)
    destination = output_root / _final_directory_name(timestamp, commit)
    if destination.exists():
        raise EvidenceError("immutable evidence directory already exists")
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = pathlib.Path(tempfile.mkdtemp(prefix=".evidence-", dir=output_root))
    try:
        metrics = _metrics(report, cases, chaos_cases)
        rows = _business_rows(cases)
        _write_json(temporary / "metrics.json", metrics)
        _write_business_formats(temporary, rows)
        _write_json(temporary / "security-report.json", security)
        stability = {
            "schema_version": 1,
            "normal_live": report["stability"],
            "normal_live_latency_ms": metrics["normal_live_latency_ms"],
            "chaos_isolated": chaos,
        }
        _write_json(temporary / "stability-report.json", stability)
        _write_case_evidence(temporary, cases)
        _write_traces(temporary, cases)
        _copy_demo_evidence(temporary, source_root, report)
        for case_id in review_packet_case_ids():
            case = cases[case_id]
            if "input" not in case:
                case["input"] = _case_fixture_map()[case_id]
        qualitative = _write_review_packets(temporary, cases)
        browser_counts = _copy_browser_evidence(temporary, browser_root)
        report_html = _report_html(report, metrics, rows)
        (temporary / "report.html").write_text(report_html, encoding="utf-8")
        renderer(temporary / "report.html", temporary / "report.pdf", temporary / "report.jpg")
        _validate_rendered_formats(temporary)
        manifest = _manifest(
            report=report,
            source_root=source_root,
            metrics=metrics,
            qualitative=qualitative,
            browser_counts=browser_counts,
            commit=commit,
            contract=contract,
            built_at=timestamp,
        )
        _write_json(temporary / "manifest.json", manifest)
        findings = scan_generated_artifacts([temporary])
        if findings:
            raise EvidenceError("generated evidence failed the secret/PII/internal scan")
        _write_checksums(temporary)
        immutable = {
            "schema_version": 1,
            "status": "IMMUTABLE",
            "evaluation_id": report["evaluation_id"],
            "manifest_sha256": _sha256_file(temporary / "manifest.json"),
            "checksums_sha256": _sha256_file(temporary / "checksums.sha256"),
            "finalized_at": timestamp.isoformat(),
        }
        _write_json(temporary / "IMMUTABLE.json", immutable)
        validate_evidence_directory(temporary)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def _validate_review_packets(root: pathlib.Path) -> None:
    qualitative = _load_object(root / "qualitative-review.json", "qualitative review")
    packets = qualitative.get("packets")
    expected_case_ids = list(review_packet_case_ids())
    if (
        qualitative.get("status") != "WAITING_FOR_OPERATOR"
        or qualitative.get("manual_measured") is not False
        or qualitative.get("packet_count") != 6
        or qualitative.get("preselected_case_ids") != expected_case_ids
        or qualitative.get("aggregate") is not None
        or qualitative.get("reviewer_records") != []
        or not isinstance(packets, list)
        or len(packets) != 6
        or [row.get("case_id") for row in packets if isinstance(row, dict)] != expected_case_ids
    ):
        raise EvidenceError("qualitative review gate is malformed or fabricated")
    for row in packets:
        if not isinstance(row, dict) or row.get("status") != "WAITING_FOR_OPERATOR":
            raise EvidenceError("qualitative packet status is invalid")
        packet_ref = row.get("packet_ref")
        packet_sha256 = row.get("packet_sha256")
        form_ref = row.get("form_ref")
        if (
            not isinstance(packet_ref, str)
            or not SHA256_PATTERN.fullmatch(str(packet_sha256 or ""))
            or not isinstance(form_ref, str)
        ):
            raise EvidenceError("qualitative packet references are invalid")
        try:
            packet_path = (root / pathlib.PurePosixPath(packet_ref)).resolve()
            form_path = (root / pathlib.PurePosixPath(form_ref)).resolve()
            packet_path.relative_to(root.resolve())
            form_path.relative_to(root.resolve())
        except ValueError as exc:
            raise EvidenceError("qualitative packet reference escapes evidence root") from exc
        packet_json_path = packet_path.with_name("packet.json")
        if (
            not packet_path.is_file()
            or _sha256_file(packet_path) != packet_sha256
            or not packet_json_path.is_file()
            or not form_path.is_file()
        ):
            raise EvidenceError("qualitative packet artifact is missing")
        packet = _load_object(packet_json_path, "qualitative packet")
        if (
            packet.get("packet_id") != row.get("packet_id")
            or packet.get("case_id") != row.get("case_id")
            or packet.get("package_hash") != row.get("package_hash")
            or packet.get("source_execution_mode") != "live_ouroboros"
            or packet.get("review_status") != "WAITING_FOR_OPERATOR"
            or packet.get("manual_measured") is not False
        ):
            raise EvidenceError("qualitative packet index differs from packet content")
        form = _load_object(form_path, "qualitative review form")
        if (
            form.get("reviewer_role") is not None
            or form.get("reviewer_id") is not None
            or form.get("completed_at") is not None
            or form.get("comments") is not None
            or not isinstance(form.get("scores"), dict)
            or any(value is not None for value in form["scores"].values())
        ):
            raise EvidenceError("implementation review form contains fabricated human values")


def validate_evidence_directory(root: pathlib.Path) -> dict[str, Any]:
    if not root.is_dir():
        raise EvidenceError("evidence directory is missing")
    missing = [name for name in REQUIRED_TOP_LEVEL_FILES if not (root / name).is_file()]
    if missing:
        raise EvidenceError("evidence directory is missing required top-level files")
    validate_checksums(root)
    marker = _load_object(root / "IMMUTABLE.json", "evidence immutable marker")
    manifest = _load_object(root / "manifest.json", "evidence manifest")
    rule_approval = _load_object(root / "demo-case" / "rule-approval.json", "rule approval")
    package_approval = _load_object(
        root / "demo-case" / "package-approval.json", "package approval"
    )
    if (
        marker.get("status") != "IMMUTABLE"
        or marker.get("manifest_sha256") != _sha256_file(root / "manifest.json")
        or marker.get("checksums_sha256") != _sha256_file(root / "checksums.sha256")
        or manifest.get("frozen") is not True
        or manifest.get("evidence_kind") != "implementation"
        or manifest.get("human_gate_status") != "WAITING_FOR_OPERATOR"
        or manifest.get("synthetic") is not True
        or manifest.get("no_send") is not True
    ):
        raise EvidenceError("evidence immutable manifest is invalid")
    expected_targets = _submission_approval_targets(
        {"rule_approval": rule_approval, "package_approval": package_approval}
    )
    if manifest.get("submission_approval_targets") != expected_targets:
        raise EvidenceError("evidence submission approval targets do not match demo artifacts")
    _validate_rendered_formats(root)
    with (root / "business-results.csv").open(encoding="utf-8", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    jsonl_rows = [
        json.loads(line)
        for line in (root / "business-results.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    expected_ids = [f"B{ordinal:02d}" for ordinal in range(1, 16)]
    if [row.get("case_id") for row in csv_rows] != expected_ids or [
        row.get("case_id") for row in jsonl_rows
    ] != expected_ids:
        raise EvidenceError("offline business result formats do not contain exact B01-B15")
    for case_id in expected_ids:
        case_root = root / "business-cases" / case_id
        required = {
            "input.json",
            "expected.json",
            "context-manifest.json",
            "package.json",
            "claims.json",
            "findings.json",
            "actual.json",
        }
        if any(not (case_root / name).is_file() for name in required):
            raise EvidenceError(f"{case_id} evidence directory is incomplete")
    _validate_review_packets(root)
    if (
        len(list((root / "screenshots").glob("*.png"))) < 5
        or len(list((root / "playwright-traces").glob("*.zip"))) < 5
    ):
        raise EvidenceError("golden browser evidence is incomplete")
    findings = scan_generated_artifacts([root])
    if findings:
        raise EvidenceError("final evidence failed the secret/PII/internal scan")
    return manifest


def _resolve_source(evaluation_id: str) -> pathlib.Path:
    if not RUN_ID_PATTERN.fullmatch(evaluation_id):
        raise EvidenceError("EVALUATION_ID is invalid")
    return LIVE_ROOT / evaluation_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build immutable Communication Factory evidence")
    parser.add_argument("--validate", type=pathlib.Path)
    args = parser.parse_args(argv)
    try:
        if args.validate is not None:
            manifest = validate_evidence_directory(args.validate.resolve())
            print(
                "evidence-validate: PASS "
                f"evaluation={manifest['evaluation_id']} human={manifest['human_gate_status']}"
            )
            return 0
        evaluation_id = str(os.environ.get("EVALUATION_ID") or "").strip()
        if not evaluation_id:
            raise EvidenceError("EVALUATION_ID is required to select existing frozen live evidence")
        destination = build_evidence(source_root=_resolve_source(evaluation_id))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"evidence: FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"evidence: PASS path={destination.relative_to(ROOT)} human=WAITING_FOR_OPERATOR")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
