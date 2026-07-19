from __future__ import annotations

import hashlib
import json
import math
import pathlib
import re
import statistics
from datetime import datetime
from typing import Any

from apps.api.app.domain.presentation import (
    EvaluationReportLink,
    MvpCaseResult,
    MvpEmailResult,
    MvpResultsMetrics,
    MvpResultsView,
    MvpSmsResult,
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CONFIRMED_LIVE_CASE_IDS = (
    "B01",
    "B02",
    "B03",
    "B04",
    "B06",
    "B07",
    "B09",
    "B10",
    "B14",
    "B15",
)
EXPECTED_CODE_BASE_COMMIT = "6010d93300ddbfa87c19edf6e6d688e19198a0ff"
MVP_ARTIFACT_FILES = {
    "report.pdf": ("Полный отчёт", "pdf", "application/pdf"),
    "report.html": ("Полный отчёт", "html", "text/html; charset=utf-8"),
    "basket03-report.json": ("Данные отчёта", "json", "application/json"),
    "summary.csv": ("Сводная таблица", "csv", "text/csv; charset=utf-8"),
}


class MvpResultsError(RuntimeError):
    pass


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: pathlib.Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MvpResultsError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise MvpResultsError(f"{label} is not an object")
    return {str(key): item for key, item in value.items()}


def _as_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MvpResultsError(f"{label} is malformed")
    return {str(key): item for key, item in value.items()}


def _checksums(root: pathlib.Path) -> dict[str, str]:
    try:
        lines = (root / "checksums.sha256").read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise MvpResultsError("MVP report checksums are unreadable") from exc
    result: dict[str, str] = {}
    for line in lines:
        if "  " not in line:
            raise MvpResultsError("MVP report checksum row is malformed")
        digest, relative = line.split("  ", 1)
        candidate = pathlib.PurePosixPath(relative)
        if (
            not SHA256_RE.fullmatch(digest)
            or not relative
            or candidate.is_absolute()
            or ".." in candidate.parts
            or relative in result
        ):
            raise MvpResultsError("MVP report checksum identity is unsafe")
        path = root / candidate
        if not path.is_file() or _sha256(path) != digest:
            raise MvpResultsError("MVP report checksum does not match")
        result[relative] = digest
    if len(result) != 20:
        raise MvpResultsError("MVP report checksum inventory is incomplete")
    return result


def _case_result(case: dict[str, Any]) -> MvpCaseResult:
    case_id = str(case.get("case_id") or "")
    execution = _as_object(case.get("execution"), f"{case_id} execution")
    outputs = _as_object(case.get("outputs"), f"{case_id} outputs")
    qa = _as_object(case.get("qa"), f"{case_id} QA")
    metrics = _as_object(case.get("metrics"), f"{case_id} metrics")
    sms_value = outputs.get("sms")
    email_value = outputs.get("email")
    sms = _as_object(sms_value, f"{case_id} SMS") if sms_value is not None else None
    email = _as_object(email_value, f"{case_id} e-mail") if email_value is not None else None
    if (
        case_id not in CONFIRMED_LIVE_CASE_IDS
        or case.get("synthetic") is not True
        or case.get("passed") is not True
        or case.get("mode") != "live_ouroboros"
        or execution.get("run_status") != "COMPLETED"
        or execution.get("actual_terminal") != "APPROVABLE"
        or qa.get("approvable") is not True
        or qa.get("deterministic_score") != 100
        or metrics.get("usage_complete") is not True
        or not (sms or email)
    ):
        raise MvpResultsError(f"{case_id} is not a confirmed successful live case")
    provider_calls = int(metrics.get("provider_calls") or 0)
    provider_tokens = int(metrics.get("total_tokens") or 0)
    latency_ms = int(metrics.get("user_visible_terminal_ms") or 0)
    if provider_calls <= 0 or provider_tokens <= 0 or latency_ms <= 0:
        raise MvpResultsError(f"{case_id} live metrics are incomplete")
    channels: list[str] = []
    sms_result = None
    if sms is not None:
        text = str(sms.get("text") or "").strip()
        sms_metrics = qa.get("sms_metrics")
        sms_metric_object = (
            _as_object(sms_metrics, f"{case_id} SMS metrics") if sms_metrics is not None else {}
        )
        if not text:
            raise MvpResultsError(f"{case_id} SMS output is empty")
        channels.append("sms")
        raw_segments = sms_metric_object.get("segments")
        sms_result = MvpSmsResult(
            text=text,
            segments=int(raw_segments) if raw_segments is not None else None,
        )
    email_result = None
    if email is not None:
        subject = str(email.get("subject") or "").strip()
        plain_text = str(email.get("plain_text") or "").strip()
        if not subject or not plain_text:
            raise MvpResultsError(f"{case_id} e-mail output is empty")
        channels.append("email")
        email_result = MvpEmailResult(subject=subject, plain_text=plain_text)
    return MvpCaseResult(
        case_id=case_id,
        title=str(case.get("title") or case_id),
        actual_terminal=str(execution["actual_terminal"]),
        qa_score=int(qa["deterministic_score"]),
        latency_ms=latency_ms,
        provider_calls=provider_calls,
        provider_tokens=provider_tokens,
        cost_usd=float(metrics.get("cost_usd") or 0),
        channels=tuple(channels),
        sms=sms_result,
        email=email_result,
    )


class MvpResultsCatalog:
    def __init__(self, root: pathlib.Path) -> None:
        self._root = root

    def view(self) -> MvpResultsView:
        report = _load_object(self._root / "basket03-report.json", "MVP report")
        checksums = _checksums(self._root)
        result = _as_object(report.get("result"), "MVP report result")
        source = _as_object(report.get("source"), "MVP report source")
        scope = _as_object(report.get("scope"), "MVP report scope")
        raw_cases = report.get("cases")
        if (
            report.get("schema_version") != 1
            or report.get("report_type") != "MVP_TESTING_REPORT"
            or report.get("evidence_status") != "FAILED_NON_EVIDENCE"
            or report.get("canonical_release_evidence") is not False
            or result.get("status") != "FAIL"
            or result.get("live_passed_cases") != 10
            or result.get("live_total_cases") != 12
            or result.get("passed_cases") != 13
            or result.get("total_cases") != 15
            or source.get("provider") != "openrouter"
            or source.get("model") != "z-ai/glm-5.2"
            or source.get("recommended_code_base_commit") != EXPECTED_CODE_BASE_COMMIT
            or scope.get("synthetic_only") is not True
            or scope.get("no_send") is not True
            or not isinstance(raw_cases, list)
        ):
            raise MvpResultsError("MVP report identity is invalid")
        case_objects = [
            _as_object(item, "MVP report case") for item in raw_cases if isinstance(item, dict)
        ]
        selected = [
            item
            for item in case_objects
            if item.get("passed") is True and item.get("mode") == "live_ouroboros"
        ]
        selected_ids = tuple(str(item.get("case_id") or "") for item in selected)
        if selected_ids != CONFIRMED_LIVE_CASE_IDS:
            raise MvpResultsError("confirmed MVP case selection changed")
        for item in selected:
            case_id = str(item["case_id"])
            individual = _load_object(self._root / "cases" / f"{case_id}.json", case_id)
            if individual != item or f"cases/{case_id}.json" not in checksums:
                raise MvpResultsError(f"{case_id} report projection does not match")
        cases = tuple(_case_result(item) for item in selected)
        latencies = [item.latency_ms for item in cases]
        p95_index = math.ceil(0.95 * len(latencies)) - 1
        generated_at = datetime.fromisoformat(str(source.get("generated_at")))
        links = tuple(
            EvaluationReportLink(
                label=label,
                format=format_name,
                href=f"/api/v1/results/mvp/artifacts/{filename}",
                checksum=checksums[filename],
            )
            for filename, (label, format_name, _) in MVP_ARTIFACT_FILES.items()
        )
        return MvpResultsView(
            results_id="basket03_mvp_testing",
            status="MVP_CONFIRMED_NON_RELEASE",
            generated_at=generated_at,
            cases=cases,
            metrics=MvpResultsMetrics(
                confirmed_live_case_count=len(cases),
                basket_live_case_count=int(result["live_total_cases"]),
                full_basket_passed_count=int(result["passed_cases"]),
                full_basket_case_count=int(result["total_cases"]),
                p50_latency_ms=int(statistics.median(latencies)),
                p95_latency_ms=sorted(latencies)[p95_index],
                max_latency_ms=max(latencies),
                provider_calls=sum(item.provider_calls for item in cases),
                provider_tokens=sum(item.provider_tokens for item in cases),
                provider_cost_usd=sum(item.cost_usd for item in cases),
            ),
            report_links=links,
        )

    def artifact(self, filename: str) -> tuple[pathlib.Path, str]:
        if filename not in MVP_ARTIFACT_FILES:
            raise MvpResultsError("MVP report artifact is not allowlisted")
        self.view()
        return self._root / filename, MVP_ARTIFACT_FILES[filename][2]
