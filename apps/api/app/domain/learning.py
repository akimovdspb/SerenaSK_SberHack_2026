from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field

from apps.api.app.domain.campaigns import FrozenStrictModel
from apps.api.app.domain.models import (
    Identifier,
    JsonPointer,
    NonEmptyText,
    RuleProposal,
    RuleScope,
    Sha256,
    StrictModel,
)


class FeedbackScope(StrEnum):
    CURRENT_FIELD = "CURRENT_FIELD"
    CURRENT_CHANNEL = "CURRENT_CHANNEL"
    PACKAGE = "PACKAGE"


class FeedbackAuthorRole(StrEnum):
    EDITOR = "editor"
    APPROVER = "approver"


class FeedbackCreateRequest(StrictModel):
    artifact_path: JsonPointer
    comment: NonEmptyText
    scope: FeedbackScope
    author_role: FeedbackAuthorRole


class FeedbackView(FrozenStrictModel):
    feedback_id: Identifier
    campaign_id: Identifier
    package_id: Identifier
    package_version: int = Field(ge=1)
    package_hash: Sha256
    artifact_path: JsonPointer
    comment: NonEmptyText
    scope: FeedbackScope
    author_id: Identifier
    author_role: FeedbackAuthorRole
    created_at: datetime


class RevisionStartRequest(StrictModel):
    feedback_id: Identifier
    mode: Literal["deterministic_template", "live_ouroboros"] = "deterministic_template"


class PackageDiffChange(FrozenStrictModel):
    path: JsonPointer
    before_hash: Sha256
    after_hash: Sha256
    before_preview: str = Field(max_length=300)
    after_preview: str = Field(max_length=300)
    protected: bool = False


class PackageDiffView(FrozenStrictModel):
    diff_id: Identifier
    campaign_id: Identifier
    feedback_id: Identifier
    from_package_id: Identifier
    from_package_hash: Sha256
    to_package_id: Identifier
    to_package_hash: Sha256
    changed_paths: tuple[JsonPointer, ...] = Field(min_length=1, max_length=50)
    changes: tuple[PackageDiffChange, ...] = Field(min_length=1, max_length=50)
    protected_paths: tuple[JsonPointer, ...]
    created_at: datetime


class RuleProposalStartRequest(StrictModel):
    selected_scope: RuleScope
    mode: Literal["deterministic_template", "live_ouroboros"] = "deterministic_template"


class RuleProposalStatus(StrEnum):
    PROPOSED = "PROPOSED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    READY_FOR_APPROVAL = "READY_FOR_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class RuleTestResult(FrozenStrictModel):
    case_id: Identifier
    test_kind: Literal["target", "regression", "out_of_scope"]
    expected_applied: bool
    actual_applied: bool
    passed: bool
    detail: NonEmptyText


class RuleProposalView(FrozenStrictModel):
    proposal_id: Identifier
    campaign_id: Identifier
    context_version: Sha256
    proposal: RuleProposal
    status: RuleProposalStatus
    validation_errors: tuple[Identifier, ...] = Field(default_factory=tuple)
    tests: tuple[RuleTestResult, ...] = Field(default_factory=tuple)
    actor_id: Identifier | None = None
    test_only: bool | None = None
    decision_comment: str | None = Field(default=None, max_length=1_000)
    created_at: datetime
    decided_at: datetime | None = None


class RuleApprovalRequest(StrictModel):
    candidate_rules_version: Sha256
    test_only: bool = False


class RuleRejectionRequest(StrictModel):
    candidate_rules_version: Sha256
    reason: NonEmptyText
    test_only: bool = False


class RuleVersionStatus(StrEnum):
    APPROVED = "APPROVED"
    ROLLED_BACK = "ROLLED_BACK"


class RuleVersionView(FrozenStrictModel):
    rule_version_id: Identifier
    rule_id: Identifier
    proposal_id: Identifier
    status: RuleVersionStatus
    rule: RuleProposal
    rules_version: Sha256
    previous_rules_version: Sha256
    active: bool
    actor_id: Identifier
    actor_role: Literal["human"] = "human"
    test_only: bool
    created_at: datetime


class RuleRollbackRequest(StrictModel):
    active_rules_version: Sha256
    reason: NonEmptyText
    test_only: bool = False
