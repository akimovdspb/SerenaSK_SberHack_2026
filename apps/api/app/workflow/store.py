from __future__ import annotations

import hashlib
import html
import json
import pathlib
import shutil
import threading
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, TypeVar, cast

from pydantic import BaseModel
from pydantic_core import to_jsonable_python
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
    update,
)
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from apps.api.app.domain.authoring import (
    AuthoringCatalogView,
    AuthoringProductView,
    CustomProductCreateRequest,
    CustomProductRecord,
    RecentCampaignView,
)
from apps.api.app.domain.campaigns import (
    BriefValidationResult,
    CampaignBriefDraft,
    CampaignBriefInput,
    ContextBundle,
    ReadyCampaignBrief,
)
from apps.api.app.domain.learning import (
    FeedbackCreateRequest,
    FeedbackView,
    PackageDiffView,
    RuleApprovalRequest,
    RuleProposalStatus,
    RuleProposalView,
    RuleRejectionRequest,
    RuleRollbackRequest,
    RuleTestResult,
    RuleVersionStatus,
    RuleVersionView,
)
from apps.api.app.domain.models import (
    CommunicationBundle,
    CommunicationBundleEnvelope,
    CommunicationPatchEnvelope,
    DraftSaveRequest,
    Operation,
    RuleProposal,
    RuleProposalDraft,
    RuleProposalEnvelope,
    RuleScope,
)
from apps.api.app.domain.presentation import (
    DashboardCaseView,
    DashboardMetrics,
    DashboardView,
    DiagnosticErrorView,
    EvaluationReportLink,
    EvaluationRunSummary,
    EvaluationRunView,
    OperationPresentationView,
    SafeTraceEventView,
    WorkspaceView,
)
from apps.api.app.domain.quality import FindingSeverity, QualityReport
from apps.api.app.domain.workflow import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRequest,
    CampaignState,
    CampaignView,
    CaseView,
    DemoResetResult,
    ExportRecord,
    PackageView,
    RunAttemptStatus,
    RunAttemptView,
    RunEventView,
    RunStatus,
    RunView,
)
from apps.api.app.mcp.service import DraftProcessingResult
from apps.api.app.services.authoring import (
    catalog_product_view,
    custom_product_view,
    load_reference_prefills,
    materialize_custom_product,
    persona_views,
)
from apps.api.app.services.briefs import (
    build_initial_context,
    create_draft,
    hash_value,
    validate_and_promote,
)
from apps.api.app.services.catalog import SyntheticCatalog, load_catalog
from apps.api.app.services.deterministic import build_deterministic_bundle
from apps.api.app.services.evidence_catalog import EvidenceCatalog, EvidenceCatalogError
from apps.api.app.services.quality import evaluate_bundle
from apps.api.app.services.rendering import render_email_html
from apps.api.app.services.revisions import (
    RevisionError,
    build_deterministic_patch,
    build_revision_context,
    merge_communication_patch,
)
from apps.api.app.services.rules import (
    active_rule_payload,
    active_rules_hash,
    apply_active_rules,
    build_deterministic_rule_proposal,
    materialize_rule_proposal_draft,
    validate_rule_proposal,
)
from apps.api.app.sqlite_runtime import create_sqlite_aware_engine

ModelT = TypeVar("ModelT", bound=BaseModel)
ACTIVE_RUN_STATUSES = {
    RunStatus.QUEUED.value,
    RunStatus.RUNNING.value,
    RunStatus.CANCEL_REQUESTED.value,
}
TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED.value,
    RunStatus.COMPLETED_FALLBACK.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
}


class WorkflowError(RuntimeError):
    pass


class WorkflowNotFound(WorkflowError):
    pass


class WorkflowConflict(WorkflowError):
    pass


class WorkflowInvalidState(WorkflowError):
    pass


class WorkflowBase(DeclarativeBase):
    pass


class CampaignRow(WorkflowBase):
    __tablename__ = "campaigns"

    campaign_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_case_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    current_draft_version: Mapped[int] = mapped_column(Integer, nullable=False)
    current_ready_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_context_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_package_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CustomProductRow(WorkflowBase):
    __tablename__ = "custom_product_versions"
    __table_args__ = (UniqueConstraint("product_id", "version", name="uq_custom_product_version"),)

    record_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    record_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BriefVersionRow(WorkflowBase):
    __tablename__ = "campaign_brief_versions"
    __table_args__ = (UniqueConstraint("campaign_id", "version", name="uq_campaign_brief_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.campaign_id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    draft_json: Mapped[str] = mapped_column(Text, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    ready_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ContextRow(WorkflowBase):
    __tablename__ = "campaign_context_versions"

    context_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.campaign_id"), nullable=False, index=True
    )
    brief_version: Mapped[int] = mapped_column(Integer, nullable=False)
    context_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PackageRow(WorkflowBase):
    __tablename__ = "package_versions"
    __table_args__ = (
        UniqueConstraint("campaign_id", "version", name="uq_campaign_package_version"),
    )

    package_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.campaign_id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    context_version: Mapped[str] = mapped_column(String(64), nullable=False)
    package_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_json: Mapped[str] = mapped_column(Text, nullable=False)
    report_json: Mapped[str] = mapped_column(Text, nullable=False)
    email_html: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApprovalRow(WorkflowBase):
    __tablename__ = "package_approvals"

    approval_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    package_id: Mapped[str] = mapped_column(
        ForeignKey("package_versions.package_id"), unique=True, nullable=False
    )
    package_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    acknowledged_warning_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    test_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    approval_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExportRow(WorkflowBase):
    __tablename__ = "package_exports"

    export_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    package_id: Mapped[str] = mapped_column(
        ForeignKey("package_versions.package_id"), unique=True, nullable=False
    )
    package_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    archive_path: Mapped[str] = mapped_column(Text, nullable=False)
    archive_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FeedbackRow(WorkflowBase):
    __tablename__ = "package_feedback"

    feedback_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.campaign_id"), nullable=False, index=True
    )
    package_id: Mapped[str] = mapped_column(
        ForeignKey("package_versions.package_id"), nullable=False, index=True
    )
    package_version: Mapped[int] = mapped_column(Integer, nullable=False)
    package_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_path: Mapped[str] = mapped_column(String(512), nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    author_id: Mapped[str] = mapped_column(String(128), nullable=False)
    author_role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PackageDiffRow(WorkflowBase):
    __tablename__ = "package_diffs"

    diff_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.campaign_id"), nullable=False, index=True
    )
    feedback_id: Mapped[str] = mapped_column(
        ForeignKey("package_feedback.feedback_id"), nullable=False, index=True
    )
    from_package_id: Mapped[str] = mapped_column(String(128), nullable=False)
    from_package_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    to_package_id: Mapped[str] = mapped_column(
        ForeignKey("package_versions.package_id"), unique=True, nullable=False
    )
    to_package_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    changed_paths_json: Mapped[str] = mapped_column(Text, nullable=False)
    changes_json: Mapped[str] = mapped_column(Text, nullable=False)
    protected_paths_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RuleProposalRow(WorkflowBase):
    __tablename__ = "rule_proposals"

    proposal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.campaign_id"), nullable=False, index=True
    )
    context_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_feedback_id: Mapped[str] = mapped_column(
        ForeignKey("package_feedback.feedback_id"), nullable=False, index=True
    )
    selected_scope_json: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    validation_errors_json: Mapped[str] = mapped_column(Text, nullable=False)
    tests_json: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    test_only: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RuleVersionRow(WorkflowBase):
    __tablename__ = "rule_versions"

    rule_version_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    rule_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("rule_proposals.proposal_id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_json: Mapped[str] = mapped_column(Text, nullable=False)
    rules_version: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_rules_version: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    test_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RuleSetPointerRow(WorkflowBase):
    __tablename__ = "active_rule_set"

    pointer_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    rules_version: Mapped[str] = mapped_column(String(64), nullable=False)
    active_rule_version_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IdempotencyRow(WorkflowBase):
    __tablename__ = "http_idempotency"
    __table_args__ = (UniqueConstraint("scope", "key", name="uq_http_idempotency"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(200), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RunRow(WorkflowBase):
    __tablename__ = "campaign_runs"

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.campaign_id"), nullable=False, index=True
    )
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reason_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    project_id: Mapped[str] = mapped_column(String(128), nullable=False)
    context_version: Mapped[str] = mapped_column(String(64), nullable=False)
    package_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    skill_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_inventory_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_receipts_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    provider_call_ledger_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    physical_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    final_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RunAttemptRow(WorkflowBase):
    __tablename__ = "run_attempts"
    __table_args__ = (UniqueConstraint("run_id", "attempt_number", name="uq_run_attempt_number"),)

    attempt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_runs.run_id"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(256), nullable=False)
    provider_profile: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    context_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    retry_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tool_receipts_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    provider_call_ledger_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    usage_status: Mapped[str] = mapped_column(String(16), nullable=False, default="UNKNOWN")
    draft_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    result_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunEventRow(WorkflowBase):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "event_key", name="uq_run_event_key"),)

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_runs.run_id"), nullable=False, index=True
    )
    event_key: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    data_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _json_text(value: Any, *, pretty: bool = False) -> str:
    return json.dumps(
        to_jsonable_python(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=None if pretty else (",", ":"),
        indent=2 if pretty else None,
    )


def _json_bytes(value: Any) -> bytes:
    return (_json_text(value, pretty=True) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class WorkflowStore:
    def __init__(
        self,
        database_url: str,
        *,
        data_dir: pathlib.Path,
        artifacts_dir: pathlib.Path,
        evidence_dir: pathlib.Path | None = None,
    ) -> None:
        self._engine = create_sqlite_aware_engine(database_url)
        self._data_dir = data_dir
        self._catalog: SyntheticCatalog | None = None
        self._artifacts_dir = artifacts_dir
        self._evidence_dir = evidence_dir
        self._write_lock = threading.RLock()

    def initialize(self) -> None:
        self._catalog = load_catalog(self._data_dir)
        WorkflowBase.metadata.create_all(self._engine)
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        with self._write_lock, Session(self._engine) as session:
            if session.get(RuleSetPointerRow, "active") is None:
                session.add(
                    RuleSetPointerRow(
                        pointer_id="active",
                        rules_version=self._catalog.rules_version,
                        active_rule_version_ids_json="[]",
                        updated_at=datetime.now(UTC),
                    )
                )
            self._backfill_legacy_run_attempts(session)
            session.commit()

    def _backfill_legacy_run_attempts(self, session: Session) -> None:
        run_ids_with_attempts = set(session.scalars(select(RunAttemptRow.run_id)))
        for run in session.scalars(select(RunRow).order_by(RunRow.created_at)):
            if run.run_id in run_ids_with_attempts or run.task_id is None:
                continue
            try:
                ledger = json.loads(run.provider_call_ledger_json)
            except json.JSONDecodeError:
                ledger = {}
            providers: set[str] = set()
            models: set[str] = set()
            if isinstance(ledger, dict):
                for value in ledger.values():
                    if not isinstance(value, dict):
                        continue
                    providers.update(str(item) for item in value.get("providers") or [] if item)
                    models.update(str(item) for item in value.get("models") or [] if item)
            if run.worker_released_at is not None:
                attempt_status = RunAttemptStatus.RELEASED.value
            elif run.terminal_at is not None:
                attempt_status = RunAttemptStatus.TERMINAL.value
            elif run.started_at is not None:
                attempt_status = RunAttemptStatus.RUNNING.value
            else:
                attempt_status = RunAttemptStatus.PREPARED.value
            succeeded = run.status == RunStatus.COMPLETED.value
            terminal = run.terminal_at is not None
            session.add(
                RunAttemptRow(
                    attempt_id=f"attempt_legacy_{hashlib.sha256(run.run_id.encode()).hexdigest()[:32]}",
                    run_id=run.run_id,
                    attempt_number=1,
                    task_id=run.task_id,
                    status=attempt_status,
                    provider=next(iter(sorted(providers)), "unknown"),
                    model=next(iter(sorted(models)), "unknown"),
                    provider_profile="legacy_unknown",
                    request_digest=hashlib.sha256(
                        f"legacy:{run.run_id}:{run.context_version}".encode()
                    ).hexdigest(),
                    context_digest=run.context_version,
                    outcome=(
                        "SUCCEEDED"
                        if succeeded
                        else ("PERMANENT_FAILURE" if terminal else "PENDING")
                    ),
                    reason_code=(
                        "LIVE_RESULT_ACCEPTED"
                        if succeeded
                        else (run.reason_code if terminal else None)
                    ),
                    failure_kind="legacy_unclassified",
                    retry_allowed=False,
                    tool_receipts_json=run.tool_receipts_json,
                    provider_call_ledger_json=run.provider_call_ledger_json,
                    usage_status="UNKNOWN",
                    draft_present=run.package_id is not None,
                    result_present=run.package_id is not None,
                    created_at=run.created_at,
                    started_at=run.started_at,
                    terminal_at=run.terminal_at,
                    released_at=run.worker_released_at,
                )
            )

    def reset_demo_state(self) -> DemoResetResult:
        """Clear only mutable workflow state while preserving catalog and evidence."""
        if self._artifacts_dir.is_symlink():
            raise WorkflowInvalidState("demo artifacts directory must not be a symlink")
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        with self._write_lock, Session(self._engine) as session:
            active = session.scalar(
                select(RunRow).where(RunRow.status.in_(ACTIVE_RUN_STATUSES)).limit(1)
            )
            if active is not None:
                raise WorkflowConflict("demo reset is unavailable while a run is active")
            for table in reversed(WorkflowBase.metadata.sorted_tables):
                if table.name == CustomProductRow.__tablename__:
                    continue
                session.execute(table.delete())
            catalog = self._require_catalog()
            reset_at = datetime.now(UTC)
            session.add(
                RuleSetPointerRow(
                    pointer_id="active",
                    rules_version=catalog.rules_version,
                    active_rule_version_ids_json="[]",
                    updated_at=reset_at,
                )
            )
            session.commit()

            for child in self._artifacts_dir.iterdir():
                if child.is_symlink() or child.is_file():
                    child.unlink()
                elif child.is_dir():
                    shutil.rmtree(child)
                else:
                    raise WorkflowInvalidState("demo artifacts contain an unsupported entry")

        return DemoResetResult(
            reset_id=f"reset_{uuid.uuid4().hex}",
            catalog_case_count=len(self.list_cases()),
            reset_at=reset_at,
        )

    def list_cases(self) -> list[CaseView]:
        return [
            CaseView(
                case_id=case.case_id,
                title=case.title,
                expected_status=case.expected.status.value,
            )
            for case in self._require_catalog().cases.values()
        ]

    def _latest_custom_records(self, session: Session) -> dict[str, CustomProductRecord]:
        records: dict[str, CustomProductRecord] = {}
        rows = session.scalars(
            select(CustomProductRow).order_by(
                CustomProductRow.normalized_name,
                CustomProductRow.version.desc(),
            )
        )
        seen_names: set[str] = set()
        for row in rows:
            if row.normalized_name not in seen_names:
                records[row.product_id] = CustomProductRecord.model_validate_json(row.record_json)
                seen_names.add(row.normalized_name)
        return records

    def _all_custom_records(self, session: Session) -> dict[str, CustomProductRecord]:
        return {
            row.product_id: CustomProductRecord.model_validate_json(row.record_json)
            for row in session.scalars(select(CustomProductRow))
        }

    def _effective_catalog(self, session: Session) -> SyntheticCatalog:
        base = self._require_catalog()
        custom = {
            product_id: record.product
            for product_id, record in self._all_custom_records(session).items()
        }
        return replace(base, products={**base.products, **custom})

    def create_custom_product(
        self,
        request: CustomProductCreateRequest,
    ) -> AuthoringProductView:
        base = self._require_catalog()
        normalized_name = " ".join(request.exact_name.casefold().split())
        if any(
            " ".join(product.fact_card.exact_name.casefold().split()) == normalized_name
            for product in base.products.values()
        ):
            raise WorkflowConflict("product name already belongs to the immutable catalog")
        with self._write_lock, Session(self._engine) as session:
            latest = session.scalar(
                select(CustomProductRow)
                .where(CustomProductRow.normalized_name == normalized_name)
                .order_by(CustomProductRow.version.desc())
                .limit(1)
            )
            next_version = (latest.version + 1) if latest is not None else 1
            record = materialize_custom_product(request, version=next_version)
            if latest is not None and latest.request_hash == record.request_hash:
                return custom_product_view(
                    CustomProductRecord.model_validate_json(latest.record_json)
                )
            now = datetime.now(UTC)
            product_id = record.product.fact_card.product_id
            session.add(
                CustomProductRow(
                    record_id=f"{product_id}:v{next_version}",
                    product_id=product_id,
                    version=next_version,
                    normalized_name=normalized_name,
                    request_hash=record.request_hash,
                    record_json=_json_text(record.model_dump(mode="json")),
                    created_at=now,
                )
            )
            session.commit()
            return custom_product_view(record)

    def authoring_catalog(self) -> AuthoringCatalogView:
        with Session(self._engine) as session:
            base = self._require_catalog()
            records = self._latest_custom_records(session)
            products = [
                catalog_product_view(product_id, base) for product_id in sorted(base.products)
            ]
            products.extend(
                custom_product_view(records[product_id]) for product_id in sorted(records)
            )
            references = load_reference_prefills(
                self._data_dir.parent / "editorial" / "copy_quality_references.json"
            )
            return AuthoringCatalogView(
                products=tuple(products),
                personas=persona_views(base),
                references=references,
            )

    def recent_authoring_campaigns(self, *, limit: int = 10) -> list[RecentCampaignView]:
        with Session(self._engine) as session:
            catalog = self._effective_catalog(session)
            rows = list(
                session.scalars(
                    select(CampaignRow)
                    .where(CampaignRow.source_case_id.is_(None))
                    .order_by(CampaignRow.updated_at.desc())
                    .limit(limit)
                )
            )
            result: list[RecentCampaignView] = []
            for row in rows:
                draft = CampaignBriefDraft.model_validate_json(
                    self._brief_row(session, row).draft_json
                )
                product = catalog.products.get(str(draft.product_id or ""))
                result.append(
                    RecentCampaignView(
                        campaign_id=row.campaign_id,
                        name=draft.name,
                        product_name=product.fact_card.exact_name if product is not None else None,
                        channels=tuple(channel.value for channel in draft.channels),
                        state=row.state,
                        updated_at=row.updated_at,
                    )
                )
            return result

    def dashboard(self) -> DashboardView:
        generated_at = datetime.now(UTC)
        case_views: list[DashboardCaseView] = []
        with Session(self._engine) as session:
            for case in self.list_cases():
                campaign = session.scalar(
                    select(CampaignRow)
                    .where(CampaignRow.source_case_id == case.case_id)
                    .order_by(CampaignRow.updated_at.desc())
                    .limit(1)
                )
                if campaign is None:
                    case_views.append(DashboardCaseView(case=case))
                    continue
                package = (
                    session.get(PackageRow, campaign.current_package_id)
                    if campaign.current_package_id is not None
                    else None
                )
                run = session.scalar(
                    select(RunRow)
                    .where(RunRow.campaign_id == campaign.campaign_id)
                    .order_by(RunRow.created_at.desc())
                    .limit(1)
                )
                report = (
                    QualityReport.model_validate_json(package.report_json)
                    if package is not None
                    else None
                )
                latency_ms = self._run_latency_ms(run) if run is not None else None
                case_views.append(
                    DashboardCaseView(
                        case=case,
                        campaign_id=campaign.campaign_id,
                        actual_status=campaign.state,
                        execution_mode=package.mode
                        if package is not None
                        else run.mode
                        if run is not None
                        else "validation_only",
                        last_run_status=run.status if run is not None else None,
                        latency_ms=latency_ms,
                        qa_score=report.deterministic_score if report is not None else None,
                        blocker_count=sum(
                            1
                            for finding in report.findings
                            if finding.severity is FindingSeverity.BLOCKER
                        )
                        if report is not None
                        else 0,
                        package_id=package.package_id if package is not None else None,
                        updated_at=campaign.updated_at,
                    )
                )
            live_runs = list(session.scalars(select(RunRow).where(RunRow.mode == "live_ouroboros")))
        latencies = sorted(
            latency for row in live_runs if (latency := self._run_latency_ms(row)) is not None
        )
        provider_tokens = 0
        provider_cost_usd = 0.0
        for row in live_runs:
            tokens, cost = self._provider_usage_totals(row.provider_call_ledger_json)
            provider_tokens += tokens
            provider_cost_usd += cost
        metrics = DashboardMetrics(
            catalog_case_count=len(case_views),
            target_business_case_count=15,
            observed_case_count=sum(item.campaign_id is not None for item in case_views),
            live_case_count=sum(item.execution_mode == "live_ouroboros" for item in case_views),
            p50_latency_ms=self._nearest_rank(latencies, 0.50),
            p95_latency_ms=self._nearest_rank(latencies, 0.95),
            max_latency_ms=max(latencies, default=None),
            crash_count=sum(
                row.status == RunStatus.FAILED.value
                and "timeout" not in (row.reason_code or "").lower()
                for row in live_runs
            ),
            timeout_count=sum("timeout" in (row.reason_code or "").lower() for row in live_runs),
            provider_tokens=provider_tokens,
            provider_cost_usd=round(provider_cost_usd, 6),
        )
        return DashboardView(
            generated_at=generated_at,
            business_cases=tuple(case_views),
            metrics=metrics,
        )

    def workspace(self, campaign_id: str) -> WorkspaceView:
        with Session(self._engine) as session:
            campaign = session.get(CampaignRow, campaign_id)
            if campaign is None:
                raise WorkflowNotFound("campaign was not found")
            package_rows = list(
                session.scalars(
                    select(PackageRow)
                    .where(PackageRow.campaign_id == campaign_id)
                    .order_by(PackageRow.version)
                )
            )
            package_ids = [row.package_id for row in package_rows]
            current_package = (
                session.get(PackageRow, campaign.current_package_id)
                if campaign.current_package_id is not None
                else None
            )
            context_row = (
                session.get(ContextRow, campaign.current_context_version)
                if campaign.current_context_version is not None
                else None
            )
            feedback_rows = list(
                session.scalars(
                    select(FeedbackRow)
                    .where(FeedbackRow.campaign_id == campaign_id)
                    .order_by(FeedbackRow.created_at)
                )
            )
            diff_rows = list(
                session.scalars(
                    select(PackageDiffRow)
                    .where(PackageDiffRow.campaign_id == campaign_id)
                    .order_by(PackageDiffRow.created_at)
                )
            )
            proposal_rows = list(
                session.scalars(
                    select(RuleProposalRow)
                    .where(RuleProposalRow.campaign_id == campaign_id)
                    .order_by(RuleProposalRow.created_at)
                )
            )
            proposal_ids = [row.proposal_id for row in proposal_rows]
            rule_rows = (
                list(
                    session.scalars(
                        select(RuleVersionRow)
                        .where(RuleVersionRow.proposal_id.in_(proposal_ids))
                        .order_by(RuleVersionRow.created_at)
                    )
                )
                if proposal_ids
                else []
            )
            approval_rows = (
                list(
                    session.scalars(
                        select(ApprovalRow)
                        .where(ApprovalRow.package_id.in_(package_ids))
                        .order_by(ApprovalRow.created_at)
                    )
                )
                if package_ids
                else []
            )
            export_rows = (
                list(
                    session.scalars(
                        select(ExportRow)
                        .where(ExportRow.package_id.in_(package_ids))
                        .order_by(ExportRow.created_at)
                    )
                )
                if package_ids
                else []
            )
            run_rows = list(
                session.scalars(
                    select(RunRow)
                    .where(RunRow.campaign_id == campaign_id)
                    .order_by(RunRow.created_at)
                )
            )
            run_ids = [row.run_id for row in run_rows]
            attempt_rows = (
                list(
                    session.scalars(
                        select(RunAttemptRow)
                        .where(RunAttemptRow.run_id.in_(run_ids))
                        .order_by(RunAttemptRow.run_id, RunAttemptRow.attempt_number)
                    )
                )
                if run_ids
                else []
            )
            attempts_by_run: dict[str, list[RunAttemptView]] = {}
            for attempt_row in attempt_rows:
                attempts_by_run.setdefault(attempt_row.run_id, []).append(
                    self._attempt_view(attempt_row)
                )
            event_rows = (
                list(
                    session.scalars(
                        select(RunEventRow)
                        .where(RunEventRow.run_id.in_(run_ids))
                        .order_by(RunEventRow.event_id)
                    )
                )
                if run_ids
                else []
            )
            safe_trace = self._workspace_trace(
                campaign=campaign,
                packages=package_rows,
                feedback=feedback_rows,
                diffs=diff_rows,
                proposals=proposal_rows,
                rules=rule_rows,
                approvals=approval_rows,
                exports=export_rows,
                runs=run_rows,
                run_events=event_rows,
            )
            operation_state = self._operation_presentation(run_rows, event_rows)
            approval_eligible, approval_reason = self._approval_eligibility(
                session, campaign, current_package
            )
            current_approval = next(
                (
                    row
                    for row in reversed(approval_rows)
                    if current_package is not None and row.package_id == current_package.package_id
                ),
                None,
            )
            export_eligible = approval_eligible and current_approval is not None
            export_reason = (
                None
                if export_eligible
                else (approval_reason if not approval_eligible else "PACKAGE_NOT_APPROVED")
            )
            return WorkspaceView(
                campaign=self._campaign_view(session, campaign),
                context=ContextBundle.model_validate_json(context_row.context_json)
                if context_row is not None
                else None,
                package=self._package_view(current_package)
                if current_package is not None
                else None,
                package_history=tuple(self._package_view(row) for row in package_rows),
                feedback=tuple(self._feedback_view(row) for row in feedback_rows),
                diffs=tuple(self._package_diff_view(row) for row in diff_rows),
                rule_proposals=tuple(self._rule_proposal_view(row) for row in proposal_rows),
                rule_versions=tuple(self._rule_version_view(session, row) for row in rule_rows),
                approvals=tuple(self._approval_record(row) for row in approval_rows),
                exports=tuple(self._export_record(row) for row in export_rows),
                runs=tuple(
                    self._run_view(row, tuple(attempts_by_run.get(row.run_id, [])))
                    for row in run_rows
                ),
                safe_trace=tuple(safe_trace),
                operation_state=operation_state,
                approval_eligible=approval_eligible,
                approval_disabled_reason=approval_reason,
                export_eligible=export_eligible,
                export_disabled_reason=export_reason,
            )

    def evaluation_run(self, evaluation_id: str = "current_development_slice") -> EvaluationRunView:
        if evaluation_id != "current_development_slice":
            if self._evidence_dir is None:
                raise WorkflowNotFound("evaluation run was not found")
            try:
                return EvidenceCatalog(self._evidence_dir, self._require_catalog()).run(
                    evaluation_id
                )
            except EvidenceCatalogError as exc:
                raise WorkflowNotFound("evaluation run was not found") from exc
        dashboard = self.dashboard()
        mode_counts: dict[str, int] = {}
        for item in dashboard.business_cases:
            if item.execution_mode is not None:
                mode_counts[item.execution_mode] = mode_counts.get(item.execution_mode, 0) + 1
        return EvaluationRunView(
            evaluation_id="current_development_slice",
            label="Текущий незамороженный инженерный срез",
            status="NOT_FROZEN",
            frozen=False,
            generated_at=dashboard.generated_at,
            business_cases=dashboard.business_cases,
            chaos_cases=dashboard.chaos_cases,
            metrics=dashboard.metrics,
            mode_counts=mode_counts,
            qualitative_review_status="WAITING_FOR_OPERATOR",
            report_links=(
                EvaluationReportLink(
                    label="Публичный JSON текущего среза",
                    format="json",
                    href="/api/v1/evaluation/runs/current_development_slice",
                ),
            ),
        )

    def evaluation_runs(self) -> list[EvaluationRunSummary]:
        current = self.evaluation_run()
        frozen = (
            EvidenceCatalog(self._evidence_dir, self._require_catalog()).summaries()
            if self._evidence_dir is not None
            else []
        )
        return [
            *frozen,
            EvaluationRunSummary(
                evaluation_id=current.evaluation_id,
                label=current.label,
                status=current.status,
                frozen=current.frozen,
                generated_at=current.generated_at,
                observed_case_count=current.metrics.observed_case_count,
            ),
        ]

    def evaluation_artifact(
        self,
        evaluation_id: str,
        filename: str,
    ) -> tuple[pathlib.Path, str]:
        if self._evidence_dir is None:
            raise WorkflowNotFound("evaluation artifact was not found")
        try:
            return EvidenceCatalog(self._evidence_dir, self._require_catalog()).artifact(
                evaluation_id,
                filename,
            )
        except EvidenceCatalogError as exc:
            raise WorkflowNotFound("evaluation artifact was not found") from exc

    def latest_run_errors(self, *, limit: int = 5) -> list[DiagnosticErrorView]:
        with Session(self._engine) as session:
            rows = list(
                session.scalars(
                    select(RunRow)
                    .where(RunRow.status.in_({RunStatus.FAILED.value, RunStatus.CANCELLED.value}))
                    .order_by(RunRow.created_at.desc())
                    .limit(limit)
                )
            )
            return [
                DiagnosticErrorView(
                    run_id=row.run_id,
                    reason_code=row.reason_code,
                    status=row.status,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    def execute_idempotent(
        self,
        *,
        scope: str,
        key: str,
        payload: Any,
        operation: Callable[[], ModelT],
    ) -> dict[str, Any]:
        request_hash = hash_value(payload)
        with self._write_lock:
            with Session(self._engine) as session:
                existing = session.scalar(
                    select(IdempotencyRow).where(
                        IdempotencyRow.scope == scope,
                        IdempotencyRow.key == key,
                    )
                )
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise WorkflowConflict(
                            "idempotency key was reused with a different request"
                        )
                    response = json.loads(existing.response_json)
                    if not isinstance(response, dict):
                        raise WorkflowConflict("stored idempotent response is invalid")
                    return response
            result = operation()
            response = result.model_dump(mode="json")
            with Session(self._engine) as session:
                session.add(
                    IdempotencyRow(
                        scope=scope,
                        key=key,
                        request_hash=request_hash,
                        response_json=_json_text(response),
                        created_at=datetime.now(UTC),
                    )
                )
                session.commit()
            return response

    def create_campaign(
        self,
        *,
        brief: CampaignBriefInput | None,
        case_id: str | None,
    ) -> CampaignView:
        if case_id is not None:
            case = self._require_catalog().cases.get(case_id)
            if case is None:
                raise WorkflowNotFound("synthetic case was not found")
            values = case.brief
        else:
            values = brief or CampaignBriefInput()
        now = datetime.now(UTC)
        campaign_id = f"campaign_{uuid.uuid4().hex}"
        draft = create_draft(campaign_id=campaign_id, values=values)
        with self._write_lock, Session(self._engine) as session:
            campaign = CampaignRow(
                campaign_id=campaign_id,
                source_case_id=case_id,
                state=CampaignState.DRAFT.value,
                current_draft_version=1,
                current_ready_version=None,
                current_context_version=None,
                current_package_id=None,
                created_at=now,
                updated_at=now,
            )
            session.add(campaign)
            session.flush()
            session.add(
                BriefVersionRow(
                    campaign_id=campaign_id,
                    version=1,
                    draft_json=_json_text(draft.model_dump(mode="json")),
                    input_hash=draft.input_hash,
                    validation_json=None,
                    ready_json=None,
                    created_at=now,
                )
            )
            session.commit()
        return self.get_campaign(campaign_id)

    def get_campaign(self, campaign_id: str) -> CampaignView:
        with Session(self._engine) as session:
            campaign = session.get(CampaignRow, campaign_id)
            if campaign is None:
                raise WorkflowNotFound("campaign was not found")
            return self._campaign_view(session, campaign)

    def get_current_context(self, campaign_id: str) -> ContextBundle:
        with Session(self._engine) as session:
            campaign = session.get(CampaignRow, campaign_id)
            if campaign is None:
                raise WorkflowNotFound("campaign was not found")
            if campaign.current_context_version is None:
                raise WorkflowInvalidState("campaign has no current ready context")
            context = session.get(ContextRow, campaign.current_context_version)
            if context is None:
                raise WorkflowInvalidState("campaign context is unavailable")
            return ContextBundle.model_validate_json(context.context_json)

    def patch_brief(
        self,
        campaign_id: str,
        patch: CampaignBriefInput,
        *,
        fields_set: set[str],
    ) -> CampaignView:
        with self._write_lock, Session(self._engine) as session:
            campaign = session.get(CampaignRow, campaign_id)
            if campaign is None:
                raise WorkflowNotFound("campaign was not found")
            current = self._brief_row(session, campaign)
            current_draft = CampaignBriefDraft.model_validate_json(current.draft_json)
            values = current_draft.model_dump(
                mode="json", exclude={"campaign_id", "version", "input_hash"}
            )
            updates = patch.model_dump(mode="json", include=fields_set)
            values.update(updates)
            next_version = current.version + 1
            draft = create_draft(
                campaign_id=campaign_id,
                values=CampaignBriefInput.model_validate(values),
                version=next_version,
            )
            now = datetime.now(UTC)
            session.add(
                BriefVersionRow(
                    campaign_id=campaign_id,
                    version=next_version,
                    draft_json=_json_text(draft.model_dump(mode="json")),
                    input_hash=draft.input_hash,
                    validation_json=None,
                    ready_json=None,
                    created_at=now,
                )
            )
            campaign.current_draft_version = next_version
            campaign.current_ready_version = None
            campaign.current_context_version = None
            campaign.current_package_id = None
            campaign.state = CampaignState.DRAFT.value
            campaign.updated_at = now
            session.commit()
        return self.get_campaign(campaign_id)

    def validate_campaign(self, campaign_id: str) -> CampaignView:
        with self._write_lock, Session(self._engine) as session:
            campaign = session.get(CampaignRow, campaign_id)
            if campaign is None:
                raise WorkflowNotFound("campaign was not found")
            row = self._brief_row(session, campaign)
            draft = CampaignBriefDraft.model_validate_json(row.draft_json)
            catalog = self._effective_catalog(session)
            result = validate_and_promote(draft, catalog)
            row.validation_json = _json_text(result.model_dump(mode="json"))
            row.ready_json = (
                _json_text(result.ready_brief.model_dump(mode="json"))
                if result.ready_brief is not None
                else None
            )
            campaign.state = CampaignState(result.status.value).value
            campaign.current_package_id = None
            now = datetime.now(UTC)
            if result.ready_brief is not None:
                context = build_initial_context(result.ready_brief, catalog)
                active_rules, rules_version = self._active_rule_payloads(session)
                context = apply_active_rules(
                    context,
                    active_rules=active_rules,
                    rules_version=rules_version,
                )
                if session.get(ContextRow, context.context_version) is None:
                    session.add(
                        ContextRow(
                            context_version=context.context_version,
                            campaign_id=campaign_id,
                            brief_version=row.version,
                            context_json=_json_text(context.model_dump(mode="json")),
                            created_at=now,
                        )
                    )
                campaign.current_ready_version = row.version
                campaign.current_context_version = context.context_version
            else:
                campaign.current_ready_version = None
                campaign.current_context_version = None
            campaign.updated_at = now
            session.commit()
        return self.get_campaign(campaign_id)

    def next_run_iteration(self, campaign_id: str, operation: str = "initial") -> int:
        with Session(self._engine) as session:
            iterations = list(
                session.scalars(
                    select(RunRow.iteration).where(
                        RunRow.campaign_id == campaign_id,
                        RunRow.operation == operation,
                    )
                )
            )
            return max(iterations, default=0) + 1

    def create_live_run(
        self,
        *,
        run_id: str,
        campaign_id: str,
        operation: str,
        iteration: int,
        task_id: str,
        project_id: str,
        context_version: str,
        prompt_hash: str,
        skill_content_hash: str,
        tool_inventory_hash: str,
        attempt_id: str | None = None,
        provider: str = "unknown",
        model: str = "unknown",
        provider_profile: str = "unknown",
        request_digest: str | None = None,
    ) -> RunView:
        with self._write_lock, Session(self._engine) as session:
            campaign = session.get(CampaignRow, campaign_id)
            if campaign is None:
                raise WorkflowNotFound("campaign was not found")
            if (
                campaign.current_context_version != context_version
                or campaign.current_ready_version is None
            ):
                raise WorkflowInvalidState("campaign has no current ready context")
            active = session.scalar(
                select(RunRow).where(
                    RunRow.campaign_id == campaign_id,
                    RunRow.status.in_(ACTIVE_RUN_STATUSES),
                )
            )
            if active is not None:
                raise WorkflowConflict("campaign already has an active run")
            now = datetime.now(UTC)
            row = RunRow(
                run_id=run_id,
                campaign_id=campaign_id,
                operation=operation,
                iteration=iteration,
                requested_mode="live_ouroboros",
                mode="live_ouroboros",
                status=RunStatus.QUEUED.value,
                reason_code=None,
                task_id=task_id,
                project_id=project_id,
                context_version=context_version,
                package_id=None,
                prompt_hash=prompt_hash,
                skill_content_hash=skill_content_hash,
                tool_inventory_hash=tool_inventory_hash,
                tool_receipts_json="[]",
                provider_call_ledger_json="{}",
                physical_attempt_count=1,
                final_answer=None,
                created_at=now,
                started_at=None,
                terminal_at=None,
                worker_released_at=None,
            )
            session.add(row)
            # Flush the logical parent before the attempt FK; the models intentionally
            # avoid an ORM relationship because all transitions are explicit.
            session.flush()
            effective_attempt_id = attempt_id or f"attempt_{uuid.uuid4().hex}"
            effective_request_digest = (
                request_digest
                or hashlib.sha256(
                    _json_text(
                        {
                            "run_id": run_id,
                            "campaign_id": campaign_id,
                            "operation": operation,
                            "iteration": iteration,
                            "project_id": project_id,
                            "context_version": context_version,
                            "prompt_hash": prompt_hash,
                            "skill_content_hash": skill_content_hash,
                            "tool_inventory_hash": tool_inventory_hash,
                        }
                    ).encode("utf-8")
                ).hexdigest()
            )
            attempt = RunAttemptRow(
                attempt_id=effective_attempt_id,
                run_id=run_id,
                attempt_number=1,
                task_id=task_id,
                status=RunAttemptStatus.PREPARED.value,
                provider=provider,
                model=model,
                provider_profile=provider_profile,
                request_digest=effective_request_digest,
                context_digest=context_version,
                outcome="PENDING",
                reason_code=None,
                failure_kind="",
                retry_allowed=False,
                tool_receipts_json="[]",
                provider_call_ledger_json="{}",
                usage_status="UNKNOWN",
                draft_present=False,
                result_present=False,
                created_at=now,
                started_at=None,
                terminal_at=None,
                released_at=None,
            )
            session.add(attempt)
            campaign.state = CampaignState.QUEUED.value
            if operation == Operation.INITIAL.value:
                campaign.current_package_id = None
            campaign.updated_at = now
            self._add_run_event(
                session,
                run_id=run_id,
                event_key="run.accepted",
                event_type="run.accepted",
                data={"mode": "live_ouroboros", "operation": operation, "iteration": iteration},
                created_at=now,
            )
            session.commit()
            return self._run_view(row, (self._attempt_view(attempt),))

    def prepare_retry_attempt(
        self,
        run_id: str,
        *,
        attempt_id: str,
        task_id: str,
        request_digest: str,
    ) -> RunAttemptView:
        """Create the only permitted second attempt after a proven first release."""

        with self._write_lock, Session(self._engine) as session:
            run = session.get(RunRow, run_id)
            if run is None:
                raise WorkflowNotFound("run was not found")
            attempts = list(
                session.scalars(
                    select(RunAttemptRow)
                    .where(RunAttemptRow.run_id == run_id)
                    .order_by(RunAttemptRow.attempt_number)
                )
            )
            if len(attempts) == 2:
                existing = attempts[1]
                if (
                    existing.attempt_id == attempt_id
                    and existing.task_id == task_id
                    and existing.request_digest == request_digest
                ):
                    return self._attempt_view(existing)
                raise WorkflowConflict("second physical attempt was already prepared")
            if len(attempts) != 1:
                raise WorkflowInvalidState("retry requires exactly one prior attempt")
            first = attempts[0]
            if run.status not in ACTIVE_RUN_STATUSES:
                raise WorkflowInvalidState("retry requires an active logical run")
            if (
                first.status != RunAttemptStatus.RELEASED.value
                or first.released_at is None
                or not first.retry_allowed
                or first.draft_present
                or first.result_present
            ):
                raise WorkflowInvalidState("first attempt is not safely retryable")
            if request_digest != first.request_digest:
                raise WorkflowConflict("retry request digest differs from the first attempt")
            now = datetime.now(UTC)
            second = RunAttemptRow(
                attempt_id=attempt_id,
                run_id=run_id,
                attempt_number=2,
                task_id=task_id,
                status=RunAttemptStatus.PREPARED.value,
                provider=first.provider,
                model=first.model,
                provider_profile=first.provider_profile,
                request_digest=first.request_digest,
                context_digest=first.context_digest,
                outcome="PENDING",
                reason_code=None,
                failure_kind="",
                retry_allowed=False,
                tool_receipts_json="[]",
                provider_call_ledger_json="{}",
                usage_status="UNKNOWN",
                draft_present=False,
                result_present=False,
                created_at=now,
                started_at=None,
                terminal_at=None,
                released_at=None,
            )
            session.add(second)
            run.task_id = task_id
            run.physical_attempt_count = 2
            self._add_run_event(
                session,
                run_id=run_id,
                event_key="run.retry_scheduled",
                event_type="run.retry_scheduled",
                data={"attempt_number": 2},
                created_at=now,
            )
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                concurrent = session.scalar(
                    select(RunAttemptRow).where(
                        RunAttemptRow.run_id == run_id,
                        RunAttemptRow.attempt_number == 2,
                    )
                )
                if concurrent is not None and concurrent.request_digest == request_digest:
                    return self._attempt_view(concurrent)
                raise WorkflowConflict(
                    "second physical attempt was concurrently prepared differently"
                ) from exc
            return self._attempt_view(second)

    def claim_attempt_submission(self, attempt_id: str) -> bool:
        """Atomically grant exactly one coordinator permission to submit an attempt."""

        with Session(self._engine) as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(RunAttemptRow)
                    .where(
                        RunAttemptRow.attempt_id == attempt_id,
                        RunAttemptRow.status == RunAttemptStatus.PREPARED.value,
                    )
                    .values(status=RunAttemptStatus.SUBMITTING.value)
                ),
            )
            session.commit()
            return result.rowcount == 1

    def mark_attempt_started(self, attempt_id: str) -> RunAttemptView:
        with self._write_lock, Session(self._engine) as session:
            attempt = session.get(RunAttemptRow, attempt_id)
            if attempt is None:
                raise WorkflowNotFound("run attempt was not found")
            if attempt.status in {
                RunAttemptStatus.PREPARED.value,
                RunAttemptStatus.SUBMITTING.value,
            }:
                attempt.status = RunAttemptStatus.RUNNING.value
                attempt.started_at = attempt.started_at or datetime.now(UTC)
                session.commit()
            return self._attempt_view(attempt)

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        outcome: str,
        reason_code: str,
        failure_kind: str,
        retry_allowed: bool,
        tool_receipts: list[str],
        provider_call_ledger: dict[str, Any],
        usage_status: str,
        draft_present: bool,
        result_present: bool,
        released: bool,
    ) -> RunAttemptView:
        if usage_status not in {"EXACT", "UNKNOWN"}:
            raise ValueError("attempt usage status is invalid")
        with self._write_lock, Session(self._engine) as session:
            attempt = session.get(RunAttemptRow, attempt_id)
            if attempt is None:
                raise WorkflowNotFound("run attempt was not found")
            if attempt.status == RunAttemptStatus.RELEASED.value:
                return self._attempt_view(attempt)
            now = datetime.now(UTC)
            attempt.status = (
                RunAttemptStatus.RELEASED.value if released else RunAttemptStatus.TERMINAL.value
            )
            attempt.outcome = outcome
            attempt.reason_code = reason_code
            attempt.failure_kind = failure_kind[:64]
            attempt.retry_allowed = (
                retry_allowed and released and not draft_present and not result_present
            )
            attempt.tool_receipts_json = _json_text(tool_receipts)
            attempt.provider_call_ledger_json = _json_text(provider_call_ledger)
            attempt.usage_status = usage_status
            attempt.draft_present = draft_present
            attempt.result_present = result_present
            attempt.terminal_at = attempt.terminal_at or now
            if released:
                attempt.released_at = attempt.released_at or now
            session.commit()
            return self._attempt_view(attempt)

    def run_attempts(self, run_id: str) -> tuple[RunAttemptView, ...]:
        with Session(self._engine) as session:
            if session.get(RunRow, run_id) is None:
                raise WorkflowNotFound("run was not found")
            rows = list(
                session.scalars(
                    select(RunAttemptRow)
                    .where(RunAttemptRow.run_id == run_id)
                    .order_by(RunAttemptRow.attempt_number)
                )
            )
            return tuple(self._attempt_view(row) for row in rows)

    def mark_run_started(self, run_id: str) -> RunView:
        with self._write_lock, Session(self._engine) as session:
            row = session.get(RunRow, run_id)
            if row is None:
                raise WorkflowNotFound("run was not found")
            if row.status in TERMINAL_RUN_STATUSES:
                return self._run_view(row, self._attempt_views(session, run_id))
            now = datetime.now(UTC)
            row.status = RunStatus.RUNNING.value
            row.started_at = row.started_at or now
            campaign = session.get(CampaignRow, row.campaign_id)
            if campaign is not None:
                campaign.state = CampaignState.RUNNING.value
                campaign.updated_at = now
            self._add_run_event(
                session,
                run_id=run_id,
                event_key="run.started",
                event_type="run.started",
                data={"task_id": row.task_id or ""},
                created_at=now,
            )
            session.commit()
            return self._run_view(row, self._attempt_views(session, run_id))

    def append_run_event(
        self,
        run_id: str,
        *,
        event_key: str,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        with self._write_lock, Session(self._engine) as session:
            if session.get(RunRow, run_id) is None:
                raise WorkflowNotFound("run was not found")
            self._add_run_event(
                session,
                run_id=run_id,
                event_key=event_key,
                event_type=event_type,
                data=data,
                created_at=datetime.now(UTC),
            )
            session.commit()

    def request_run_cancel(self, run_id: str) -> RunView:
        with self._write_lock, Session(self._engine) as session:
            row = session.get(RunRow, run_id)
            if row is None:
                raise WorkflowNotFound("run was not found")
            if row.status in TERMINAL_RUN_STATUSES:
                return self._run_view(row, self._attempt_views(session, run_id))
            now = datetime.now(UTC)
            row.status = RunStatus.CANCEL_REQUESTED.value
            self._add_run_event(
                session,
                run_id=run_id,
                event_key="run.cancel_requested",
                event_type="run.stage",
                data={"stage": "cancel_requested"},
                created_at=now,
            )
            session.commit()
            return self._run_view(row, self._attempt_views(session, run_id))

    def finish_run(
        self,
        run_id: str,
        *,
        status: RunStatus,
        reason_code: str | None,
        mode: str,
        tool_receipts: list[str],
        provider_call_ledger: dict[str, Any],
        final_answer: str | None,
    ) -> RunView:
        if status.value not in TERMINAL_RUN_STATUSES:
            raise ValueError("finish_run requires a terminal status")
        with self._write_lock, Session(self._engine) as session:
            row = session.get(RunRow, run_id)
            if row is None:
                raise WorkflowNotFound("run was not found")
            if row.status in TERMINAL_RUN_STATUSES:
                return self._run_view(row, self._attempt_views(session, run_id))
            campaign = session.get(CampaignRow, row.campaign_id)
            now = datetime.now(UTC)
            effective_status = status
            effective_reason = reason_code
            result_package = (
                session.scalar(
                    select(PackageRow).where(
                        PackageRow.campaign_id == row.campaign_id,
                        PackageRow.context_version == row.context_version,
                    )
                )
                if row.operation != Operation.RULE_PROPOSAL.value
                else None
            )
            result_proposal = (
                session.scalar(
                    select(RuleProposalRow).where(
                        RuleProposalRow.campaign_id == row.campaign_id,
                        RuleProposalRow.context_version == row.context_version,
                        RuleProposalRow.status.in_(
                            {
                                RuleProposalStatus.READY_FOR_APPROVAL.value,
                                RuleProposalStatus.APPROVED.value,
                            }
                        ),
                    )
                )
                if row.operation == Operation.RULE_PROPOSAL.value
                else None
            )
            result_persisted = result_proposal is not None or result_package is not None
            if status in {RunStatus.COMPLETED, RunStatus.COMPLETED_FALLBACK} and (
                campaign is None or not result_persisted
            ):
                effective_status = RunStatus.FAILED
                effective_reason = "DRAFT_NOT_PERSISTED"
            row.status = effective_status.value
            row.mode = mode
            row.reason_code = effective_reason
            row.tool_receipts_json = _json_text(tool_receipts)
            row.provider_call_ledger_json = _json_text(provider_call_ledger)
            row.final_answer = final_answer
            row.package_id = result_package.package_id if result_package is not None else None
            row.terminal_at = now
            if campaign is not None:
                if effective_status is RunStatus.FAILED:
                    campaign.state = CampaignState.FAILED.value
                elif effective_status is RunStatus.CANCELLED:
                    campaign.state = CampaignState.CANCELLED.value
                elif row.operation == Operation.RULE_PROPOSAL.value and result_proposal is not None:
                    campaign.state = CampaignState.REVIEW_REQUIRED.value
                elif (
                    effective_status
                    in {
                        RunStatus.COMPLETED,
                        RunStatus.COMPLETED_FALLBACK,
                    }
                    and result_package is not None
                ):
                    report = QualityReport.model_validate_json(result_package.report_json)
                    campaign.state = (
                        CampaignState.APPROVABLE.value
                        if report.approvable
                        else CampaignState.REVIEW_REQUIRED.value
                    )
                campaign.updated_at = now
            self._add_run_event(
                session,
                run_id=run_id,
                event_key="run.terminal",
                event_type="run.terminal",
                data={
                    "status": effective_status.value,
                    "reason_code": effective_reason or "",
                    "mode": mode,
                    "package_id": row.package_id or "",
                },
                created_at=now,
            )
            session.commit()
            return self._run_view(row, self._attempt_views(session, run_id))

    def mark_worker_released(self, run_id: str) -> RunView:
        with self._write_lock, Session(self._engine) as session:
            row = session.get(RunRow, run_id)
            if row is None:
                raise WorkflowNotFound("run was not found")
            if row.worker_released_at is None:
                now = datetime.now(UTC)
                row.worker_released_at = now
                self._add_run_event(
                    session,
                    run_id=run_id,
                    event_key="run.worker_released",
                    event_type="run.stage",
                    data={"stage": "worker_released"},
                    created_at=now,
                )
                session.commit()
            return self._run_view(row, self._attempt_views(session, run_id))

    def get_run(self, run_id: str) -> RunView:
        with Session(self._engine) as session:
            row = session.get(RunRow, run_id)
            if row is None:
                raise WorkflowNotFound("run was not found")
            return self._run_view(row, self._attempt_views(session, run_id))

    def active_runs(self) -> list[RunView]:
        with Session(self._engine) as session:
            rows = list(
                session.scalars(
                    select(RunRow)
                    .where(RunRow.status.in_(ACTIVE_RUN_STATUSES))
                    .order_by(RunRow.created_at)
                )
            )
            return [self._run_view(row, self._attempt_views(session, row.run_id)) for row in rows]

    def active_rule_state(self) -> tuple[tuple[str, ...], str]:
        with Session(self._engine) as session:
            pointer = self._rule_pointer(session)
            raw_ids = json.loads(pointer.active_rule_version_ids_json)
            if not isinstance(raw_ids, list) or not all(isinstance(item, str) for item in raw_ids):
                raise WorkflowInvalidState("active rule pointer is malformed")
            return tuple(raw_ids), pointer.rules_version

    def run_events(self, run_id: str, *, after_id: int = 0) -> list[RunEventView]:
        with Session(self._engine) as session:
            if session.get(RunRow, run_id) is None:
                raise WorkflowNotFound("run was not found")
            rows = list(
                session.scalars(
                    select(RunEventRow)
                    .where(
                        RunEventRow.run_id == run_id,
                        RunEventRow.event_id > after_id,
                    )
                    .order_by(RunEventRow.event_id)
                )
            )
            return [self._run_event_view(row) for row in rows]

    def run_deterministic(self, campaign_id: str) -> PackageView:
        with self._write_lock, Session(self._engine) as session:
            campaign = session.get(CampaignRow, campaign_id)
            if campaign is None:
                raise WorkflowNotFound("campaign was not found")
            if campaign.current_context_version is None or campaign.current_ready_version is None:
                raise WorkflowInvalidState("campaign has no current ready brief")
            context_row = session.get(ContextRow, campaign.current_context_version)
            if context_row is None:
                raise WorkflowInvalidState("campaign context is unavailable")
            context = ContextBundle.model_validate_json(context_row.context_json)
            bundle = build_deterministic_bundle(context)
            report = evaluate_bundle(bundle, context)
            email_html = render_email_html(bundle.email) if bundle.email is not None else ""
            versions = list(
                session.scalars(
                    select(PackageRow.version).where(PackageRow.campaign_id == campaign_id)
                )
            )
            version = max(versions, default=0) + 1
            package_id = f"package_{uuid.uuid4().hex}"
            now = datetime.now(UTC)
            row = PackageRow(
                package_id=package_id,
                campaign_id=campaign_id,
                version=version,
                mode="deterministic_template",
                context_version=context.context_version,
                package_hash=report.package_hash,
                bundle_json=_json_text(bundle.model_dump(mode="json")),
                report_json=_json_text(report.model_dump(mode="json")),
                email_html=email_html,
                created_at=now,
            )
            session.add(row)
            campaign.current_package_id = package_id
            campaign.state = (
                CampaignState.APPROVABLE.value
                if report.approvable
                else CampaignState.REVIEW_REQUIRED.value
            )
            campaign.updated_at = now
            session.commit()
            return self._package_view(row)

    def get_package(self, package_id: str) -> PackageView:
        with Session(self._engine) as session:
            row = session.get(PackageRow, package_id)
            if row is None:
                raise WorkflowNotFound("package was not found")
            return self._package_view(row)

    def create_feedback(
        self,
        package_id: str,
        request: FeedbackCreateRequest,
        *,
        author_id: str,
    ) -> FeedbackView:
        with self._write_lock, Session(self._engine) as session:
            package = session.get(PackageRow, package_id)
            if package is None:
                raise WorkflowNotFound("package was not found")
            campaign = session.get(CampaignRow, package.campaign_id)
            if campaign is None or campaign.current_package_id != package_id:
                raise WorkflowInvalidState("feedback requires the current package version")
            context_row = session.get(ContextRow, package.context_version)
            if context_row is None:
                raise WorkflowInvalidState("package context is unavailable")
            feedback_id = f"feedback_{uuid.uuid4().hex}"
            now = datetime.now(UTC)
            row = FeedbackRow(
                feedback_id=feedback_id,
                campaign_id=package.campaign_id,
                package_id=package.package_id,
                package_version=package.version,
                package_hash=package.package_hash,
                artifact_path=request.artifact_path,
                comment=request.comment,
                scope=request.scope.value,
                author_id=author_id,
                author_role=request.author_role.value,
                created_at=now,
            )
            view = self._feedback_view(row)
            try:
                build_revision_context(
                    base_context=ContextBundle.model_validate_json(context_row.context_json),
                    base_bundle=CommunicationBundle.model_validate_json(package.bundle_json),
                    feedback=view,
                )
            except RevisionError as exc:
                raise WorkflowInvalidState(str(exc)) from exc
            session.add(row)
            campaign.state = CampaignState.REVIEW_REQUIRED.value
            campaign.updated_at = now
            session.commit()
            return view

    def get_feedback(self, feedback_id: str) -> FeedbackView:
        with Session(self._engine) as session:
            row = session.get(FeedbackRow, feedback_id)
            if row is None:
                raise WorkflowNotFound("feedback was not found")
            return self._feedback_view(row)

    def prepare_revision_context(self, package_id: str, feedback_id: str) -> ContextBundle:
        with self._write_lock, Session(self._engine) as session:
            package = session.get(PackageRow, package_id)
            feedback = session.get(FeedbackRow, feedback_id)
            if package is None:
                raise WorkflowNotFound("package was not found")
            if feedback is None:
                raise WorkflowNotFound("feedback was not found")
            campaign = session.get(CampaignRow, package.campaign_id)
            if (
                campaign is None
                or campaign.current_package_id != package_id
                or feedback.package_id != package_id
            ):
                raise WorkflowInvalidState("revision base or feedback is no longer current")
            base_context_row = session.get(ContextRow, package.context_version)
            if base_context_row is None:
                raise WorkflowInvalidState("revision base context is unavailable")
            try:
                context = build_revision_context(
                    base_context=ContextBundle.model_validate_json(base_context_row.context_json),
                    base_bundle=self._package_view(package).bundle,
                    feedback=self._feedback_view(feedback),
                )
            except RevisionError as exc:
                if exc.code == "STALE_BASE_PACKAGE":
                    raise WorkflowConflict(str(exc)) from exc
                raise WorkflowInvalidState(str(exc)) from exc
            if session.get(ContextRow, context.context_version) is None:
                session.add(
                    ContextRow(
                        context_version=context.context_version,
                        campaign_id=package.campaign_id,
                        brief_version=base_context_row.brief_version,
                        context_json=_json_text(context.model_dump(mode="json")),
                        created_at=datetime.now(UTC),
                    )
                )
            campaign.current_context_version = context.context_version
            campaign.state = CampaignState.REVIEW_REQUIRED.value
            campaign.updated_at = datetime.now(UTC)
            session.commit()
            return context

    def run_deterministic_revision(self, package_id: str, feedback_id: str) -> PackageView:
        context = self.prepare_revision_context(package_id, feedback_id)
        try:
            patch = build_deterministic_patch(context)
        except RevisionError as exc:
            raise WorkflowInvalidState(str(exc)) from exc
        with self._write_lock, Session(self._engine) as session:
            processing, new_package_id = self._persist_revision(
                session,
                context=context,
                patch=patch,
                mode="deterministic_template",
                saved_at=datetime.now(UTC),
            )
            if processing.blockers or new_package_id is None:
                raise WorkflowInvalidState(
                    ",".join(processing.blockers) or "revision did not create a package"
                )
            session.commit()
        return self.get_package(new_package_id)

    def get_package_diff(self, package_id: str) -> PackageDiffView:
        with Session(self._engine) as session:
            row = session.scalar(
                select(PackageDiffRow).where(PackageDiffRow.to_package_id == package_id)
            )
            if row is None:
                raise WorkflowNotFound("package diff was not found")
            return self._package_diff_view(row)

    def prepare_rule_proposal_context(
        self,
        feedback_id: str,
        selected_scope: RuleScope,
    ) -> ContextBundle:
        with self._write_lock, Session(self._engine) as session:
            feedback = session.get(FeedbackRow, feedback_id)
            if feedback is None:
                raise WorkflowNotFound("feedback was not found")
            campaign = session.get(CampaignRow, feedback.campaign_id)
            if campaign is None or campaign.current_package_id is None:
                raise WorkflowInvalidState("rule proposal requires a current revised package")
            package = session.get(PackageRow, campaign.current_package_id)
            if package is None:
                raise WorkflowInvalidState("current package is unavailable")
            revision = session.scalar(
                select(PackageDiffRow).where(
                    PackageDiffRow.feedback_id == feedback_id,
                    PackageDiffRow.to_package_id == package.package_id,
                )
            )
            if revision is None:
                raise WorkflowInvalidState("rule proposal requires a saved revision from feedback")
            context_row = session.get(ContextRow, package.context_version)
            if context_row is None:
                raise WorkflowInvalidState("current revised context is unavailable")
            base = ContextBundle.model_validate_json(context_row.context_json)
            active_rules, rules_version = self._active_rule_payloads(session)
            payload = base.model_dump(mode="json")
            feedback_payload = self._feedback_view(feedback).model_dump(mode="json")
            feedback_payload["selected_scope"] = selected_scope.model_dump(mode="json")
            payload.update(
                {
                    "context_version": "0" * 64,
                    "operation": Operation.RULE_PROPOSAL,
                    "active_rules": active_rules,
                    "rules_version": rules_version,
                    "previous_package": json.loads(package.bundle_json),
                    "feedback": feedback_payload,
                    "allowed_changed_paths": [],
                    "protected_paths": [],
                    "protected_hashes": {},
                    "output_schema_id": "rule_proposal:1.0",
                }
            )
            payload["context_version"] = hash_value(
                {key: value for key, value in payload.items() if key != "context_version"}
            )
            context = ContextBundle.model_validate(payload)
            if session.get(ContextRow, context.context_version) is None:
                session.add(
                    ContextRow(
                        context_version=context.context_version,
                        campaign_id=campaign.campaign_id,
                        brief_version=context_row.brief_version,
                        context_json=_json_text(context.model_dump(mode="json")),
                        created_at=datetime.now(UTC),
                    )
                )
            campaign.current_context_version = context.context_version
            campaign.state = CampaignState.REVIEW_REQUIRED.value
            campaign.updated_at = datetime.now(UTC)
            session.commit()
            return context

    def run_deterministic_rule_proposal(
        self,
        feedback_id: str,
        selected_scope: RuleScope,
    ) -> RuleProposalView:
        context = self.prepare_rule_proposal_context(feedback_id, selected_scope)
        feedback = self.get_feedback(feedback_id)
        proposal_id = f"proposal_{uuid.uuid4().hex}"
        try:
            proposal = build_deterministic_rule_proposal(
                proposal_id=proposal_id,
                feedback=feedback,
                selected_scope=selected_scope,
                base_rules_version=context.rules_version,
                catalog=self._require_catalog(),
            )
        except ValueError as exc:
            raise WorkflowInvalidState(str(exc)) from exc
        with self._write_lock, Session(self._engine) as session:
            processing = self._persist_rule_proposal(
                session,
                context=context,
                proposal=proposal,
                saved_at=datetime.now(UTC),
            )
            session.commit()
            if processing.blockers:
                raise WorkflowInvalidState(",".join(processing.blockers))
        return self.get_rule_proposal(proposal_id)

    def get_rule_proposal(self, proposal_id: str) -> RuleProposalView:
        with Session(self._engine) as session:
            row = session.get(RuleProposalRow, proposal_id)
            if row is None:
                raise WorkflowNotFound("rule proposal was not found")
            return self._rule_proposal_view(row)

    def operation_result_present(
        self,
        *,
        campaign_id: str,
        operation: str,
        context_version: str,
    ) -> bool:
        with Session(self._engine) as session:
            if operation in {Operation.INITIAL.value, Operation.REVISION.value}:
                return (
                    session.scalar(
                        select(PackageRow).where(
                            PackageRow.campaign_id == campaign_id,
                            PackageRow.context_version == context_version,
                        )
                    )
                    is not None
                )
            return (
                session.scalar(
                    select(RuleProposalRow).where(
                        RuleProposalRow.campaign_id == campaign_id,
                        RuleProposalRow.context_version == context_version,
                        RuleProposalRow.status.in_(
                            {
                                RuleProposalStatus.READY_FOR_APPROVAL.value,
                                RuleProposalStatus.APPROVED.value,
                            }
                        ),
                    )
                )
                is not None
            )

    def run_current_deterministic_operation(self, campaign_id: str) -> None:
        context = self.get_current_context(campaign_id)
        if context.operation is Operation.INITIAL:
            self.run_deterministic(campaign_id)
            return
        if context.operation is Operation.REVISION:
            try:
                patch = build_deterministic_patch(context)
            except RevisionError as exc:
                raise WorkflowInvalidState(str(exc)) from exc
            with self._write_lock, Session(self._engine) as session:
                processing, package_id = self._persist_revision(
                    session,
                    context=context,
                    patch=patch,
                    mode="deterministic_template",
                    saved_at=datetime.now(UTC),
                )
                if processing.blockers or package_id is None:
                    raise WorkflowInvalidState(",".join(processing.blockers))
                session.commit()
            return
        if context.feedback is None:
            raise WorkflowInvalidState("rule proposal context has no feedback")
        feedback_id = str(context.feedback.get("feedback_id") or "")
        selected_scope = RuleScope.model_validate(context.feedback.get("selected_scope"))
        feedback = self.get_feedback(feedback_id)
        proposal = build_deterministic_rule_proposal(
            proposal_id=f"proposal_{uuid.uuid4().hex}",
            feedback=feedback,
            selected_scope=selected_scope,
            base_rules_version=context.rules_version,
            catalog=self._require_catalog(),
        )
        with self._write_lock, Session(self._engine) as session:
            processing = self._persist_rule_proposal(
                session,
                context=context,
                proposal=proposal,
                saved_at=datetime.now(UTC),
            )
            if processing.blockers:
                raise WorkflowInvalidState(",".join(processing.blockers))
            session.commit()

    def approve_rule_proposal(
        self,
        proposal_id: str,
        request: RuleApprovalRequest,
        *,
        actor_id: str,
    ) -> RuleVersionView:
        with self._write_lock, Session(self._engine) as session:
            row = session.get(RuleProposalRow, proposal_id)
            if row is None:
                raise WorkflowNotFound("rule proposal was not found")
            proposal = RuleProposal.model_validate_json(row.proposal_json)
            if request.candidate_rules_version != proposal.candidate_rules_version:
                raise WorkflowConflict("rule candidate hash confirmation does not match")
            existing = session.scalar(
                select(RuleVersionRow).where(
                    RuleVersionRow.proposal_id == proposal_id,
                    RuleVersionRow.status == RuleVersionStatus.APPROVED.value,
                )
            )
            if existing is not None:
                return self._rule_version_view(session, existing)
            tests = [RuleTestResult.model_validate(item) for item in json.loads(row.tests_json)]
            errors = json.loads(row.validation_errors_json)
            if (
                row.status != RuleProposalStatus.READY_FOR_APPROVAL.value
                or errors
                or not tests
                or any(not item.passed for item in tests)
            ):
                raise WorkflowInvalidState("only a fully tested rule proposal can be approved")
            pointer = self._rule_pointer(session)
            active_rules, _ = self._active_rule_payloads(session)
            now = datetime.now(UTC)
            rule_version_id = f"rulev_{uuid.uuid4().hex}"
            rule_id = f"rule_{hashlib.sha256(proposal_id.encode()).hexdigest()[:24]}"
            active_rules.append(
                active_rule_payload(
                    rule_version_id=rule_version_id,
                    proposal=proposal,
                )
            )
            next_rules_version = active_rules_hash(active_rules)
            version = RuleVersionRow(
                rule_version_id=rule_version_id,
                rule_id=rule_id,
                proposal_id=proposal_id,
                status=RuleVersionStatus.APPROVED.value,
                rule_json=row.proposal_json,
                rules_version=next_rules_version,
                previous_rules_version=pointer.rules_version,
                actor_id=actor_id,
                actor_role="human",
                test_only=request.test_only,
                created_at=now,
            )
            session.add(version)
            active_ids = json.loads(pointer.active_rule_version_ids_json)
            if not isinstance(active_ids, list):
                raise WorkflowInvalidState("active rule pointer is invalid")
            pointer.active_rule_version_ids_json = _json_text([*active_ids, rule_version_id])
            pointer.rules_version = next_rules_version
            pointer.updated_at = now
            row.status = RuleProposalStatus.APPROVED.value
            row.actor_id = actor_id
            row.test_only = request.test_only
            row.decision_comment = None
            row.decided_at = now
            session.commit()
            return self._rule_version_view(session, version)

    def reject_rule_proposal(
        self,
        proposal_id: str,
        request: RuleRejectionRequest,
        *,
        actor_id: str,
    ) -> RuleProposalView:
        with self._write_lock, Session(self._engine) as session:
            row = session.get(RuleProposalRow, proposal_id)
            if row is None:
                raise WorkflowNotFound("rule proposal was not found")
            proposal = RuleProposal.model_validate_json(row.proposal_json)
            if request.candidate_rules_version != proposal.candidate_rules_version:
                raise WorkflowConflict("rule candidate hash confirmation does not match")
            if row.status != RuleProposalStatus.READY_FOR_APPROVAL.value:
                raise WorkflowInvalidState("rule proposal is not awaiting a decision")
            row.status = RuleProposalStatus.REJECTED.value
            row.actor_id = actor_id
            row.test_only = request.test_only
            row.decision_comment = request.reason
            row.decided_at = datetime.now(UTC)
            session.commit()
            return self._rule_proposal_view(row)

    def rollback_rule(
        self,
        rule_version_id: str,
        request: RuleRollbackRequest,
        *,
        actor_id: str,
    ) -> RuleVersionView:
        with self._write_lock, Session(self._engine) as session:
            target = session.get(RuleVersionRow, rule_version_id)
            if target is None or target.status != RuleVersionStatus.APPROVED.value:
                raise WorkflowNotFound("approved rule version was not found")
            pointer = self._rule_pointer(session)
            if request.active_rules_version != pointer.rules_version:
                raise WorkflowConflict("active rules version confirmation is stale")
            active_ids = json.loads(pointer.active_rule_version_ids_json)
            if not isinstance(active_ids, list) or rule_version_id not in active_ids:
                raise WorkflowInvalidState("rule version is not active")
            next_ids = [item for item in active_ids if item != rule_version_id]
            next_payloads = self._active_rule_payloads_for_ids(session, next_ids)
            next_rules_version = active_rules_hash(next_payloads)
            now = datetime.now(UTC)
            event = RuleVersionRow(
                rule_version_id=f"rulev_{uuid.uuid4().hex}",
                rule_id=target.rule_id,
                proposal_id=target.proposal_id,
                status=RuleVersionStatus.ROLLED_BACK.value,
                rule_json=target.rule_json,
                rules_version=next_rules_version,
                previous_rules_version=pointer.rules_version,
                actor_id=actor_id,
                actor_role="human",
                test_only=request.test_only,
                created_at=now,
            )
            session.add(event)
            pointer.active_rule_version_ids_json = _json_text(next_ids)
            pointer.rules_version = next_rules_version
            pointer.updated_at = now
            session.commit()
            return self._rule_version_view(session, event)

    def process_agent_draft(
        self,
        session: Session,
        request: DraftSaveRequest,
        *,
        saved_at: datetime,
    ) -> DraftProcessingResult:
        campaign = session.get(CampaignRow, request.campaign_id)
        if campaign is None:
            return DraftProcessingResult(blockers=("CAMPAIGN_NOT_READY",))
        if campaign.current_context_version != request.context_version or campaign.state not in {
            CampaignState.READY.value,
            CampaignState.REVIEW_REQUIRED.value,
            CampaignState.QUEUED.value,
            CampaignState.RUNNING.value,
        }:
            return DraftProcessingResult(blockers=("STALE_CAMPAIGN_CONTEXT",))
        context_row = session.get(ContextRow, request.context_version)
        if context_row is None:
            return DraftProcessingResult(blockers=("CONTEXT_NOT_FOUND",))
        context = ContextBundle.model_validate_json(context_row.context_json)
        if request.operation is Operation.REVISION and isinstance(
            request.draft, CommunicationPatchEnvelope
        ):
            processing, _ = self._persist_revision(
                session,
                context=context,
                patch=request.draft.payload,
                mode="live_ouroboros",
                saved_at=saved_at,
            )
            return processing
        if request.operation is Operation.RULE_PROPOSAL and isinstance(
            request.draft, RuleProposalEnvelope
        ):
            return self._persist_rule_proposal(
                session,
                context=context,
                proposal=request.draft.payload,
                saved_at=saved_at,
            )
        if request.operation is not Operation.INITIAL or not isinstance(
            request.draft, CommunicationBundleEnvelope
        ):
            return DraftProcessingResult(blockers=("OPERATION_NOT_SUPPORTED",))
        bundle = request.draft.payload
        report = evaluate_bundle(bundle, context)
        blockers = tuple(
            dict.fromkeys(
                f"QA_BLOCKER_{finding.check_id}" for finding in report.findings if finding.blocking
            )
        )
        warnings = tuple(
            finding.finding_id
            for finding in report.findings
            if finding.severity is FindingSeverity.WARNING
        )
        if blockers:
            campaign.state = CampaignState.BLOCKED.value
            campaign.updated_at = saved_at
            return DraftProcessingResult(blockers=blockers, warnings=warnings)
        versions = list(
            session.scalars(
                select(PackageRow.version).where(PackageRow.campaign_id == request.campaign_id)
            )
        )
        version = max(versions, default=0) + 1
        package_id = f"package_{uuid.uuid4().hex}"
        session.add(
            PackageRow(
                package_id=package_id,
                campaign_id=request.campaign_id,
                version=version,
                mode="live_ouroboros",
                context_version=context.context_version,
                package_hash=report.package_hash,
                bundle_json=_json_text(bundle.model_dump(mode="json")),
                report_json=_json_text(report.model_dump(mode="json")),
                email_html=render_email_html(bundle.email) if bundle.email is not None else "",
                created_at=saved_at,
            )
        )
        campaign.current_package_id = package_id
        campaign.state = (
            CampaignState.APPROVABLE.value
            if report.approvable and not warnings
            else CampaignState.REVIEW_REQUIRED.value
        )
        campaign.updated_at = saved_at
        return DraftProcessingResult(warnings=warnings)

    def _persist_revision(
        self,
        session: Session,
        *,
        context: ContextBundle,
        patch: Any,
        mode: str,
        saved_at: datetime,
    ) -> tuple[DraftProcessingResult, str | None]:
        campaign = session.get(CampaignRow, context.brief_snapshot.campaign_id)
        if campaign is None or campaign.current_package_id is None:
            return DraftProcessingResult(blockers=("STALE_BASE_PACKAGE",)), None
        base_package = session.get(PackageRow, campaign.current_package_id)
        if base_package is None:
            return DraftProcessingResult(blockers=("STALE_BASE_PACKAGE",)), None
        try:
            result = merge_communication_patch(
                context=context,
                patch=patch,
                current_package_hash=base_package.package_hash,
            )
        except RevisionError as exc:
            return DraftProcessingResult(blockers=(exc.code,)), None
        blockers = tuple(
            dict.fromkeys(
                f"QA_BLOCKER_{finding.check_id}"
                for finding in result.report.findings
                if finding.blocking
            )
        )
        warnings = tuple(
            finding.finding_id
            for finding in result.report.findings
            if finding.severity is FindingSeverity.WARNING
        )
        if blockers:
            campaign.state = CampaignState.REVIEW_REQUIRED.value
            campaign.updated_at = saved_at
            return DraftProcessingResult(blockers=blockers, warnings=warnings), None
        versions = list(
            session.scalars(
                select(PackageRow.version).where(PackageRow.campaign_id == campaign.campaign_id)
            )
        )
        package_id = f"package_{uuid.uuid4().hex}"
        package = PackageRow(
            package_id=package_id,
            campaign_id=campaign.campaign_id,
            version=max(versions, default=0) + 1,
            mode=mode,
            context_version=context.context_version,
            package_hash=result.report.package_hash,
            bundle_json=_json_text(result.bundle.model_dump(mode="json")),
            report_json=_json_text(result.report.model_dump(mode="json")),
            email_html=(
                render_email_html(result.bundle.email) if result.bundle.email is not None else ""
            ),
            created_at=saved_at,
        )
        feedback_id = str((context.feedback or {}).get("feedback_id") or "")
        session.add(package)
        session.flush()
        session.add(
            PackageDiffRow(
                diff_id=f"diff_{uuid.uuid4().hex}",
                campaign_id=campaign.campaign_id,
                feedback_id=feedback_id,
                from_package_id=base_package.package_id,
                from_package_hash=base_package.package_hash,
                to_package_id=package_id,
                to_package_hash=result.report.package_hash,
                changed_paths_json=_json_text(list(result.changed_paths)),
                changes_json=_json_text(
                    [change.model_dump(mode="json") for change in result.changes]
                ),
                protected_paths_json=_json_text(list(context.protected_paths)),
                created_at=saved_at,
            )
        )
        campaign.current_package_id = package_id
        campaign.state = (
            CampaignState.APPROVABLE.value
            if result.report.approvable and not warnings
            else CampaignState.REVIEW_REQUIRED.value
        )
        campaign.updated_at = saved_at
        return DraftProcessingResult(warnings=warnings), package_id

    def _persist_rule_proposal(
        self,
        session: Session,
        *,
        context: ContextBundle,
        proposal: RuleProposal | RuleProposalDraft,
        saved_at: datetime,
    ) -> DraftProcessingResult:
        if context.operation is not Operation.RULE_PROPOSAL or context.feedback is None:
            return DraftProcessingResult(blockers=("RULE_PROPOSAL_CONTEXT_INVALID",))
        feedback_id = str(context.feedback.get("feedback_id") or "")
        feedback = session.get(FeedbackRow, feedback_id)
        if feedback is None or feedback.campaign_id != context.brief_snapshot.campaign_id:
            return DraftProcessingResult(blockers=("RULE_SOURCE_FEEDBACK_MISMATCH",))
        selected_scope_raw = context.feedback.get("selected_scope")
        try:
            selected_scope = RuleScope.model_validate(selected_scope_raw)
        except ValueError:
            return DraftProcessingResult(blockers=("RULE_SCOPE_MISMATCH",))
        pointer = self._rule_pointer(session)
        if isinstance(proposal, RuleProposalDraft):
            try:
                proposal = materialize_rule_proposal_draft(
                    draft=proposal,
                    feedback=self._feedback_view(feedback),
                    selected_scope=selected_scope,
                    base_rules_version=pointer.rules_version,
                    context_version=context.context_version,
                    catalog=self._require_catalog(),
                )
            except ValueError:
                return DraftProcessingResult(blockers=("RULE_DRAFT_CANONICALIZATION_FAILED",))
        validation = validate_rule_proposal(
            proposal=proposal,
            feedback=self._feedback_view(feedback),
            selected_scope=selected_scope,
            current_rules_version=pointer.rules_version,
            catalog=self._require_catalog(),
        )
        if session.get(RuleProposalRow, proposal.proposal_id) is not None:
            return DraftProcessingResult(blockers=("RULE_PROPOSAL_ALREADY_EXISTS",))
        status = (
            RuleProposalStatus.READY_FOR_APPROVAL
            if validation.passed
            else RuleProposalStatus.VALIDATION_FAILED
        )
        session.add(
            RuleProposalRow(
                proposal_id=proposal.proposal_id,
                campaign_id=feedback.campaign_id,
                context_version=context.context_version,
                source_feedback_id=feedback_id,
                selected_scope_json=_json_text(selected_scope.model_dump(mode="json")),
                proposal_json=_json_text(proposal.model_dump(mode="json")),
                status=status.value,
                validation_errors_json=_json_text(list(validation.errors)),
                tests_json=_json_text([test.model_dump(mode="json") for test in validation.tests]),
                actor_id=None,
                test_only=None,
                decision_comment=None,
                created_at=saved_at,
                decided_at=None,
            )
        )
        campaign = session.get(CampaignRow, feedback.campaign_id)
        if campaign is not None:
            campaign.state = CampaignState.REVIEW_REQUIRED.value
            campaign.updated_at = saved_at
        return DraftProcessingResult(blockers=validation.errors)

    def approve_package(
        self,
        package_id: str,
        request: ApprovalRequest,
        *,
        actor_id: str,
    ) -> ApprovalRecord:
        with self._write_lock, Session(self._engine) as session:
            package = session.get(PackageRow, package_id)
            if package is None:
                raise WorkflowNotFound("package was not found")
            campaign = session.get(CampaignRow, package.campaign_id)
            if (
                campaign is None
                or campaign.current_package_id != package_id
                or campaign.current_context_version != package.context_version
                or self._package_has_pending_feedback(session, package_id)
            ):
                raise WorkflowInvalidState("package is no longer the current campaign version")
            if request.package_hash != package.package_hash:
                raise WorkflowConflict("package hash confirmation does not match")
            report = QualityReport.model_validate_json(package.report_json)
            blockers = [finding for finding in report.findings if finding.blocking]
            if blockers or not report.approvable:
                raise WorkflowInvalidState("a package with blockers cannot be approved")
            warning_ids = {
                finding.finding_id
                for finding in report.findings
                if finding.severity is FindingSeverity.WARNING
            }
            acknowledged = set(request.acknowledged_warning_ids)
            if warning_ids:
                if (
                    request.decision is not ApprovalDecision.ACCEPTED_WITH_WARNING
                    or acknowledged != warning_ids
                ):
                    raise WorkflowInvalidState(
                        "every warning requires exact explicit acknowledgement"
                    )
            elif (
                request.decision is not ApprovalDecision.APPROVED
                or request.acknowledged_warning_ids
            ):
                raise WorkflowInvalidState("warning acceptance is invalid when no warnings exist")
            existing = session.scalar(
                select(ApprovalRow).where(ApprovalRow.package_id == package_id)
            )
            if existing is not None:
                return self._approval_record(existing)
            now = datetime.now(UTC)
            approval_payload = {
                "package_id": package_id,
                "package_hash": package.package_hash,
                "decision": request.decision,
                "acknowledged_warning_ids": sorted(acknowledged),
                "actor_id": actor_id,
                "actor_role": "human",
                "test_only": request.test_only,
                "created_at": now,
            }
            row = ApprovalRow(
                approval_id=f"approval_{uuid.uuid4().hex}",
                package_id=package_id,
                package_hash=package.package_hash,
                decision=request.decision.value,
                acknowledged_warning_ids_json=_json_text(sorted(acknowledged)),
                actor_id=actor_id,
                actor_role="human",
                test_only=request.test_only,
                approval_hash=hash_value(approval_payload),
                created_at=now,
            )
            session.add(row)
            campaign.state = request.decision.value
            campaign.updated_at = now
            session.commit()
            return self._approval_record(row)

    def export_package(self, package_id: str) -> ExportRecord:
        with self._write_lock, Session(self._engine) as session:
            package = session.get(PackageRow, package_id)
            if package is None:
                raise WorkflowNotFound("package was not found")
            campaign = session.get(CampaignRow, package.campaign_id)
            if (
                campaign is None
                or campaign.current_package_id != package_id
                or campaign.current_context_version != package.context_version
                or self._package_has_pending_feedback(session, package_id)
                or campaign.state
                not in {
                    CampaignState.APPROVED.value,
                    CampaignState.ACCEPTED_WITH_WARNING.value,
                    CampaignState.EXPORTED.value,
                }
            ):
                raise WorkflowInvalidState("package is no longer the current campaign version")
            approval = session.scalar(
                select(ApprovalRow).where(ApprovalRow.package_id == package_id)
            )
            if approval is None or approval.package_hash != package.package_hash:
                raise WorkflowInvalidState("current exact package has no human approval")
            existing = session.scalar(select(ExportRow).where(ExportRow.package_id == package_id))
            if existing is not None:
                return self._export_record(existing)
            context_row = session.get(ContextRow, package.context_version)
            if context_row is None:
                raise WorkflowInvalidState("package context is unavailable")
            context = ContextBundle.model_validate_json(context_row.context_json)
            brief_row = session.scalar(
                select(BriefVersionRow).where(
                    BriefVersionRow.campaign_id == package.campaign_id,
                    BriefVersionRow.version == context_row.brief_version,
                )
            )
            if brief_row is None or brief_row.ready_json is None:
                raise WorkflowInvalidState("ready brief snapshot is unavailable")
            ready = ReadyCampaignBrief.model_validate_json(brief_row.ready_json)
            bundle = PackageView.model_validate(self._package_view(package)).bundle
            report = QualityReport.model_validate_json(package.report_json)
            run_row = session.scalar(
                select(RunRow)
                .where(RunRow.package_id == package_id)
                .order_by(RunRow.created_at.desc())
            )
            run_view = (
                self._run_view(
                    run_row,
                    self._attempt_views(session, run_row.run_id),
                )
                if run_row is not None
                else None
            )
            diff_row = session.scalar(
                select(PackageDiffRow).where(PackageDiffRow.to_package_id == package.package_id)
            )
            feedback_view: FeedbackView | None = None
            diff_view: PackageDiffView | None = None
            rule_proposal_view: RuleProposalView | None = None
            if diff_row is not None:
                diff_view = self._package_diff_view(diff_row)
                feedback_row = session.get(FeedbackRow, diff_row.feedback_id)
                if feedback_row is not None:
                    feedback_view = self._feedback_view(feedback_row)
                    proposal_row = session.scalar(
                        select(RuleProposalRow).where(
                            RuleProposalRow.source_feedback_id == feedback_row.feedback_id
                        )
                    )
                    if proposal_row is not None:
                        rule_proposal_view = self._rule_proposal_view(proposal_row)
            export_id = f"export_{uuid.uuid4().hex}"
            now = datetime.now(UTC)
            files = self._export_files(
                campaign=campaign,
                package=package,
                approval=approval,
                context=context,
                ready=ready,
                bundle=bundle,
                report=report,
                feedback=feedback_view,
                diff=diff_view,
                rule_proposal=rule_proposal_view,
                run=run_view,
                created_at=now,
            )
            checksums = {name: _sha256_bytes(content) for name, content in sorted(files.items())}
            manifest = {
                "schema_version": "1.0",
                "export_id": export_id,
                "campaign_id": campaign.campaign_id,
                "package_id": package.package_id,
                "package_hash": package.package_hash,
                "approval_hash": approval.approval_hash,
                "context_version": package.context_version,
                "rules_version": context.rules_version,
                "mode": package.mode,
                "synthetic": True,
                "no_send": True,
                "created_at": now,
                "files": checksums,
            }
            files["manifest.json"] = _json_bytes(manifest)
            archive_dir = self._artifacts_dir / "exports"
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_path = archive_dir / f"{export_id}.zip"
            temporary = archive_path.with_suffix(".tmp")
            self._write_zip(temporary, files, now)
            temporary.replace(archive_path)
            archive_hash = _sha256_bytes(archive_path.read_bytes())
            row = ExportRow(
                export_id=export_id,
                package_id=package_id,
                package_hash=package.package_hash,
                approval_hash=approval.approval_hash,
                archive_path=str(archive_path),
                archive_sha256=archive_hash,
                file_count=len(files),
                created_at=now,
            )
            session.add(row)
            campaign.state = CampaignState.EXPORTED.value
            campaign.updated_at = now
            session.commit()
            return self._export_record(row)

    def get_export(self, export_id: str) -> ExportRecord:
        with Session(self._engine) as session:
            row = session.get(ExportRow, export_id)
            if row is None:
                raise WorkflowNotFound("export was not found")
            return self._export_record(row)

    def export_path(self, export_id: str) -> pathlib.Path:
        with Session(self._engine) as session:
            row = session.get(ExportRow, export_id)
            if row is None:
                raise WorkflowNotFound("export was not found")
            path = pathlib.Path(row.archive_path)
            if not path.is_file() or _sha256_bytes(path.read_bytes()) != row.archive_sha256:
                raise WorkflowInvalidState("export artifact is missing or has changed")
            return path

    @staticmethod
    def _run_latency_ms(row: RunRow) -> int | None:
        if row.started_at is None or row.terminal_at is None:
            return None
        return max(0, int((row.terminal_at - row.started_at).total_seconds() * 1_000))

    @staticmethod
    def _nearest_rank(values: list[int], percentile: float) -> int | None:
        if not values:
            return None
        rank = max(1, int(len(values) * percentile + 0.999999))
        return values[min(rank, len(values)) - 1]

    @staticmethod
    def _provider_usage_totals(raw: str) -> tuple[int, float]:
        try:
            ledger = json.loads(raw)
        except json.JSONDecodeError:
            return 0, 0.0
        if not isinstance(ledger, dict):
            return 0, 0.0
        tokens = 0
        cost = 0.0
        for value in ledger.values():
            if not isinstance(value, dict):
                continue
            tokens += int(value.get("prompt_tokens") or 0)
            tokens += int(value.get("completion_tokens") or 0)
            cost += float(value.get("cost_usd") or 0.0)
        return tokens, cost

    def _approval_eligibility(
        self,
        session: Session,
        campaign: CampaignRow,
        package: PackageRow | None,
    ) -> tuple[bool, str | None]:
        if package is None:
            return False, "PACKAGE_UNAVAILABLE"
        if campaign.current_package_id != package.package_id:
            return False, "STALE_PACKAGE"
        if campaign.current_context_version != package.context_version:
            return False, "STALE_CONTEXT"
        if self._package_has_pending_feedback(session, package.package_id):
            return False, "PENDING_FEEDBACK"
        report = QualityReport.model_validate_json(package.report_json)
        if not report.approvable:
            return False, "QA_BLOCKER"
        return True, None

    def _workspace_trace(
        self,
        *,
        campaign: CampaignRow,
        packages: list[PackageRow],
        feedback: list[FeedbackRow],
        diffs: list[PackageDiffRow],
        proposals: list[RuleProposalRow],
        rules: list[RuleVersionRow],
        approvals: list[ApprovalRow],
        exports: list[ExportRow],
        runs: list[RunRow],
        run_events: list[RunEventRow],
    ) -> list[SafeTraceEventView]:
        events = [
            SafeTraceEventView(
                event_id=f"campaign_{campaign.campaign_id}",
                event_type="campaign.created",
                label="Кампания создана из синтетического брифа",
                created_at=campaign.created_at,
                mode="validation_only",
            )
        ]
        events.extend(
            SafeTraceEventView(
                event_id=f"package_{row.package_id}",
                event_type="package.version_created",
                label=f"Создан комплект v{row.version}; проверка качества сохранена",
                created_at=row.created_at,
                mode=row.mode,
            )
            for row in packages
        )
        events.extend(
            SafeTraceEventView(
                event_id=f"feedback_{row.feedback_id}",
                event_type="feedback.saved",
                label=f"Замечание сохранено для {row.artifact_path}",
                created_at=row.created_at,
            )
            for row in feedback
        )
        events.extend(
            SafeTraceEventView(
                event_id=f"diff_{row.diff_id}",
                event_type="package.diff_created",
                label="Точечная доработка объединена и повторно проверена",
                created_at=row.created_at,
                mode="deterministic_template",
            )
            for row in diffs
        )
        events.extend(
            SafeTraceEventView(
                event_id=f"proposal_{row.proposal_id}",
                event_type="rule.tested",
                label="Предложение правила проверено",
                created_at=row.created_at,
                mode="deterministic_template",
            )
            for row in proposals
        )
        events.extend(
            SafeTraceEventView(
                event_id=f"rule_{row.rule_version_id}",
                event_type="rule.version_recorded",
                label="Версия правила сохранена",
                created_at=row.created_at,
            )
            for row in rules
        )
        events.extend(
            SafeTraceEventView(
                event_id=f"approval_{row.approval_id}",
                event_type="package.approved",
                label=(
                    "Комплект утверждён тестовым участником"
                    if row.test_only
                    else "Комплект утверждён человеком"
                ),
                created_at=row.created_at,
            )
            for row in approvals
        )
        events.extend(
            SafeTraceEventView(
                event_id=f"export_{row.export_id}",
                event_type="export.ready",
                label="Проверяемый ZIP-экспорт готов",
                created_at=row.created_at,
            )
            for row in exports
        )
        run_modes = {row.run_id: row.mode for row in runs}
        events.extend(
            SafeTraceEventView(
                event_id=f"run_event_{row.event_id}",
                event_type=row.event_type,
                label=self._safe_run_event_label(row.event_type),
                created_at=row.created_at,
                mode=run_modes.get(row.run_id),
            )
            for row in run_events
        )
        return sorted(events, key=lambda item: (item.created_at, item.event_id))

    @staticmethod
    def _operation_presentation(
        runs: list[RunRow],
        run_events: list[RunEventRow],
    ) -> OperationPresentationView | None:
        if not runs:
            return None
        run = runs[-1]
        status = RunStatus(run.status)
        latest_event = next(
            (event for event in reversed(run_events) if event.run_id == run.run_id),
            None,
        )
        stage, stage_label = WorkflowStore._operation_stage(status, latest_event)
        return OperationPresentationView(
            run_id=run.run_id,
            operation=run.operation,
            status=status,
            mode=run.mode,
            active=run.status in ACTIVE_RUN_STATUSES,
            title={
                Operation.INITIAL.value: "Ouroboros создаёт комплект",
                Operation.REVISION.value: "Ouroboros создаёт точечную версию",
                Operation.RULE_PROPOSAL.value: "Ouroboros формирует проект правила",
            }[run.operation],
            stage=stage,
            stage_label=stage_label,
            attempt_number=min(max(run.physical_attempt_count, 1), 2),
            elapsed_from=run.started_at or run.created_at,
            reason_code=run.reason_code,
        )

    @staticmethod
    def _operation_stage(
        status: RunStatus,
        event: RunEventRow | None,
    ) -> tuple[str, str]:
        terminal = {
            RunStatus.COMPLETED: ("completed", "Результат сохранён"),
            RunStatus.COMPLETED_FALLBACK: ("completed", "Результат сохранён"),
            RunStatus.FAILED: ("failed", "Операция завершилась с ошибкой"),  # noqa: RUF001
            RunStatus.CANCELLED: ("cancelled", "Операция отменена"),
        }
        if status in terminal:
            return terminal[status]
        if status is RunStatus.CANCEL_REQUESTED:
            return "cancel_requested", "Передаём запрос на отмену"
        if event is None:
            return "queued", "Задача ожидает выполнения"
        if event.event_type == "run.accepted":
            return "accepted", "Запуск принят, задача ожидает выполнения"
        if event.event_type == "run.retry_scheduled":
            return "retry_scheduled", "Временный сбой, готовим попытку 2 из 2"
        if event.event_type == "run.task_bound":
            return "task_bound", "Задача Ouroboros связана с операцией"  # noqa: RUF001
        if event.event_type == "run.started":
            return "running", "Ouroboros выполняет задачу"
        if event.event_type == "run.qa_completed":
            return "quality_check", "Проверяем сохранённый результат"
        try:
            data = json.loads(event.data_json)
        except (json.JSONDecodeError, TypeError):
            data = {}
        if event.event_type in {"run.tool_started", "run.tool_completed"}:
            tool = str(data.get("tool") or "")
            if tool.endswith("cf_context_get"):
                return "context", "Получаем разрешённый контекст"
            if tool.endswith("cf_draft_save"):
                return "save", "Сохраняем и проверяем результат"
        if event.event_type == "run.stage":
            raw_stage = str(data.get("stage") or "")
            mapped = {
                "cancel_requested": ("cancel_requested", "Передаём запрос на отмену"),
                "context_version_bound": ("context_bound", "Контекст операции зафиксирован"),
                "startup_reconcile": ("reconcile", "Восстанавливаем состояние операции"),
                "ambiguous_submit_recovered": (
                    "submission_check",
                    "Проверяем исход предыдущей отправки задачи",
                ),
                "retry_backoff": ("retry_wait", "Ожидаем повторную попытку"),
                "task_started": ("running", "Ouroboros выполняет задачу"),
                "safety_check": ("safety_check", "Проверяем безопасность результата"),
                "llm_usage": ("running", "Ouroboros выполняет задачу"),
            }
            if raw_stage in mapped:
                return mapped[raw_stage]
        return "running", "Ouroboros выполняет задачу"

    @staticmethod
    def _safe_run_event_label(event_type: str) -> str:
        return {
            "run.accepted": "Запуск принят приложением",
            "run.started": "Запуск Ouroboros начат",
            "run.task_bound": "Идентификатор задачи связан с операцией",  # noqa: RUF001
            "run.tool_started": "Вызван разрешённый инструмент MCP",
            "run.tool_completed": "Разрешённый инструмент MCP завершён",
            "run.qa_completed": "Детерминированная проверка качества завершена",
            "run.retry_scheduled": "Временный сбой — подготовлен повторный запрос (2 из 2)",
            "run.terminal": "Запуск достиг итогового состояния",
            "run.stage": "Стадия запуска обновлена",
        }.get(event_type, "Безопасное событие запуска")

    @staticmethod
    def _feedback_view(row: FeedbackRow) -> FeedbackView:
        return FeedbackView(
            feedback_id=row.feedback_id,
            campaign_id=row.campaign_id,
            package_id=row.package_id,
            package_version=row.package_version,
            package_hash=row.package_hash,
            artifact_path=row.artifact_path,
            comment=row.comment,
            scope=row.scope,
            author_id=row.author_id,
            author_role=row.author_role,
            created_at=row.created_at,
        )

    @staticmethod
    def _package_diff_view(row: PackageDiffRow) -> PackageDiffView:
        return PackageDiffView(
            diff_id=row.diff_id,
            campaign_id=row.campaign_id,
            feedback_id=row.feedback_id,
            from_package_id=row.from_package_id,
            from_package_hash=row.from_package_hash,
            to_package_id=row.to_package_id,
            to_package_hash=row.to_package_hash,
            changed_paths=json.loads(row.changed_paths_json),
            changes=json.loads(row.changes_json),
            protected_paths=json.loads(row.protected_paths_json),
            created_at=row.created_at,
        )

    @staticmethod
    def _rule_proposal_view(row: RuleProposalRow) -> RuleProposalView:
        return RuleProposalView(
            proposal_id=row.proposal_id,
            campaign_id=row.campaign_id,
            context_version=row.context_version,
            proposal=RuleProposal.model_validate_json(row.proposal_json),
            status=row.status,
            validation_errors=json.loads(row.validation_errors_json),
            tests=json.loads(row.tests_json),
            actor_id=row.actor_id,
            test_only=row.test_only,
            decision_comment=row.decision_comment,
            created_at=row.created_at,
            decided_at=row.decided_at,
        )

    @staticmethod
    def _rule_pointer(session: Session) -> RuleSetPointerRow:
        pointer = session.get(RuleSetPointerRow, "active")
        if pointer is None:
            raise WorkflowInvalidState("active rule pointer is unavailable")
        return pointer

    def _active_rule_payloads_for_ids(
        self,
        session: Session,
        active_ids: list[str],
    ) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for rule_version_id in active_ids:
            row = session.get(RuleVersionRow, rule_version_id)
            if row is None or row.status != RuleVersionStatus.APPROVED.value:
                raise WorkflowInvalidState("active rule pointer references an invalid version")
            payloads.append(
                active_rule_payload(
                    rule_version_id=rule_version_id,
                    proposal=RuleProposal.model_validate_json(row.rule_json),
                )
            )
        return payloads

    def _active_rule_payloads(self, session: Session) -> tuple[list[dict[str, Any]], str]:
        pointer = self._rule_pointer(session)
        active_ids = json.loads(pointer.active_rule_version_ids_json)
        if not isinstance(active_ids, list) or not all(
            isinstance(item, str) for item in active_ids
        ):
            raise WorkflowInvalidState("active rule pointer is invalid")
        return self._active_rule_payloads_for_ids(session, active_ids), pointer.rules_version

    def _rule_version_view(self, session: Session, row: RuleVersionRow) -> RuleVersionView:
        pointer = self._rule_pointer(session)
        active_ids = json.loads(pointer.active_rule_version_ids_json)
        return RuleVersionView(
            rule_version_id=row.rule_version_id,
            rule_id=row.rule_id,
            proposal_id=row.proposal_id,
            status=row.status,
            rule=RuleProposal.model_validate_json(row.rule_json),
            rules_version=row.rules_version,
            previous_rules_version=row.previous_rules_version,
            active=isinstance(active_ids, list) and row.rule_version_id in active_ids,
            actor_id=row.actor_id,
            actor_role="human",
            test_only=row.test_only,
            created_at=row.created_at,
        )

    @staticmethod
    def _package_has_pending_feedback(session: Session, package_id: str) -> bool:
        feedback_rows = list(
            session.scalars(select(FeedbackRow).where(FeedbackRow.package_id == package_id))
        )
        for feedback in feedback_rows:
            diff = session.scalar(
                select(PackageDiffRow).where(PackageDiffRow.feedback_id == feedback.feedback_id)
            )
            if diff is None:
                return True
        return False

    @staticmethod
    def _add_run_event(
        session: Session,
        *,
        run_id: str,
        event_key: str,
        event_type: str,
        data: dict[str, Any],
        created_at: datetime,
    ) -> None:
        existing = session.scalar(
            select(RunEventRow).where(
                RunEventRow.run_id == run_id,
                RunEventRow.event_key == event_key,
            )
        )
        if existing is None:
            session.add(
                RunEventRow(
                    run_id=run_id,
                    event_key=event_key,
                    event_type=event_type,
                    data_json=_json_text(data),
                    created_at=created_at,
                )
            )

    def _attempt_views(
        self,
        session: Session,
        run_id: str,
    ) -> tuple[RunAttemptView, ...]:
        rows = list(
            session.scalars(
                select(RunAttemptRow)
                .where(RunAttemptRow.run_id == run_id)
                .order_by(RunAttemptRow.attempt_number)
            )
        )
        return tuple(self._attempt_view(row) for row in rows)

    @staticmethod
    def _run_view(
        row: RunRow,
        attempts: tuple[RunAttemptView, ...] = (),
    ) -> RunView:
        ledger = json.loads(row.provider_call_ledger_json)
        if not isinstance(ledger, dict):
            ledger = {}
        receipts = json.loads(row.tool_receipts_json)
        if not isinstance(receipts, list):
            receipts = []
        return RunView(
            run_id=row.run_id,
            campaign_id=row.campaign_id,
            operation=row.operation,
            iteration=row.iteration,
            requested_mode=row.requested_mode,
            mode=row.mode,
            status=RunStatus(row.status),
            reason_code=row.reason_code,
            task_id=row.task_id,
            project_id=row.project_id,
            context_version=row.context_version,
            package_id=row.package_id,
            prompt_hash=row.prompt_hash,
            skill_content_hash=row.skill_content_hash,
            tool_inventory_hash=row.tool_inventory_hash,
            tool_receipts=tuple(str(item) for item in receipts),
            provider_call_ledger={str(key): value for key, value in ledger.items()},
            physical_attempt_count=row.physical_attempt_count,
            attempts=attempts,
            final_answer=row.final_answer,
            created_at=row.created_at,
            started_at=row.started_at,
            terminal_at=row.terminal_at,
            worker_released_at=row.worker_released_at,
        )

    @staticmethod
    def _attempt_view(row: RunAttemptRow) -> RunAttemptView:
        ledger = json.loads(row.provider_call_ledger_json)
        if not isinstance(ledger, dict):
            ledger = {}
        receipts = json.loads(row.tool_receipts_json)
        if not isinstance(receipts, list):
            receipts = []
        return RunAttemptView(
            attempt_id=row.attempt_id,
            run_id=row.run_id,
            attempt_number=row.attempt_number,
            task_id=row.task_id,
            status=RunAttemptStatus(row.status),
            provider=row.provider,
            model=row.model,
            provider_profile=row.provider_profile,
            request_digest=row.request_digest,
            context_digest=row.context_digest,
            outcome=row.outcome,
            reason_code=row.reason_code,
            failure_kind=row.failure_kind,
            retry_allowed=row.retry_allowed,
            tool_receipts=tuple(str(item) for item in receipts),
            provider_call_ledger={str(key): value for key, value in ledger.items()},
            usage_status=row.usage_status,
            draft_present=row.draft_present,
            result_present=row.result_present,
            created_at=row.created_at,
            started_at=row.started_at,
            terminal_at=row.terminal_at,
            released_at=row.released_at,
        )

    @staticmethod
    def _run_event_view(row: RunEventRow) -> RunEventView:
        data = json.loads(row.data_json)
        return RunEventView(
            event_id=row.event_id,
            run_id=row.run_id,
            event_type=row.event_type,
            data={str(key): value for key, value in data.items()} if isinstance(data, dict) else {},
            created_at=row.created_at,
        )

    def _campaign_view(self, session: Session, campaign: CampaignRow) -> CampaignView:
        row = self._brief_row(session, campaign)
        return CampaignView(
            campaign_id=campaign.campaign_id,
            state=CampaignState(campaign.state),
            draft_version=row.version,
            draft=CampaignBriefDraft.model_validate_json(row.draft_json),
            validation=BriefValidationResult.model_validate_json(row.validation_json)
            if row.validation_json
            else None,
            ready_brief=ReadyCampaignBrief.model_validate_json(row.ready_json)
            if row.ready_json
            else None,
            context_version=campaign.current_context_version,
            package_id=campaign.current_package_id,
            created_at=campaign.created_at,
            updated_at=campaign.updated_at,
        )

    def _require_catalog(self) -> SyntheticCatalog:
        if self._catalog is None:
            raise WorkflowInvalidState("workflow store is not initialized")
        return self._catalog

    @staticmethod
    def _brief_row(session: Session, campaign: CampaignRow) -> BriefVersionRow:
        row = session.scalar(
            select(BriefVersionRow).where(
                BriefVersionRow.campaign_id == campaign.campaign_id,
                BriefVersionRow.version == campaign.current_draft_version,
            )
        )
        if row is None:
            raise WorkflowInvalidState("current brief version is unavailable")
        return row

    @staticmethod
    def _package_view(row: PackageRow) -> PackageView:
        return PackageView(
            package_id=row.package_id,
            campaign_id=row.campaign_id,
            package_version=row.version,
            mode=row.mode,
            context_version=row.context_version,
            package_hash=row.package_hash,
            bundle=json.loads(row.bundle_json),
            quality_report=json.loads(row.report_json),
            email_html=row.email_html,
            created_at=row.created_at,
        )

    @staticmethod
    def _approval_record(row: ApprovalRow) -> ApprovalRecord:
        return ApprovalRecord(
            approval_id=row.approval_id,
            package_id=row.package_id,
            package_hash=row.package_hash,
            decision=row.decision,
            acknowledged_warning_ids=json.loads(row.acknowledged_warning_ids_json),
            actor_id=row.actor_id,
            actor_role="human",
            test_only=row.test_only,
            approval_hash=row.approval_hash,
            created_at=row.created_at,
        )

    @staticmethod
    def _export_record(row: ExportRow) -> ExportRecord:
        return ExportRecord(
            export_id=row.export_id,
            package_id=row.package_id,
            package_hash=row.package_hash,
            approval_hash=row.approval_hash,
            archive_sha256=row.archive_sha256,
            file_count=row.file_count,
            created_at=row.created_at,
        )

    @staticmethod
    def _write_zip(path: pathlib.Path, files: dict[str, bytes], created_at: datetime) -> None:
        timestamp = created_at.astimezone(UTC)
        zip_time = (
            max(1980, timestamp.year),
            timestamp.month,
            timestamp.day,
            timestamp.hour,
            timestamp.minute,
            timestamp.second,
        )
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in sorted(files.items()):
                info = zipfile.ZipInfo(name, date_time=zip_time)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100640 << 16
                archive.writestr(info, content)

    @staticmethod
    def _export_files(
        *,
        campaign: CampaignRow,
        package: PackageRow,
        approval: ApprovalRow,
        context: ContextBundle,
        ready: ReadyCampaignBrief,
        bundle: Any,
        report: QualityReport,
        feedback: FeedbackView | None,
        diff: PackageDiffView | None,
        rule_proposal: RuleProposalView | None,
        run: RunView | None,
        created_at: datetime,
    ) -> dict[str, bytes]:
        sms = bundle.sms
        email = bundle.email
        findings_json = [finding.model_dump(mode="json") for finding in report.findings]
        report_html = (
            '<!doctype html><html lang="ru"><meta charset="utf-8">'
            "<title>QA report</title><body><h1>QA report</h1><pre>"
            + html.escape(_json_text(report.model_dump(mode="json"), pretty=True))
            + "</pre></body></html>"
        )
        ledger = run.provider_call_ledger if run is not None else {}
        provider_calls = sum(
            int(value.get("call_count") or 0)
            for value in ledger.values()
            if isinstance(value, dict)
        )
        run_document: dict[str, Any] = {
            "run_id": run.run_id if run is not None else None,
            "mode": package.mode,
            "status": run.status.value if run is not None else "terminal",
            "reason_code": run.reason_code if run is not None else None,
            "provider_calls": provider_calls,
            "physical_attempt_count": run.physical_attempt_count if run is not None else 0,
            "attempts": [
                attempt.model_dump(mode="json", exclude={"provider_call_ledger"})
                for attempt in run.attempts
            ]
            if run is not None
            else [],
            "package_id": package.package_id,
            "package_hash": package.package_hash,
            "created_at": package.created_at,
        }
        files: dict[str, bytes] = {
            "campaign.json": _json_bytes(
                {
                    "campaign_id": campaign.campaign_id,
                    "state": campaign.state,
                    "synthetic": True,
                    "no_send": True,
                }
            ),
            "brief.json": _json_bytes(ready.model_dump(mode="json")),
            "run.json": _json_bytes(run_document),
            "context-manifest.json": _json_bytes(
                {
                    "context_version": context.context_version,
                    "source_manifest": [
                        item.model_dump(mode="json") for item in context.source_manifest
                    ],
                    "content_plan": context.content_plan.model_dump(mode="json"),
                }
            ),
            "fact-card.json": _json_bytes(context.product.model_dump(mode="json")),
            "rules-version.json": _json_bytes(
                {
                    "rules_version": context.rules_version,
                    "active_rules": list(context.active_rules),
                }
            ),
            "sms/message.txt": ((sms.text if sms else "SUPPRESSED") + "\n").encode(),
            "sms/metrics.json": _json_bytes(
                report.sms_metrics.model_dump(mode="json") if report.sms_metrics else {}
            ),
            "email/email.html": (package.email_html + "\n").encode(),
            "email/email.txt": ((email.plain_text if email else "SUPPRESSED") + "\n").encode(),
            "email/content.json": _json_bytes(
                email.model_dump(mode="json") if email else {"suppressed": True}
            ),
            "qa/findings.json": _json_bytes(findings_json),
            "qa/report.html": report_html.encode(),
            "feedback/feedback.json": _json_bytes(
                feedback.model_dump(mode="json") if feedback else []
            ),
            "feedback/diff.json": _json_bytes(diff.model_dump(mode="json") if diff else []),
            "learning/rule-proposal.json": _json_bytes(
                rule_proposal.model_dump(mode="json") if rule_proposal else None
            ),
            "trace/safe-events.jsonl": b"",
            "trace/mcp-calls.jsonl": b"",
            "trace/model-usage.json": _json_bytes(
                {
                    "provider_calls": provider_calls,
                    "mode": package.mode,
                    "provider_call_ledger": ledger,
                    "attempt_usage": [
                        {
                            "attempt_id": attempt.attempt_id,
                            "attempt_number": attempt.attempt_number,
                            "usage_status": attempt.usage_status,
                            "provider_call_ledger": attempt.provider_call_ledger,
                        }
                        for attempt in run.attempts
                    ]
                    if run is not None
                    else [],
                }
            ),
            "README.txt": (
                "SYNTHETIC · NO SEND\n"
                "This archive contains a fictional test campaign. Approval does not mean sending.\n"
                f"Package: {package.package_id}\n"
                f"Approval hash: {approval.approval_hash}\n"
                f"Created: {created_at.isoformat()}\n"
            ).encode(),
        }
        return files
