from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from apps.api.app.domain.campaigns import ContextBundle, FrozenStrictModel
from apps.api.app.domain.learning import (
    FeedbackView,
    PackageDiffView,
    RuleProposalView,
    RuleVersionView,
)
from apps.api.app.domain.models import Identifier, Sha256
from apps.api.app.domain.workflow import (
    ApprovalRecord,
    CampaignView,
    CaseView,
    ExportRecord,
    PackageView,
    RunStatus,
    RunView,
)


class DashboardCaseView(FrozenStrictModel):
    case: CaseView
    campaign_id: Identifier | None = None
    actual_status: str | None = Field(default=None, max_length=64)
    execution_mode: str | None = Field(default=None, max_length=64)
    last_run_status: str | None = Field(default=None, max_length=64)
    latency_ms: int | None = Field(default=None, ge=0)
    qa_score: int | None = Field(default=None, ge=0, le=100)
    blocker_count: int = Field(default=0, ge=0)
    package_id: Identifier | None = None
    updated_at: datetime | None = None


class DashboardMetrics(FrozenStrictModel):
    catalog_case_count: int = Field(ge=0)
    target_business_case_count: int = Field(ge=1)
    observed_case_count: int = Field(ge=0)
    live_case_count: int = Field(ge=0)
    p50_latency_ms: int | None = Field(default=None, ge=0)
    p95_latency_ms: int | None = Field(default=None, ge=0)
    max_latency_ms: int | None = Field(default=None, ge=0)
    crash_count: int = Field(default=0, ge=0)
    timeout_count: int = Field(default=0, ge=0)
    provider_tokens: int = Field(default=0, ge=0)
    provider_cost_usd: float = Field(default=0.0, ge=0)


class DashboardView(FrozenStrictModel):
    generated_at: datetime
    business_cases: tuple[DashboardCaseView, ...]
    chaos_cases: tuple[DashboardCaseView, ...] = ()
    metrics: DashboardMetrics
    synthetic: Literal[True] = True
    no_send: Literal[True] = True


class SafeTraceEventView(FrozenStrictModel):
    event_id: str = Field(min_length=3, max_length=160)
    event_type: str = Field(min_length=3, max_length=128)
    label: str = Field(min_length=1, max_length=300)
    created_at: datetime
    mode: str | None = Field(default=None, max_length=64)


class OperationPresentationView(FrozenStrictModel):
    run_id: Identifier
    operation: Literal["initial", "revision", "rule_proposal"]
    status: RunStatus
    mode: Literal["live_ouroboros", "deterministic_template"]
    active: bool
    title: str = Field(min_length=1, max_length=160)
    stage: str = Field(min_length=1, max_length=64)
    stage_label: str = Field(min_length=1, max_length=240)
    attempt_number: int = Field(ge=1, le=2)
    elapsed_from: datetime
    result_hint: Literal["Результат появится здесь после сохранения."] = (
        "Результат появится здесь после сохранения."
    )
    reason_code: Identifier | None = None


class WorkspaceView(FrozenStrictModel):
    campaign: CampaignView
    context: ContextBundle | None = None
    package: PackageView | None = None
    package_history: tuple[PackageView, ...] = ()
    feedback: tuple[FeedbackView, ...] = ()
    diffs: tuple[PackageDiffView, ...] = ()
    rule_proposals: tuple[RuleProposalView, ...] = ()
    rule_versions: tuple[RuleVersionView, ...] = ()
    approvals: tuple[ApprovalRecord, ...] = ()
    exports: tuple[ExportRecord, ...] = ()
    runs: tuple[RunView, ...] = ()
    safe_trace: tuple[SafeTraceEventView, ...] = ()
    operation_state: OperationPresentationView | None = None
    approval_eligible: bool = False
    approval_disabled_reason: str | None = Field(default=None, max_length=128)
    export_eligible: bool = False
    export_disabled_reason: str | None = Field(default=None, max_length=128)


class EvaluationReportLink(FrozenStrictModel):
    label: str = Field(min_length=1, max_length=120)
    format: Literal["json", "html", "csv", "pdf", "jpg"]
    href: str = Field(pattern=r"^/api/v1/[A-Za-z0-9_./:-]+$", max_length=300)
    checksum: Sha256 | None = None


class EvaluationRunView(FrozenStrictModel):
    evaluation_id: Identifier
    label: str = Field(min_length=1, max_length=200)
    status: Literal["NOT_FROZEN", "FROZEN", "FAILED"]
    frozen: bool
    generated_at: datetime
    business_cases: tuple[DashboardCaseView, ...]
    chaos_cases: tuple[DashboardCaseView, ...]
    metrics: DashboardMetrics
    mode_counts: dict[str, int]
    qualitative_review_status: Literal["WAITING_FOR_OPERATOR", "COMPLETE"]
    report_links: tuple[EvaluationReportLink, ...]
    synthetic: Literal[True] = True
    no_send: Literal[True] = True


class EvaluationRunSummary(FrozenStrictModel):
    evaluation_id: Identifier
    label: str = Field(min_length=1, max_length=200)
    status: Literal["NOT_FROZEN", "FROZEN", "FAILED"]
    frozen: bool
    generated_at: datetime
    observed_case_count: int = Field(ge=0)


class MvpSmsResult(FrozenStrictModel):
    text: str = Field(min_length=1, max_length=2_000)
    segments: int | None = Field(default=None, ge=1)


class MvpEmailResult(FrozenStrictModel):
    subject: str = Field(min_length=1, max_length=500)
    plain_text: str = Field(min_length=1, max_length=20_000)


class MvpCaseResult(FrozenStrictModel):
    case_id: Identifier
    title: str = Field(min_length=1, max_length=300)
    actual_terminal: str = Field(min_length=1, max_length=64)
    qa_score: int = Field(ge=0, le=100)
    latency_ms: int = Field(ge=0)
    provider_calls: int = Field(ge=1)
    provider_tokens: int = Field(ge=1)
    cost_usd: float = Field(ge=0)
    channels: tuple[Literal["sms", "email"], ...]
    sms: MvpSmsResult | None = None
    email: MvpEmailResult | None = None


class MvpResultsMetrics(FrozenStrictModel):
    confirmed_live_case_count: int = Field(ge=1)
    basket_live_case_count: int = Field(ge=1)
    full_basket_passed_count: int = Field(ge=1)
    full_basket_case_count: int = Field(ge=1)
    p50_latency_ms: int = Field(ge=0)
    p95_latency_ms: int = Field(ge=0)
    max_latency_ms: int = Field(ge=0)
    provider_calls: int = Field(ge=1)
    provider_tokens: int = Field(ge=1)
    provider_cost_usd: float = Field(ge=0)


class MvpResultsView(FrozenStrictModel):
    results_id: Identifier
    status: Literal["MVP_CONFIRMED_NON_RELEASE"]
    generated_at: datetime
    cases: tuple[MvpCaseResult, ...]
    metrics: MvpResultsMetrics
    report_links: tuple[EvaluationReportLink, ...]
    canonical_release_evidence: Literal[False] = False
    synthetic: Literal[True] = True
    no_send: Literal[True] = True


class DiagnosticComponent(FrozenStrictModel):
    component_id: Identifier
    label: str = Field(min_length=1, max_length=120)
    status: Literal["READY", "DEGRADED", "ISOLATED"]
    detail: str = Field(min_length=1, max_length=500)


class DiagnosticErrorView(FrozenStrictModel):
    run_id: Identifier
    reason_code: Identifier | None = None
    status: str = Field(min_length=1, max_length=64)
    created_at: datetime


class DiagnosticsView(FrozenStrictModel):
    generated_at: datetime
    components: tuple[DiagnosticComponent, ...]
    runtime_tag: str | None = Field(default=None, max_length=80)
    runtime_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    skill_hash: Sha256 | None = None
    prompt_hash: Sha256 | None = None
    tool_inventory_hash: Sha256 | None = None
    discovered_tools: tuple[str, ...] = ()
    contract_generated_at: datetime | None = None
    active_run_count: int = Field(ge=0)
    queue_state: Literal["IDLE", "ACTIVE"]
    admission_state: Literal["CLOSED", "OPEN"]
    latest_errors: tuple[DiagnosticErrorView, ...] = ()
    public_config_only: Literal[True] = True
