from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from apps.api.app.domain.campaigns import FrozenStrictModel
from apps.api.app.domain.models import Identifier, JsonPointer, Sha256


class FindingSeverity(StrEnum):
    BLOCKER = "BLOCKER"
    WARNING = "WARNING"
    INFO = "INFO"


class FindingArtifact(StrEnum):
    BRIEF = "brief"
    SMS = "sms"
    EMAIL = "email"
    PACKAGE = "package"
    RULE = "rule"


class FindingStatus(StrEnum):
    OPEN = "OPEN"
    FIXED = "FIXED"
    ACCEPTED = "ACCEPTED"
    RECHECKED = "RECHECKED"


class Finding(FrozenStrictModel):
    finding_id: Identifier
    check_id: Identifier
    severity: FindingSeverity
    artifact: FindingArtifact
    path: JsonPointer | None = None
    quote: str | None = Field(default=None, max_length=1_000)
    expected: str | None = Field(default=None, max_length=2_000)
    actual: str | None = Field(default=None, max_length=2_000)
    source_ids: tuple[Identifier, ...] = Field(default_factory=tuple)
    recommendation: str = Field(min_length=1, max_length=2_000)
    checker: Literal["deterministic", "human"] = "deterministic"
    status: FindingStatus = FindingStatus.OPEN
    blocking: bool

    @model_validator(mode="after")
    def blocker_flag_matches_severity(self) -> Finding:
        if self.blocking != (self.severity is FindingSeverity.BLOCKER):
            raise ValueError("blocking must match BLOCKER severity")
        return self


class SmsMetrics(FrozenStrictModel):
    encoding: Literal["GSM-7", "UCS-2"]
    characters: int = Field(ge=0)
    code_units: int = Field(ge=0)
    septets: int | None = Field(default=None, ge=0)
    segments: int = Field(ge=0)
    units_per_segment: int = Field(ge=1)


class QualityReport(FrozenStrictModel):
    report_version: Literal["1.0"] = "1.0"
    registry_version: Literal["1.0"] = "1.0"
    registry_hash: Sha256
    package_hash: Sha256
    context_version: Sha256
    findings: tuple[Finding, ...]
    approvable: bool
    checked_ids: tuple[Identifier, ...]
    checked_fact_ids: tuple[Identifier, ...]
    checked_claim_ids: tuple[Identifier, ...]
    checked_policy_ids: tuple[Identifier, ...]
    sms_metrics: SmsMetrics | None = None
    deterministic_score: int = Field(ge=0, le=100)
    evidence_hashes: dict[str, Sha256]

    @model_validator(mode="after")
    def approvable_matches_open_blockers(self) -> QualityReport:
        has_blocker = any(
            finding.blocking and finding.status is FindingStatus.OPEN for finding in self.findings
        )
        if self.approvable == has_blocker:
            raise ValueError("approvable must be false exactly when an open blocker exists")
        return self
