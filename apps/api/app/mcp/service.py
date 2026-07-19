from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from apps.api.app.domain.campaigns import ContextBundle
from apps.api.app.domain.models import (
    Channel,
    ClaimType,
    CommunicationBundle,
    CommunicationBundleEnvelope,
    CommunicationPatchEnvelope,
    ContextGetRequest,
    ContextToolResult,
    DraftSaveRequest,
    DraftSaveResult,
    RuleProposalEnvelope,
    ToolQuestion,
)
from apps.api.app.sqlite_runtime import create_sqlite_aware_engine


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _apply_sms_segment_bound(
    definitions: dict[str, Any],
    context: ContextBundle,
) -> bool:
    sms_schema = definitions.get("SmsArtifact")
    if not isinstance(sms_schema, dict):
        return False
    sms_properties = sms_schema.get("properties")
    if not isinstance(sms_properties, dict):
        return False
    text_schema = sms_properties.get("text")
    if not isinstance(text_schema, dict):
        return False
    try:
        max_segments = int(context.channel_policies.get("sms", {}).get("max_segments", 3))
    except (TypeError, ValueError):
        return False
    if not 1 <= max_segments <= 10:
        return False

    # Russian campaign copy is UCS-2. A concatenated UCS-2 message carries 67 UTF-16 code units
    # per segment; a one-segment message carries 70. JSON Schema maxLength is a conservative
    # provider-facing guard, while QA12 remains the exact encoding-aware persistence boundary.
    max_code_units = 70 if max_segments == 1 else 67 * max_segments
    current_max = int(text_schema.get("maxLength") or max_code_units)
    text_schema["maxLength"] = min(current_max, max_code_units)
    segment_label = "сегмента" if max_segments == 1 else "сегментов"
    text_schema["description"] = (
        f"Жёсткий предел текущей SMS-политики: не более {max_segments} {segment_label}; для "
        f"русского "
        f"текста UCS-2 используй не более {max_code_units} кодовых единиц UTF-16 и не используй "
        f"эмодзи. {text_schema.get('description', '')}"
    ).strip()
    return True


def _initial_output_schema(context: dict[str, Any]) -> dict[str, Any]:
    schema = copy.deepcopy(CommunicationBundleEnvelope.model_json_schema())
    try:
        typed = ContextBundle.model_validate(context)
    except ValueError:
        return schema

    facts_by_id = {fact.fact_id: fact for fact in typed.facts}
    sms_facts = [
        facts_by_id[fact_id]
        for fact_id in typed.content_plan.fact_ids_for(Channel.SMS)
        if fact_id in facts_by_id
    ]
    email_facts = [
        facts_by_id[fact_id]
        for fact_id in typed.content_plan.fact_ids_for(Channel.EMAIL)
        if fact_id in facts_by_id
    ]
    sms_non_url_count = sum(fact.kind is not ClaimType.URL for fact in sms_facts)
    sms_url_count = len(sms_facts) - sms_non_url_count
    email_non_url_count = sum(fact.kind is not ClaimType.URL for fact in email_facts)
    email_url_count = len(email_facts) - email_non_url_count
    requested = set(typed.brief_snapshot.channels)

    def rendered(channel: Channel) -> bool:
        return channel in requested and (
            not typed.contact_policy.require_channel_consent
            or typed.persona.channel_consent.get(channel, False)
        )

    evidence_count = 0
    allowed_artifact_paths: list[str] = []
    if rendered(Channel.SMS):
        evidence_count += sms_non_url_count + (2 * sms_url_count)
        if sms_facts:
            allowed_artifact_paths.append("/sms/text")
        if sms_url_count:
            allowed_artifact_paths.append("/sms/cta_url")
    section_count = min(4, max(2, email_non_url_count))
    if rendered(Channel.EMAIL):
        evidence_count += (2 * email_non_url_count) + (2 * email_url_count)
        allowed_artifact_paths.extend(
            f"/email/sections/{index}/body" for index in range(section_count)
        )
        if email_facts:
            allowed_artifact_paths.append("/email/plain_text")
        if email_url_count:
            allowed_artifact_paths.append("/email/cta_url")

    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        return schema
    if not _apply_sms_segment_bound(definitions, typed):
        return schema
    email_schema = definitions.get("EmailArtifact")
    section_schema = definitions.get("EmailSection")
    bundle_schema = definitions.get("CommunicationBundle")
    claim_schema = definitions.get("ClaimEvidence")
    if (
        not isinstance(email_schema, dict)
        or not isinstance(section_schema, dict)
        or not isinstance(bundle_schema, dict)
        or not isinstance(claim_schema, dict)
    ):
        return schema
    email_properties = email_schema.get("properties")
    section_properties = section_schema.get("properties")
    bundle_properties = bundle_schema.get("properties")
    claim_properties = claim_schema.get("properties")
    if (
        not isinstance(email_properties, dict)
        or not isinstance(section_properties, dict)
        or not isinstance(bundle_properties, dict)
        or not isinstance(claim_properties, dict)
    ):
        return schema
    sections_schema = email_properties.get("sections")
    evidence_schema = bundle_properties.get("claim_evidence")
    artifact_path_schema = claim_properties.get("artifact_path")
    if (
        not isinstance(sections_schema, dict)
        or not isinstance(evidence_schema, dict)
        or not isinstance(artifact_path_schema, dict)
    ):
        return schema

    summary_schema = bundle_properties.get("summary")
    if not isinstance(summary_schema, dict):
        return schema
    summary_schema["const"] = "Синтетический пакет без отправки."
    summary_schema["description"] = (
        "Для текущего initial-контекста сервер зафиксировал нейтральное описание без выбранных "
        "фактов или URL."
    )

    sections_schema["minItems"] = section_count
    sections_schema["maxItems"] = section_count
    sections_schema["description"] = (
        f"Для текущего initial-контекста подготовь {section_count} смысловые e-mail секции. "
        f"{sections_schema.get('description', '')}"
    ).strip()
    evidence_schema["minItems"] = evidence_count
    evidence_schema["maxItems"] = evidence_count
    evidence_schema["description"] = (
        f"Для текущего initial-контекста число элементов claim_evidence равно {evidence_count}. "
        f"{evidence_schema.get('description', '')}"
    ).strip()
    if allowed_artifact_paths:
        artifact_path_schema["enum"] = allowed_artifact_paths
        artifact_path_schema["description"] = (
            "Для текущего initial-контекста выбери только один из перечисленных точных JSON "
            "Pointer. Индексы sections нулевые и относятся к окончательному массиву секций."
        )
    return schema


def _revision_output_schema(context: dict[str, Any]) -> dict[str, Any]:
    schema = copy.deepcopy(CommunicationPatchEnvelope.model_json_schema())
    try:
        typed = ContextBundle.model_validate(context)
        previous = CommunicationBundle.model_validate(typed.previous_package)
    except ValueError:
        return schema
    if (
        typed.operation.value != "revision"
        or typed.previous_package is None
        or typed.feedback is None
        or not typed.allowed_changed_paths
    ):
        return schema

    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        return schema
    if not _apply_sms_segment_bound(definitions, typed):
        return schema
    patch_schema = definitions.get("CommunicationPatch")
    sms_schema = definitions.get("SmsArtifact")
    email_schema = definitions.get("EmailArtifact")
    section_schema = definitions.get("EmailSection")
    if (
        not isinstance(patch_schema, dict)
        or not isinstance(sms_schema, dict)
        or not isinstance(email_schema, dict)
        or not isinstance(section_schema, dict)
    ):
        return schema
    patch_properties = patch_schema.get("properties")
    sms_properties = sms_schema.get("properties")
    email_properties = email_schema.get("properties")
    section_properties = section_schema.get("properties")
    if (
        not isinstance(patch_properties, dict)
        or not isinstance(sms_properties, dict)
        or not isinstance(email_properties, dict)
        or not isinstance(section_properties, dict)
    ):
        return schema

    previous_json = previous.model_dump(mode="json")
    allowed = list(typed.allowed_changed_paths)
    feedback_id = str(typed.feedback.get("feedback_id") or "")
    fixed_patch_values = {
        "base_package_hash": _sha256_text(_canonical_json(previous_json)),
        "feedback_id": feedback_id,
        "claim_evidence": previous_json["claim_evidence"],
        "warnings": previous_json["warnings"],
    }
    for field, value in fixed_patch_values.items():
        field_schema = patch_properties.get(field)
        if not isinstance(field_schema, dict):
            return schema
        field_schema["const"] = value
        field_schema["description"] = (
            "Для текущей операции сервер зафиксировал точное значение из предыдущего пакета. "
            "Скопируй это значение целиком без фильтрации или переименования."
        )
    for field in ("claim_evidence", "warnings"):
        field_schema = patch_properties[field]
        value = fixed_patch_values[field]
        field_schema["minItems"] = len(value)
        field_schema["maxItems"] = len(value)

    changed_paths_schema = patch_properties.get("changed_paths")
    if not isinstance(changed_paths_schema, dict):
        return schema
    changed_path_items = changed_paths_schema.get("items")
    if not isinstance(changed_path_items, dict):
        return schema
    changed_path_items["enum"] = allowed
    changed_path_items["description"] = (
        "Перечисли только фактически изменённые пути из этого закрытого списка."
    )
    changed_paths_schema["maxItems"] = min(20, len(allowed))

    allowed_set = set(allowed)
    sms_allowed = any(path.startswith("/sms/") for path in allowed)
    email_allowed = any(path.startswith("/email/") for path in allowed)
    if sms_allowed != email_allowed:
        active_channel = "sms" if sms_allowed else "email"
        inactive_channel = "email" if sms_allowed else "sms"
        patch_properties[active_channel] = {"$ref": f"#/$defs/{active_channel.title()}Artifact"}
        patch_properties[inactive_channel] = {
            "const": None,
            "description": "Этот канал находится вне allowed_changed_paths; передай JSON null.",
        }

    def fix_properties(
        properties: dict[str, Any],
        values: dict[str, Any],
        editable_paths: dict[str, str],
    ) -> bool:
        for field, value in values.items():
            if editable_paths.get(field) in allowed_set:
                continue
            field_schema = properties.get(field)
            if not isinstance(field_schema, dict):
                return False
            field_schema["const"] = value
            field_schema["description"] = (
                "Защищённое значение текущего previous_package; скопируй посимвольно."
            )
        return True

    if (
        previous.sms is not None
        and sms_allowed
        and not fix_properties(
            sms_properties,
            previous_json["sms"],
            {"text": "/sms/text"},
        )
    ):
        return schema
    if previous.email is not None and email_allowed:
        if not fix_properties(
            email_properties,
            {
                field: value
                for field, value in previous_json["email"].items()
                if field != "sections"
            },
            {
                "subject": "/email/subject",
                "preheader": "/email/preheader",
                "headline": "/email/headline",
                "plain_text": "/email/plain_text",
            },
        ):
            return schema
        sections_schema = email_properties.get("sections")
        if not isinstance(sections_schema, dict):
            return schema
        sections = previous_json["email"]["sections"]
        sections_schema["minItems"] = len(sections)
        sections_schema["maxItems"] = len(sections)
        if len(sections) == 1 and not fix_properties(
            section_properties,
            sections[0],
            {
                "heading": "/email/sections/0/heading",
                "body": "/email/sections/0/body",
            },
        ):
            return schema
    return schema


class Base(DeclarativeBase):
    pass


class McpRun(Base):
    __tablename__ = "mcp_runs"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "operation",
            "iteration",
            "idempotency_key",
            name="uq_mcp_run_operation",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    context_version: Mapped[str] = mapped_column(String(64), nullable=False)
    context_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentDraft(Base):
    __tablename__ = "agent_drafts"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "operation",
            "iteration",
            name="uq_one_agent_draft_per_operation",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    draft_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    campaign_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    context_version: Mapped[str] = mapped_column(String(64), nullable=False)
    envelope_json: Mapped[str] = mapped_column(Text, nullable=False)
    draft_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class McpAuditEvent(Base):
    __tablename__ = "mcp_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(128), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    draft_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class McpOperationAuthorization(Base):
    __tablename__ = "mcp_operation_authorizations"

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(128), nullable=False)
    campaign_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    context_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class McpTaskAuthorizationAttempt(Base):
    __tablename__ = "mcp_task_authorization_attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "attempt_number", name="uq_mcp_authorization_attempt"),
    )

    attempt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


@dataclass(frozen=True)
class DraftProcessingResult:
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class DraftProcessor(Protocol):
    def process_agent_draft(
        self,
        session: Session,
        request: DraftSaveRequest,
        *,
        saved_at: datetime,
    ) -> DraftProcessingResult: ...


class FactoryMcpService:
    def __init__(
        self,
        database_url: str,
        *,
        draft_processor: DraftProcessor | None = None,
    ) -> None:
        self._engine = create_sqlite_aware_engine(database_url)
        self._write_lock = threading.Lock()
        self._draft_processor = draft_processor

    def set_draft_processor(self, processor: DraftProcessor) -> None:
        self._draft_processor = processor

    def initialize(self) -> None:
        Base.metadata.create_all(self._engine)
        self._backfill_authorization_attempts()
        self._seed_contract_probe()

    def _backfill_authorization_attempts(self) -> None:
        with self._write_lock, Session(self._engine) as session:
            known = set(session.scalars(select(McpTaskAuthorizationAttempt.run_id)))
            for authorization in session.scalars(
                select(McpOperationAuthorization).order_by(McpOperationAuthorization.created_at)
            ):
                if authorization.run_id in known:
                    continue
                session.add(
                    McpTaskAuthorizationAttempt(
                        attempt_id=(
                            f"attempt_mcp_legacy_{_sha256_text(authorization.run_id)[:28]}"
                        ),
                        run_id=authorization.run_id,
                        attempt_number=1,
                        task_id=authorization.task_id,
                        status=authorization.status,
                        created_at=authorization.created_at,
                        closed_at=authorization.closed_at,
                    )
                )
            session.commit()

    def reset_demo_state(self) -> None:
        """Clear mutable MCP receipts and restore only the internal contract probe."""
        with self._write_lock, Session(self._engine) as session:
            for table in reversed(Base.metadata.sorted_tables):
                session.execute(table.delete())
            session.commit()
        self._seed_contract_probe()

    @staticmethod
    def _probe_context(campaign_id: str) -> dict[str, Any]:
        context = {
            "classification": "untrusted_data",
            "context_version": "",
            "operation": "initial",
            "brief_snapshot": {
                "campaign_id": campaign_id,
                "synthetic": True,
                "product_id": "product_probe",
                "segment_id": "segment_probe",
                "goal": "Проверить изолированный контракт сохранения без отправки.",
            },
            "facts": [],
            "active_rules": [],
            "content_plan": {
                "selected_fact_ids": [],
                "selected_concept_ids": [],
                "available_optional_fact_ids": [],
                "available_optional_concept_ids": [],
                "selection_sources": ["base_policy"],
                "applied_rule_version_ids": [],
            },
            "prompt_version": "1.0.0",
            "rules_version": _sha256_text("empty-rules-v1"),
            "output_schema_id": "communication_bundle@1.0",
        }
        context["context_version"] = _sha256_text(_canonical_json(context))
        return context

    def _seed_contract_probe(self) -> None:
        context = self._probe_context("cmp_contract_probe")
        with self._write_lock, Session(self._engine) as session:
            existing = session.scalar(
                select(McpRun).where(
                    McpRun.campaign_id == "cmp_contract_probe",
                    McpRun.operation == "initial",
                    McpRun.iteration == 1,
                    McpRun.idempotency_key == "contract-probe-idempotency-0001",
                )
            )
            if existing is not None:
                return
            session.add(
                McpRun(
                    campaign_id="cmp_contract_probe",
                    operation="initial",
                    iteration=1,
                    idempotency_key="contract-probe-idempotency-0001",
                    context_version=str(context["context_version"]),
                    context_json=_canonical_json(context),
                    created_at=datetime.now(UTC),
                )
            )
            session.commit()

    def prepare_live_probe(self, run_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
            raise ValueError("live probe run id is invalid")
        digest = _sha256_text(run_id)[:20]
        campaign_id = f"cmp_gate0_{digest}"
        idempotency_key = f"gate0-live-probe-{digest}"
        context = self._probe_context(campaign_id)
        with self._write_lock, Session(self._engine) as session:
            existing = session.scalar(
                select(McpRun).where(
                    McpRun.campaign_id == campaign_id,
                    McpRun.operation == "initial",
                    McpRun.iteration == 1,
                )
            )
            if existing is not None:
                raise ValueError("live probe run id was already prepared")
            session.add(
                McpRun(
                    campaign_id=campaign_id,
                    operation="initial",
                    iteration=1,
                    idempotency_key=idempotency_key,
                    context_version=str(context["context_version"]),
                    context_json=_canonical_json(context),
                    created_at=datetime.now(UTC),
                )
            )
            session.commit()
        return {
            "campaign_id": campaign_id,
            "operation": "initial",
            "iteration": 1,
            "idempotency_key": idempotency_key,
            "context_version": context["context_version"],
        }

    @staticmethod
    def _is_live_transport_probe(run: McpRun) -> bool:
        match = re.fullmatch(r"cmp_gate0_([a-f0-9]{20})", run.campaign_id)
        if (
            match is None
            or run.operation != "initial"
            or run.iteration != 1
            or run.idempotency_key != f"gate0-live-probe-{match.group(1)}"
        ):
            return False
        try:
            context = json.loads(run.context_json)
        except json.JSONDecodeError:
            return False
        brief = context.get("brief_snapshot") if isinstance(context, dict) else None
        plan = context.get("content_plan") if isinstance(context, dict) else None
        return (
            isinstance(brief, dict)
            and isinstance(plan, dict)
            and context.get("classification") == "untrusted_data"
            and context.get("context_version") == run.context_version
            and brief.get("campaign_id") == run.campaign_id
            and brief.get("product_id") == "product_probe"
            and context.get("facts") == []
            and plan.get("selected_fact_ids") == []
            and plan.get("selected_concept_ids") == []
        )

    def prepare_operation(
        self,
        *,
        run_id: str,
        task_id: str,
        project_id: str,
        campaign_id: str,
        operation: str,
        iteration: int,
        idempotency_key: str,
        context: dict[str, Any],
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        identifiers = {
            "run_id": run_id,
            "task_id": task_id,
            "project_id": project_id,
            "campaign_id": campaign_id,
        }
        if any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}", value)
            for value in identifiers.values()
        ):
            raise ValueError("managed operation identifiers are invalid")
        if operation not in {"initial", "revision", "rule_proposal"}:
            raise ValueError("managed operation type is invalid")
        if not 1 <= iteration <= 100:
            raise ValueError("managed operation iteration is invalid")
        if not 16 <= len(idempotency_key) <= 128:
            raise ValueError("managed operation idempotency key is invalid")
        brief_snapshot = context.get("brief_snapshot")
        if (
            context.get("context_version") is None
            or context.get("operation") != operation
            or not isinstance(brief_snapshot, dict)
            or brief_snapshot.get("campaign_id") != campaign_id
        ):
            raise ValueError("managed operation context does not match its authorization")
        context_version = str(context["context_version"])
        context_json = _canonical_json(context)
        effective_attempt_id = attempt_id or f"attempt_{_sha256_text(f'{run_id}:{task_id}')[:32]}"
        with self._write_lock, Session(self._engine) as session:
            existing = session.get(McpOperationAuthorization, run_id)
            if existing is not None:
                if (
                    existing.task_id == task_id
                    and existing.project_id == project_id
                    and existing.campaign_id == campaign_id
                    and existing.operation == operation
                    and existing.iteration == iteration
                    and existing.idempotency_key == idempotency_key
                    and existing.context_version == context_version
                ):
                    history = session.scalar(
                        select(McpTaskAuthorizationAttempt).where(
                            McpTaskAuthorizationAttempt.run_id == run_id,
                            McpTaskAuthorizationAttempt.attempt_number == 1,
                        )
                    )
                    if history is None:
                        session.add(
                            McpTaskAuthorizationAttempt(
                                attempt_id=effective_attempt_id,
                                run_id=run_id,
                                attempt_number=1,
                                task_id=task_id,
                                status=existing.status,
                                created_at=existing.created_at,
                                closed_at=existing.closed_at,
                            )
                        )
                        session.commit()
                    return {
                        "run_id": run_id,
                        "task_id": task_id,
                        "project_id": project_id,
                        "campaign_id": campaign_id,
                        "operation": operation,
                        "iteration": iteration,
                        "idempotency_key": idempotency_key,
                        "context_version": context_version,
                    }
                raise ValueError("managed operation run id was already authorized differently")
            conflicting = session.scalar(
                select(McpRun).where(
                    McpRun.campaign_id == campaign_id,
                    McpRun.operation == operation,
                    McpRun.iteration == iteration,
                )
            )
            if conflicting is not None:
                raise ValueError("managed operation slot was already prepared")
            now = datetime.now(UTC)
            session.add(
                McpRun(
                    campaign_id=campaign_id,
                    operation=operation,
                    iteration=iteration,
                    idempotency_key=idempotency_key,
                    context_version=context_version,
                    context_json=context_json,
                    created_at=now,
                )
            )
            session.add(
                McpOperationAuthorization(
                    run_id=run_id,
                    task_id=task_id,
                    project_id=project_id,
                    campaign_id=campaign_id,
                    operation=operation,
                    iteration=iteration,
                    idempotency_key=idempotency_key,
                    context_version=context_version,
                    status="ACTIVE",
                    created_at=now,
                    closed_at=None,
                )
            )
            session.add(
                McpTaskAuthorizationAttempt(
                    attempt_id=effective_attempt_id,
                    run_id=run_id,
                    attempt_number=1,
                    task_id=task_id,
                    status="ACTIVE",
                    created_at=now,
                    closed_at=None,
                )
            )
            session.commit()
        return {
            "run_id": run_id,
            "task_id": task_id,
            "project_id": project_id,
            "campaign_id": campaign_id,
            "operation": operation,
            "iteration": iteration,
            "idempotency_key": idempotency_key,
            "context_version": context_version,
        }

    def prepare_retry_operation(
        self,
        *,
        run_id: str,
        attempt_id: str,
        task_id: str,
        project_id: str,
        campaign_id: str,
        operation: str,
        iteration: int,
        idempotency_key: str,
        context_version: str,
    ) -> dict[str, Any]:
        """Bind task two only after task one is closed and no draft exists."""

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}", attempt_id):
            raise ValueError("managed retry attempt id is invalid")
        with self._write_lock, Session(self._engine) as session:
            authorization = session.get(McpOperationAuthorization, run_id)
            if authorization is None:
                raise ValueError("managed operation authorization is missing")
            expected = (
                authorization.project_id == project_id
                and authorization.campaign_id == campaign_id
                and authorization.operation == operation
                and authorization.iteration == iteration
                and authorization.idempotency_key == idempotency_key
                and authorization.context_version == context_version
            )
            if not expected:
                raise ValueError("managed retry identity differs from the logical operation")
            existing = session.scalar(
                select(McpTaskAuthorizationAttempt).where(
                    McpTaskAuthorizationAttempt.run_id == run_id,
                    McpTaskAuthorizationAttempt.attempt_number == 2,
                )
            )
            if existing is not None:
                if (
                    existing.attempt_id == attempt_id
                    and existing.task_id == task_id
                    and authorization.task_id == task_id
                    and authorization.status == "ACTIVE"
                ):
                    return {
                        "run_id": run_id,
                        "attempt_id": attempt_id,
                        "attempt_number": 2,
                        "task_id": task_id,
                    }
                raise ValueError("managed retry attempt was already authorized differently")
            first = session.scalar(
                select(McpTaskAuthorizationAttempt).where(
                    McpTaskAuthorizationAttempt.run_id == run_id,
                    McpTaskAuthorizationAttempt.attempt_number == 1,
                )
            )
            draft = session.scalar(
                select(AgentDraft).where(
                    AgentDraft.campaign_id == campaign_id,
                    AgentDraft.operation == operation,
                    AgentDraft.iteration == iteration,
                )
            )
            if (
                authorization.status != "CLOSED"
                or authorization.closed_at is None
                or first is None
                or first.status != "CLOSED"
                or first.closed_at is None
                or draft is not None
            ):
                raise ValueError("first task is not safely closed for retry authorization")
            now = datetime.now(UTC)
            authorization.task_id = task_id
            authorization.status = "ACTIVE"
            authorization.created_at = now
            authorization.closed_at = None
            session.add(
                McpTaskAuthorizationAttempt(
                    attempt_id=attempt_id,
                    run_id=run_id,
                    attempt_number=2,
                    task_id=task_id,
                    status="ACTIVE",
                    created_at=now,
                    closed_at=None,
                )
            )
            session.commit()
        return {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "attempt_number": 2,
            "task_id": task_id,
        }

    def close_operation(self, run_id: str) -> None:
        with self._write_lock, Session(self._engine) as session:
            authorization = session.get(McpOperationAuthorization, run_id)
            if authorization is None:
                return
            if authorization.status == "ACTIVE":
                now = datetime.now(UTC)
                authorization.status = "CLOSED"
                authorization.closed_at = now
                attempt = session.scalar(
                    select(McpTaskAuthorizationAttempt).where(
                        McpTaskAuthorizationAttempt.run_id == run_id,
                        McpTaskAuthorizationAttempt.task_id == authorization.task_id,
                    )
                )
                if attempt is not None and attempt.status == "ACTIVE":
                    attempt.status = "CLOSED"
                    attempt.closed_at = now
                session.commit()

    def authorization_attempts(self, run_id: str) -> list[dict[str, Any]]:
        with Session(self._engine) as session:
            rows = list(
                session.scalars(
                    select(McpTaskAuthorizationAttempt)
                    .where(McpTaskAuthorizationAttempt.run_id == run_id)
                    .order_by(McpTaskAuthorizationAttempt.attempt_number)
                )
            )
        return [
            {
                "attempt_id": row.attempt_id,
                "attempt_number": row.attempt_number,
                "task_id": row.task_id,
                "status": row.status,
                "created_at": self._iso_utc(row.created_at),
                "closed_at": self._iso_utc(row.closed_at) if row.closed_at is not None else None,
            }
            for row in rows
        ]

    @staticmethod
    def _authorization_for_run(
        session: Session,
        *,
        campaign_id: str,
        operation: str,
        iteration: int,
        idempotency_key: str,
    ) -> McpOperationAuthorization | None:
        return session.scalar(
            select(McpOperationAuthorization).where(
                McpOperationAuthorization.campaign_id == campaign_id,
                McpOperationAuthorization.operation == operation,
                McpOperationAuthorization.iteration == iteration,
                McpOperationAuthorization.idempotency_key == idempotency_key,
            )
        )

    @staticmethod
    def _close_authorization_attempt(
        session: Session,
        authorization: McpOperationAuthorization,
        *,
        status: str,
        closed_at: datetime,
    ) -> None:
        authorization.status = status
        authorization.closed_at = closed_at
        attempt = session.scalar(
            select(McpTaskAuthorizationAttempt).where(
                McpTaskAuthorizationAttempt.run_id == authorization.run_id,
                McpTaskAuthorizationAttempt.task_id == authorization.task_id,
            )
        )
        if attempt is not None:
            attempt.status = status
            attempt.closed_at = closed_at

    @staticmethod
    def _audit(
        session: Session,
        *,
        campaign_id: str,
        operation: str,
        iteration: int,
        event_type: str,
        status: str,
        started_at: float,
        draft_hash: str = "",
    ) -> None:
        session.add(
            McpAuditEvent(
                campaign_id=campaign_id,
                operation=operation,
                iteration=iteration,
                event_type=event_type,
                status=status,
                duration_ms=max(0, round((time.monotonic() - started_at) * 1000)),
                draft_hash=draft_hash,
                completed_at=datetime.now(UTC),
            )
        )

    def context_get(self, request: ContextGetRequest) -> ContextToolResult:
        started_at = time.monotonic()
        with Session(self._engine) as session:
            run = session.scalar(
                select(McpRun).where(
                    McpRun.campaign_id == request.campaign_id,
                    McpRun.operation == request.operation.value,
                    McpRun.iteration == request.iteration,
                    McpRun.idempotency_key == request.idempotency_key,
                )
            )
            authorization = self._authorization_for_run(
                session,
                campaign_id=request.campaign_id,
                operation=request.operation.value,
                iteration=request.iteration,
                idempotency_key=request.idempotency_key,
            )
            if run is None:
                result = ContextToolResult(
                    ready=False,
                    status="CAMPAIGN_NOT_READY",
                    campaign_id=request.campaign_id,
                    operation=request.operation,
                    iteration=request.iteration,
                    questions=[
                        ToolQuestion(
                            question_id="campaign_readiness",
                            path="/campaign_id",
                            message="Кампания или операция не подготовлена приложением.",
                        )
                    ],
                )
            elif authorization is not None and authorization.status != "ACTIVE":
                result = ContextToolResult(
                    ready=False,
                    status="OPERATION_NOT_ACTIVE",
                    campaign_id=request.campaign_id,
                    operation=request.operation,
                    iteration=request.iteration,
                    context_version=run.context_version,
                    questions=[],
                )
            elif request.context_version and request.context_version != run.context_version:
                result = ContextToolResult(
                    ready=False,
                    status="CONTEXT_VERSION_MISMATCH",
                    campaign_id=request.campaign_id,
                    operation=request.operation,
                    iteration=request.iteration,
                    context_version=run.context_version,
                    questions=[
                        ToolQuestion(
                            question_id="refresh_context",
                            path="/context_version",
                            message="Ожидаемая версия контекста устарела.",
                        )
                    ],
                )
            else:
                context = json.loads(run.context_json)
                schema_by_operation = {
                    "initial": _initial_output_schema(context),
                    "revision": _revision_output_schema(context),
                    "rule_proposal": RuleProposalEnvelope.model_json_schema(),
                }
                result = ContextToolResult(
                    ready=True,
                    status="READY",
                    campaign_id=request.campaign_id,
                    operation=request.operation,
                    iteration=request.iteration,
                    context_version=run.context_version,
                    context_bundle=context,
                    output_schema=schema_by_operation[request.operation.value],
                )
            self._audit(
                session,
                campaign_id=request.campaign_id,
                operation=request.operation.value,
                iteration=request.iteration,
                event_type="context_tool_completed",
                status=result.status,
                started_at=started_at,
            )
            session.commit()
            return result

    def draft_save(self, request: DraftSaveRequest) -> DraftSaveResult:
        started_at = time.monotonic()
        envelope_json = _canonical_json(request.draft.model_dump(mode="json"))
        envelope_hash = _sha256_text(envelope_json)
        with self._write_lock, Session(self._engine) as session:
            run = session.scalar(
                select(McpRun).where(
                    McpRun.campaign_id == request.campaign_id,
                    McpRun.operation == request.operation.value,
                    McpRun.iteration == request.iteration,
                    McpRun.idempotency_key == request.idempotency_key,
                )
            )
            authorization = self._authorization_for_run(
                session,
                campaign_id=request.campaign_id,
                operation=request.operation.value,
                iteration=request.iteration,
                idempotency_key=request.idempotency_key,
            )
            if run is None:
                result = self._blocked(request, "CAMPAIGN_NOT_READY")
                self._audit_save(session, request, result, started_at, envelope_hash)
                session.commit()
                return result
            if run.context_version != request.context_version:
                result = self._blocked(request, "CONTEXT_VERSION_MISMATCH")
                self._audit_save(session, request, result, started_at, envelope_hash)
                session.commit()
                return result
            if authorization is not None and authorization.status not in {"ACTIVE", "CONSUMED"}:
                result = self._blocked(request, "OPERATION_NOT_ACTIVE")
                self._audit_save(session, request, result, started_at, envelope_hash)
                session.commit()
                return result

            existing = session.scalar(
                select(AgentDraft).where(
                    AgentDraft.campaign_id == request.campaign_id,
                    AgentDraft.operation == request.operation.value,
                    AgentDraft.iteration == request.iteration,
                )
            )
            if existing is not None:
                if (
                    existing.idempotency_key == request.idempotency_key
                    and existing.draft_hash == envelope_hash
                ):
                    result = DraftSaveResult(
                        status="SAVED",
                        persisted=True,
                        idempotent_replay=True,
                        campaign_id=request.campaign_id,
                        operation=request.operation,
                        iteration=request.iteration,
                        draft_id=existing.draft_id,
                        draft_hash=existing.draft_hash,
                        saved_at=existing.created_at,
                    )
                    self._audit_save(session, request, result, started_at, envelope_hash)
                    session.commit()
                    return result
                result = self._blocked(request, "DRAFT_ALREADY_SAVED")
                self._audit_save(session, request, result, started_at, envelope_hash)
                session.commit()
                return result

            saved_at = datetime.now(UTC)
            draft_id = f"draft_{uuid.uuid4().hex}"
            processing = DraftProcessingResult()
            if self._draft_processor is not None and not self._is_live_transport_probe(run):
                processing = self._draft_processor.process_agent_draft(
                    session,
                    request,
                    saved_at=saved_at,
                )
            if processing.blockers:
                if authorization is not None:
                    self._close_authorization_attempt(
                        session,
                        authorization,
                        status="REJECTED",
                        closed_at=saved_at,
                    )
                result = DraftSaveResult(
                    status="DRAFT_REJECTED",
                    persisted=False,
                    campaign_id=request.campaign_id,
                    operation=request.operation,
                    iteration=request.iteration,
                    blockers=list(processing.blockers),
                    warnings=list(processing.warnings),
                )
                self._audit_save(session, request, result, started_at, envelope_hash)
                session.commit()
                return result
            session.add(
                AgentDraft(
                    draft_id=draft_id,
                    campaign_id=request.campaign_id,
                    operation=request.operation.value,
                    iteration=request.iteration,
                    idempotency_key=request.idempotency_key,
                    context_version=request.context_version,
                    envelope_json=envelope_json,
                    draft_hash=envelope_hash,
                    created_at=saved_at,
                )
            )
            if authorization is not None:
                self._close_authorization_attempt(
                    session,
                    authorization,
                    status="CONSUMED",
                    closed_at=saved_at,
                )
            result = DraftSaveResult(
                status="SAVED",
                persisted=True,
                campaign_id=request.campaign_id,
                operation=request.operation,
                iteration=request.iteration,
                draft_id=draft_id,
                draft_hash=envelope_hash,
                saved_at=saved_at,
                warnings=list(processing.warnings),
            )
            self._audit_save(session, request, result, started_at, envelope_hash)
            session.commit()
            return result

    def _audit_save(
        self,
        session: Session,
        request: DraftSaveRequest,
        result: DraftSaveResult,
        started_at: float,
        draft_hash: str,
    ) -> None:
        self._audit(
            session,
            campaign_id=request.campaign_id,
            operation=request.operation.value,
            iteration=request.iteration,
            event_type="draft_saved" if result.persisted else "draft_save_blocked",
            status=result.status,
            started_at=started_at,
            draft_hash=draft_hash if result.persisted else "",
        )

    def probe_snapshot(
        self,
        campaign_id: str,
        *,
        operation: str | None = None,
        iteration: int | None = None,
    ) -> dict[str, Any]:
        with Session(self._engine) as session:
            event_query = select(McpAuditEvent).where(McpAuditEvent.campaign_id == campaign_id)
            draft_query = select(AgentDraft).where(AgentDraft.campaign_id == campaign_id)
            if operation is not None:
                event_query = event_query.where(McpAuditEvent.operation == operation)
                draft_query = draft_query.where(AgentDraft.operation == operation)
            if iteration is not None:
                event_query = event_query.where(McpAuditEvent.iteration == iteration)
                draft_query = draft_query.where(AgentDraft.iteration == iteration)
            events = list(session.scalars(event_query.order_by(McpAuditEvent.id)))
            draft = session.scalar(draft_query.order_by(AgentDraft.id.desc()))
        return {
            "events": [
                {
                    "event_type": event.event_type,
                    "status": event.status,
                    "duration_ms": event.duration_ms,
                    "draft_hash": event.draft_hash,
                    "completed_at": self._iso_utc(event.completed_at),
                }
                for event in events
            ],
            "draft": (
                {
                    "draft_id": draft.draft_id,
                    "draft_hash": draft.draft_hash,
                    "saved_at": self._iso_utc(draft.created_at),
                    "envelope": json.loads(draft.envelope_json),
                }
                if draft is not None
                else None
            ),
        }

    @staticmethod
    def _iso_utc(value: datetime) -> str:
        effective = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return effective.astimezone(UTC).isoformat()

    @staticmethod
    def _blocked(request: DraftSaveRequest, code: str) -> DraftSaveResult:
        return DraftSaveResult(
            status=code,
            persisted=False,
            campaign_id=request.campaign_id,
            operation=request.operation,
            iteration=request.iteration,
            blockers=[code],
        )
