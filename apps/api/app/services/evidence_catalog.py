from __future__ import annotations

import csv
import hashlib
import json
import pathlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from apps.api.app.domain.presentation import (
    DashboardCaseView,
    DashboardMetrics,
    EvaluationReportLink,
    EvaluationRunSummary,
    EvaluationRunView,
)
from apps.api.app.domain.workflow import CaseView
from apps.api.app.services.catalog import SyntheticCatalog

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EVALUATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ARTIFACT_FILES = {
    "report.pdf": ("PDF отчёт", "pdf", "application/pdf"),
    "report.jpg": ("JPG отчёт", "jpg", "image/jpeg"),
    "report.html": ("HTML отчёт", "html", "text/html; charset=utf-8"),
    "metrics.json": ("Метрики JSON", "json", "application/json"),
    "business-results.csv": ("Business cases CSV", "csv", "text/csv; charset=utf-8"),
}


class EvidenceCatalogError(RuntimeError):
    pass


@dataclass(frozen=True)
class FrozenEvidence:
    root: pathlib.Path
    evaluation_id: str
    manifest: dict[str, Any]
    metrics: dict[str, Any]
    business_rows: tuple[dict[str, Any], ...]
    chaos_rows: tuple[dict[str, Any], ...]
    checksums: dict[str, str]


def _load_object(path: pathlib.Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceCatalogError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise EvidenceCatalogError(f"{label} is not an object")
    return {str(key): item for key, item in value.items()}


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _checksums(root: pathlib.Path) -> dict[str, str]:
    try:
        lines = (root / "checksums.sha256").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EvidenceCatalogError("evidence checksums are unreadable") from exc
    result: dict[str, str] = {}
    for line in lines:
        if "  " not in line:
            raise EvidenceCatalogError("evidence checksum row is malformed")
        digest, relative = line.split("  ", 1)
        candidate = pathlib.PurePosixPath(relative)
        if (
            not SHA256_RE.fullmatch(digest)
            or not relative
            or candidate.is_absolute()
            or ".." in candidate.parts
            or relative in result
        ):
            raise EvidenceCatalogError("evidence checksum identity is unsafe")
        path = root / candidate
        if not path.is_file() or _sha256(path) != digest:
            raise EvidenceCatalogError("evidence checksum does not match")
        result[relative] = digest
    if not result:
        raise EvidenceCatalogError("evidence checksum inventory is empty")
    return result


def load_frozen_evidence(root: pathlib.Path) -> FrozenEvidence:
    manifest = _load_object(root / "manifest.json", "evidence manifest")
    marker = _load_object(root / "IMMUTABLE.json", "evidence immutable marker")
    metrics = _load_object(root / "metrics.json", "evidence metrics")
    checksums = _checksums(root)
    evaluation_id = str(manifest.get("evaluation_id") or "")
    if (
        not EVALUATION_ID_RE.fullmatch(evaluation_id)
        or manifest.get("evidence_kind") != "implementation"
        or manifest.get("frozen") is not True
        or manifest.get("synthetic") is not True
        or manifest.get("no_send") is not True
        or manifest.get("metrics_status") != "PASS"
        or marker.get("status") != "IMMUTABLE"
        or marker.get("evaluation_id") != evaluation_id
        or marker.get("manifest_sha256") != _sha256(root / "manifest.json")
        or marker.get("checksums_sha256") != _sha256(root / "checksums.sha256")
        or metrics.get("evaluation_id") != evaluation_id
        or metrics.get("synthetic") is not True
        or metrics.get("no_send") is not True
    ):
        raise EvidenceCatalogError("evidence identity/immutability contract is invalid")
    try:
        with (root / "business-results.csv").open(encoding="utf-8", newline="") as handle:
            business_rows = tuple(dict(row) for row in csv.DictReader(handle))
    except OSError as exc:
        raise EvidenceCatalogError("business result table is unreadable") from exc
    if [row.get("case_id") for row in business_rows] != [
        f"B{ordinal:02d}" for ordinal in range(1, 16)
    ]:
        raise EvidenceCatalogError("business result table is not exact B01-B15")
    stability = _load_object(root / "stability-report.json", "stability report")
    raw_chaos = stability.get("chaos_isolated")
    raw_cases = raw_chaos.get("cases") if isinstance(raw_chaos, dict) else None
    chaos_rows = (
        tuple(dict(row) for row in raw_cases if isinstance(row, dict))
        if isinstance(raw_cases, list)
        else ()
    )
    if {row.get("case_id") for row in chaos_rows} != {f"X{ordinal:02d}" for ordinal in range(1, 6)}:
        raise EvidenceCatalogError("chaos result table is not exact X01-X05")
    for filename in ARTIFACT_FILES:
        if filename not in checksums:
            raise EvidenceCatalogError("allowlisted report artifact is absent from checksums")
    return FrozenEvidence(
        root=root,
        evaluation_id=evaluation_id,
        manifest=manifest,
        metrics=metrics,
        business_rows=business_rows,
        chaos_rows=chaos_rows,
        checksums=checksums,
    )


class EvidenceCatalog:
    def __init__(self, root: pathlib.Path, catalog: SyntheticCatalog) -> None:
        self._root = root
        self._catalog = catalog

    def _all(self) -> list[FrozenEvidence]:
        if not self._root.is_dir():
            return []
        rows: list[FrozenEvidence] = []
        for candidate in sorted(self._root.iterdir()):
            if not candidate.is_dir():
                continue
            try:
                rows.append(load_frozen_evidence(candidate))
            except EvidenceCatalogError:
                continue
        identities = [item.evaluation_id for item in rows]
        if len(identities) != len(set(identities)):
            raise EvidenceCatalogError("frozen evidence identities are ambiguous")
        return sorted(
            rows,
            key=lambda item: str(item.manifest.get("created_at") or ""),
            reverse=True,
        )

    def summaries(self) -> list[EvaluationRunSummary]:
        return [
            EvaluationRunSummary(
                evaluation_id=item.evaluation_id,
                label=f"Frozen live evaluation · {item.evaluation_id}",
                status="FROZEN",
                frozen=True,
                generated_at=datetime.fromisoformat(str(item.manifest["created_at"])),
                observed_case_count=15,
            )
            for item in self._all()
        ]

    def run(self, evaluation_id: str) -> EvaluationRunView:
        evidence = next(
            (item for item in self._all() if item.evaluation_id == evaluation_id),
            None,
        )
        if evidence is None:
            raise EvidenceCatalogError("frozen evaluation was not found")
        business_metrics = evidence.metrics.get("business")
        latency = evidence.metrics.get("normal_live_latency_ms")
        usage = evidence.metrics.get("provider_usage")
        stability = evidence.metrics.get("stability")
        if not all(
            isinstance(value, dict) for value in (business_metrics, latency, usage, stability)
        ):
            raise EvidenceCatalogError("frozen evaluation metrics are malformed")
        assert isinstance(business_metrics, dict)
        assert isinstance(latency, dict)
        assert isinstance(usage, dict)
        assert isinstance(stability, dict)
        user_latency = latency.get("user_visible_terminal")
        totals = usage.get("totals")
        if not isinstance(user_latency, dict) or not isinstance(totals, dict):
            raise EvidenceCatalogError("frozen latency/usage totals are malformed")
        cases: list[DashboardCaseView] = []
        for row in evidence.business_rows:
            case_id = str(row["case_id"])
            source = self._catalog.case(case_id)
            latency_value = row.get("user_visible_terminal_ms")
            cases.append(
                DashboardCaseView(
                    case=CaseView(
                        case_id=case_id,
                        title=source.title,
                        expected_status=source.expected.status.value,
                    ),
                    actual_status=str(row.get("actual_terminal") or "UNKNOWN"),
                    execution_mode=str(row.get("mode") or "validation_only"),
                    last_run_status="COMPLETED"
                    if str(row.get("passed")).casefold() == "true"
                    else "FAILED",
                    latency_ms=int(str(latency_value)) if str(latency_value).isdigit() else None,
                    qa_score=100 if str(row.get("passed")).casefold() == "true" else 0,
                    blocker_count=0 if str(row.get("passed")).casefold() == "true" else 1,
                )
            )
        chaos = tuple(
            DashboardCaseView(
                case=CaseView(
                    case_id=str(row["case_id"]),
                    title=f"Изолированный chaos case {row['case_id']}",
                    expected_status="PASS",
                ),
                actual_status="PASS" if row.get("passed") is True else "FAIL",
                execution_mode="validation_only",
                latency_ms=int(row.get("duration_ms") or 0),
                qa_score=100 if row.get("passed") is True else 0,
                blocker_count=0 if row.get("passed") is True else 1,
            )
            for row in evidence.chaos_rows
        )
        mode_counts = business_metrics.get("mode_counts")
        if not isinstance(mode_counts, dict):
            raise EvidenceCatalogError("frozen mode counts are malformed")
        metrics = DashboardMetrics(
            catalog_case_count=15,
            target_business_case_count=15,
            observed_case_count=int(business_metrics.get("case_count") or 0),
            live_case_count=int(business_metrics.get("live_case_count") or 0),
            p50_latency_ms=int(user_latency.get("p50") or 0),
            p95_latency_ms=int(user_latency.get("p95") or 0),
            max_latency_ms=int(user_latency.get("max") or 0),
            crash_count=int(stability.get("crash_count") or 0),
            timeout_count=int(stability.get("timeout_over_30s_count") or 0),
            provider_tokens=int(totals.get("prompt_tokens") or 0)
            + int(totals.get("completion_tokens") or 0),
            provider_cost_usd=float(totals.get("cost_usd") or 0.0),
        )
        qualitative = evidence.metrics.get("qualitative_review")
        review_status = (
            str(qualitative.get("status"))
            if isinstance(qualitative, dict)
            else "WAITING_FOR_OPERATOR"
        )
        links = tuple(
            EvaluationReportLink(
                label=label,
                format=format_name,
                href=f"/api/v1/evaluation/artifacts/{evaluation_id}/{filename}",
                checksum=evidence.checksums[filename],
            )
            for filename, (label, format_name, _) in ARTIFACT_FILES.items()
        )
        return EvaluationRunView(
            evaluation_id=evaluation_id,
            label=f"Frozen live evaluation · {evaluation_id}",
            status="FROZEN",
            frozen=True,
            generated_at=datetime.fromisoformat(str(evidence.manifest["created_at"])),
            business_cases=tuple(cases),
            chaos_cases=chaos,
            metrics=metrics,
            mode_counts={str(key): int(value) for key, value in mode_counts.items()},
            qualitative_review_status=(
                "COMPLETE" if review_status == "COMPLETE" else "WAITING_FOR_OPERATOR"
            ),
            report_links=links,
        )

    def artifact(self, evaluation_id: str, filename: str) -> tuple[pathlib.Path, str]:
        if filename not in ARTIFACT_FILES or not EVALUATION_ID_RE.fullmatch(evaluation_id):
            raise EvidenceCatalogError("evaluation artifact is not allowlisted")
        evidence = next(
            (item for item in self._all() if item.evaluation_id == evaluation_id),
            None,
        )
        if evidence is None:
            raise EvidenceCatalogError("frozen evaluation was not found")
        path = evidence.root / filename
        if _sha256(path) != evidence.checksums[filename]:
            raise EvidenceCatalogError("evaluation artifact checksum changed")
        return path, ARTIFACT_FILES[filename][2]
