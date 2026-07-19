from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from apps.api.app.domain.campaigns import (
    BriefValidationResult,
    CampaignBriefDraft,
    CampaignBriefInput,
    FrozenStrictModel,
    ReadyCampaignBrief,
)
from apps.api.app.domain.models import CommunicationBundle, Identifier, Sha256, StrictModel
from apps.api.app.domain.quality import QualityReport


class CampaignState(StrEnum):
    DRAFT = "DRAFT"
    NEEDS_INPUT = "NEEDS_INPUT"
    READY = "READY"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    APPROVABLE = "APPROVABLE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLOCKED = "BLOCKED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    APPROVED = "APPROVED"
    ACCEPTED_WITH_WARNING = "ACCEPTED_WITH_WARNING"
    EXPORTED = "EXPORTED"


class CampaignCreateRequest(StrictModel):
    case_id: Identifier | None = None
    brief: CampaignBriefInput | None = None

    @model_validator(mode="after")
    def source_is_unambiguous(self) -> CampaignCreateRequest:
        if self.case_id is not None and self.brief is not None:
            raise ValueError("case_id and brief are mutually exclusive")
        return self


class CampaignView(FrozenStrictModel):
    campaign_id: Identifier
    state: CampaignState
    draft_version: int = Field(ge=1)
    draft: CampaignBriefDraft
    validation: BriefValidationResult | None = None
    ready_brief: ReadyCampaignBrief | None = None
    context_version: Sha256 | None = None
    package_id: Identifier | None = None
    created_at: datetime
    updated_at: datetime


class CaseView(FrozenStrictModel):
    case_id: Identifier
    title: str
    expected_status: str
    synthetic: Literal[True] = True


class DeterministicRunRequest(StrictModel):
    mode: Literal["deterministic_template", "live_ouroboros"] = "deterministic_template"


class DemoResetRequest(StrictModel):
    confirmation: Literal["СБРОСИТЬ ДЕМО"]


class DemoResetResult(FrozenStrictModel):
    reset_id: Identifier
    status: Literal["RESET"] = "RESET"
    catalog_case_count: int = Field(ge=0)
    observed_case_count: Literal[0] = 0
    live_case_count: Literal[0] = 0
    provider_calls: Literal[0] = 0
    reset_at: datetime


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    COMPLETED = "COMPLETED"
    COMPLETED_FALLBACK = "COMPLETED_FALLBACK"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RunAttemptStatus(StrEnum):
    PREPARED = "PREPARED"
    SUBMITTING = "SUBMITTING"
    RUNNING = "RUNNING"
    TERMINAL = "TERMINAL"
    RELEASED = "RELEASED"


class RunAttemptView(FrozenStrictModel):
    attempt_id: Identifier
    run_id: Identifier
    attempt_number: int = Field(ge=1, le=2)
    task_id: Identifier
    status: RunAttemptStatus
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    provider_profile: str = Field(min_length=1, max_length=128)
    request_digest: Sha256
    context_digest: Sha256
    outcome: str = Field(min_length=1, max_length=64)
    reason_code: Identifier | None = None
    failure_kind: str = Field(default="", max_length=64)
    retry_allowed: bool = False
    tool_receipts: tuple[Identifier, ...] = Field(default_factory=tuple)
    provider_call_ledger: dict[str, object] = Field(default_factory=dict)
    usage_status: Literal["EXACT", "UNKNOWN"] = "UNKNOWN"
    draft_present: bool = False
    result_present: bool = False
    created_at: datetime
    started_at: datetime | None = None
    terminal_at: datetime | None = None
    released_at: datetime | None = None


class RunView(FrozenStrictModel):
    run_id: Identifier
    campaign_id: Identifier
    operation: Literal["initial", "revision", "rule_proposal"]
    iteration: int = Field(ge=1, le=100)
    requested_mode: Literal["live_ouroboros", "deterministic_template"]
    mode: Literal["live_ouroboros", "deterministic_template"]
    status: RunStatus
    reason_code: Identifier | None = None
    task_id: Identifier | None = None
    project_id: Identifier
    context_version: Sha256
    package_id: Identifier | None = None
    prompt_hash: Sha256 | None = None
    skill_content_hash: Sha256 | None = None
    tool_inventory_hash: Sha256 | None = None
    tool_receipts: tuple[Identifier, ...] = Field(default_factory=tuple)
    provider_call_ledger: dict[str, object] = Field(default_factory=dict)
    physical_attempt_count: int = Field(default=1, ge=1)
    attempts: tuple[RunAttemptView, ...] = Field(default_factory=tuple)
    final_answer: str | None = Field(default=None, max_length=4_000)
    created_at: datetime
    started_at: datetime | None = None
    terminal_at: datetime | None = None
    worker_released_at: datetime | None = None


class RunEventView(FrozenStrictModel):
    event_id: int = Field(ge=1)
    run_id: Identifier
    event_type: Identifier
    data: dict[str, object] = Field(default_factory=dict)
    created_at: datetime


class PackageView(FrozenStrictModel):
    package_id: Identifier
    campaign_id: Identifier
    package_version: int = Field(ge=1)
    mode: Literal["deterministic_template", "live_ouroboros", "replay", "mock"]
    context_version: Sha256
    package_hash: Sha256
    bundle: CommunicationBundle
    quality_report: QualityReport
    email_html: str
    created_at: datetime


class ApprovalDecision(StrEnum):
    APPROVED = "APPROVED"
    ACCEPTED_WITH_WARNING = "ACCEPTED_WITH_WARNING"


class ApprovalRequest(StrictModel):
    package_hash: Sha256
    decision: ApprovalDecision
    acknowledged_warning_ids: tuple[Identifier, ...] = Field(default_factory=tuple)
    test_only: bool = False


class ApprovalRecord(FrozenStrictModel):
    approval_id: Identifier
    package_id: Identifier
    package_hash: Sha256
    decision: ApprovalDecision
    acknowledged_warning_ids: tuple[Identifier, ...]
    actor_id: Identifier
    actor_role: Literal["human"]
    test_only: bool
    approval_hash: Sha256
    created_at: datetime


class ExportRecord(FrozenStrictModel):
    export_id: Identifier
    package_id: Identifier
    package_hash: Sha256
    approval_hash: Sha256
    archive_sha256: Sha256
    file_count: int = Field(ge=1)
    synthetic: Literal[True] = True
    no_send: Literal[True] = True
    created_at: datetime
