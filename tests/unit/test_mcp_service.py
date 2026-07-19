from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
from datetime import UTC, date, datetime
from typing import Any

import pytest

from apps.api.app.domain.campaigns import ContextBundle
from apps.api.app.domain.learning import FeedbackAuthorRole, FeedbackScope, FeedbackView
from apps.api.app.domain.models import ContextGetRequest, DraftSaveRequest
from apps.api.app.mcp.service import (
    DraftProcessingResult,
    FactoryMcpService,
    _initial_output_schema,
    _revision_output_schema,
)
from apps.api.app.services.briefs import (
    build_initial_context,
    create_draft,
    hash_value,
    validate_and_promote,
)
from apps.api.app.services.catalog import load_catalog
from apps.api.app.services.deterministic import build_deterministic_bundle
from apps.api.app.services.revisions import build_revision_context
from tests.factories import communication_bundle_envelope

ADAPTER_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "ouroboros" / "runtime" / "strict_tool_adapter.py"
)


def _load_strict_adapter() -> Any:
    spec = importlib.util.spec_from_file_location("cf_mcp_strict_tool_adapter", ADAPTER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load strict tool adapter")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _business_initial_context(case_id: str) -> dict[str, Any]:
    catalog = load_catalog()
    case = catalog.case(case_id)
    values = case.brief
    if case_id == "B01":
        values = values.model_copy(
            update={
                "cta_label": "Собрать первый реестр",
                "cta_url": "https://pulse-pay.example.test/start",
            }
        )
    draft = create_draft(campaign_id=case.campaign_id, values=values)
    result = validate_and_promote(draft, catalog, as_of=date(2026, 7, 11))
    assert result.ready_brief is not None
    return build_initial_context(result.ready_brief, catalog).model_dump(mode="json")


def _business_revision_context(case_id: str = "B01") -> dict[str, Any]:
    initial = ContextBundle.model_validate(_business_initial_context(case_id))
    previous = build_deterministic_bundle(initial)
    feedback = FeedbackView(
        feedback_id=f"feedback_{case_id.lower()}_revision",
        campaign_id=initial.brief_snapshot.campaign_id,
        package_id=f"package_{case_id.lower()}_revision",
        package_version=1,
        package_hash=hash_value(previous.model_dump(mode="json")),
        artifact_path="/email/sections/0/body",
        comment="Добавьте разрешённое понятие payouts_via_online_bank.",
        scope=FeedbackScope.CURRENT_CHANNEL,
        author_id="revision_schema_test_editor",
        author_role=FeedbackAuthorRole.EDITOR,
        created_at=datetime(2026, 7, 15, tzinfo=UTC),
    )
    return build_revision_context(
        base_context=initial,
        base_bundle=previous,
        feedback=feedback,
    ).model_dump(mode="json")


def _object_nodes(node: Any, path: str = "$") -> list[tuple[str, dict[str, Any]]]:
    if not isinstance(node, dict):
        return []
    nodes: list[tuple[str, dict[str, Any]]] = []
    if node.get("type") == "object" or isinstance(node.get("properties"), dict):
        nodes.append((path, node))
    for key in ("properties", "$defs"):
        children = node.get(key)
        if isinstance(children, dict):
            for name, child in children.items():
                nodes.extend(_object_nodes(child, f"{path}/{key}/{name}"))
    items = node.get("items")
    if items is not None:
        nodes.extend(_object_nodes(items, f"{path}/items"))
    any_of = node.get("anyOf")
    if isinstance(any_of, list):
        for index, child in enumerate(any_of):
            nodes.extend(_object_nodes(child, f"{path}/anyOf/{index}"))
    return nodes


def _ref_nodes(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, list):
        return [ref for item in node for ref in _ref_nodes(item)]
    if not isinstance(node, dict):
        return []
    nodes = [node] if "$ref" in node else []
    return nodes + [ref for value in node.values() for ref in _ref_nodes(value)]


def test_draft_save_is_persistent_and_idempotent(tmp_path: pathlib.Path) -> None:
    service = FactoryMcpService(f"sqlite:///{tmp_path / 'factory.db'}")
    service.initialize()
    context = service.context_get(
        ContextGetRequest(
            campaign_id="cmp_contract_probe",
            operation="initial",
            iteration=1,
            idempotency_key="contract-probe-idempotency-0001",
        )
    )
    assert context.ready is True
    context_version = context.context_version
    assert context_version is not None

    request = DraftSaveRequest.model_validate(
        {
            "campaign_id": "cmp_contract_probe",
            "operation": "initial",
            "iteration": 1,
            "context_version": context_version,
            "idempotency_key": "contract-probe-idempotency-0001",
            "draft": communication_bundle_envelope(context_version),
        }
    )
    first = service.draft_save(request)
    replay = service.draft_save(request)

    assert first.persisted is True
    assert first.idempotent_replay is False
    assert replay.persisted is True
    assert replay.idempotent_replay is True
    assert replay.draft_id == first.draft_id
    assert replay.draft_hash == first.draft_hash


def test_second_distinct_draft_fails_closed(tmp_path: pathlib.Path) -> None:
    service = FactoryMcpService(f"sqlite:///{tmp_path / 'factory.db'}")
    service.initialize()
    context = service.context_get(
        ContextGetRequest(
            campaign_id="cmp_contract_probe",
            operation="initial",
            iteration=1,
            idempotency_key="contract-probe-idempotency-0001",
        )
    )
    context_version = context.context_version
    assert context_version is not None

    def request(summary: str) -> DraftSaveRequest:
        return DraftSaveRequest.model_validate(
            {
                "campaign_id": "cmp_contract_probe",
                "operation": "initial",
                "iteration": 1,
                "context_version": context_version,
                "idempotency_key": "contract-probe-idempotency-0001",
                "draft": communication_bundle_envelope(context_version, summary=summary),
            }
        )

    assert service.draft_save(request("Первая версия")).persisted is True
    rejected = service.draft_save(request("Другая версия"))

    assert rejected.persisted is False
    assert rejected.status == "DRAFT_ALREADY_SAVED"
    assert rejected.blockers == ["DRAFT_ALREADY_SAVED"]


def test_live_probe_fixture_is_unique_and_audits_both_operations(
    tmp_path: pathlib.Path,
) -> None:
    service = FactoryMcpService(f"sqlite:///{tmp_path / 'factory.db'}")
    service.initialize()
    prepared = service.prepare_live_probe("gate0-live-probe-01")
    context = service.context_get(
        ContextGetRequest(
            campaign_id=prepared["campaign_id"],
            operation="initial",
            iteration=1,
            idempotency_key=prepared["idempotency_key"],
        )
    )
    assert context.context_version == prepared["context_version"]
    assert context.output_schema is not None
    definitions = context.output_schema["$defs"]
    sections_schema = definitions["EmailArtifact"]["properties"]["sections"]
    evidence_schema = definitions["CommunicationBundle"]["properties"]["claim_evidence"]
    assert sections_schema["minItems"] == 1
    assert sections_schema["maxItems"] == 20
    assert "minItems" not in evidence_schema
    assert evidence_schema["maxItems"] == 200
    assert "const" not in definitions["EmailArtifact"]["properties"]["subject"]
    assert "const" not in definitions["EmailSection"]["properties"]["heading"]
    assert "const" not in definitions["CommunicationBundle"]["properties"]["summary"]
    request = DraftSaveRequest.model_validate(
        {
            "campaign_id": prepared["campaign_id"],
            "operation": "initial",
            "iteration": 1,
            "context_version": prepared["context_version"],
            "idempotency_key": prepared["idempotency_key"],
            "draft": communication_bundle_envelope(
                prepared["context_version"],
                campaign_id=prepared["campaign_id"],
            ),
        }
    )
    saved = service.draft_save(request)

    snapshot = service.probe_snapshot(prepared["campaign_id"])

    assert [event["event_type"] for event in snapshot["events"]] == [
        "context_tool_completed",
        "draft_saved",
    ]
    assert all(event["completed_at"].endswith("+00:00") for event in snapshot["events"])
    assert snapshot["draft"]["draft_hash"] == saved.draft_hash
    assert snapshot["draft"]["envelope"]["campaign_id"] == prepared["campaign_id"]
    try:
        service.prepare_live_probe("gate0-live-probe-01")
    except ValueError as exc:
        assert str(exc) == "live probe run id was already prepared"
    else:
        raise AssertionError("a reused live probe run id was accepted")


def test_live_transport_probe_bypasses_only_the_business_draft_processor(
    tmp_path: pathlib.Path,
) -> None:
    class RejectingProcessor:
        def process_agent_draft(self, *_: Any, **__: Any) -> DraftProcessingResult:
            return DraftProcessingResult(blockers=("BUSINESS_PROCESSOR_REJECTED",))

    service = FactoryMcpService(
        f"sqlite:///{tmp_path / 'factory.db'}",
        draft_processor=RejectingProcessor(),
    )
    service.initialize()
    prepared = service.prepare_live_probe("gate0-live-probe-dispatch-test")
    probe_request = DraftSaveRequest.model_validate(
        {
            "campaign_id": prepared["campaign_id"],
            "operation": "initial",
            "iteration": 1,
            "context_version": prepared["context_version"],
            "idempotency_key": prepared["idempotency_key"],
            "draft": communication_bundle_envelope(
                prepared["context_version"], campaign_id=prepared["campaign_id"]
            ),
        }
    )

    assert service.draft_save(probe_request).persisted is True

    ordinary_context = service.context_get(
        ContextGetRequest(
            campaign_id="cmp_contract_probe",
            operation="initial",
            iteration=1,
            idempotency_key="contract-probe-idempotency-0001",
        )
    )
    assert ordinary_context.context_version is not None
    ordinary_request = DraftSaveRequest.model_validate(
        {
            "campaign_id": "cmp_contract_probe",
            "operation": "initial",
            "iteration": 1,
            "context_version": ordinary_context.context_version,
            "idempotency_key": "contract-probe-idempotency-0001",
            "draft": communication_bundle_envelope(ordinary_context.context_version),
        }
    )

    rejected = service.draft_save(ordinary_request)
    assert rejected.persisted is False
    assert rejected.blockers == ["BUSINESS_PROCESSOR_REJECTED"]


def test_revision_and_rule_schemas_do_not_receive_initial_only_constants(
    tmp_path: pathlib.Path,
) -> None:
    service = FactoryMcpService(f"sqlite:///{tmp_path / 'factory.db'}")
    service.initialize()
    for ordinal, operation in enumerate(("revision", "rule_proposal"), start=1):
        campaign_id = f"cmp_operation_scope_{ordinal:02d}"
        context = {
            "context_version": "c" * 64,
            "operation": operation,
            "brief_snapshot": {"campaign_id": campaign_id, "synthetic": True},
        }
        key = f"operation-scope-key-{ordinal:04d}"
        service.prepare_operation(
            run_id=f"run_operation_scope_{ordinal:04d}",
            task_id=f"task_operation_scope_{ordinal:04d}",
            project_id=f"project_operation_scope_{ordinal:04d}",
            campaign_id=campaign_id,
            operation=operation,
            iteration=1,
            idempotency_key=key,
            context=context,
        )
        result = service.context_get(
            ContextGetRequest(
                campaign_id=campaign_id,
                operation=operation,
                iteration=1,
                idempotency_key=key,
            )
        )
        assert result.ready is True
        schema = result.output_schema
        assert schema is not None
        serialized = json.dumps(schema, ensure_ascii=False)
        if operation == "revision":
            assert schema["title"] == "CommunicationPatchEnvelope"
            assert schema["properties"]["kind"]["const"] == "communication_patch"
            email_properties = schema["$defs"]["EmailArtifact"]["properties"]
            section_properties = schema["$defs"]["EmailSection"]["properties"]
            assert "const" not in email_properties["subject"]
            assert "const" not in email_properties["preheader"]
            assert "const" not in email_properties["headline"]
            assert "const" not in section_properties["heading"]
            sections_schema = email_properties["sections"]
            assert (sections_schema["minItems"], sections_schema["maxItems"]) == (1, 20)
        else:
            assert schema["title"] == "RuleProposalEnvelope"
            assert schema["properties"]["kind"]["const"] == "rule_proposal"
            assert "EmailArtifact" not in schema["$defs"]
            assert "Синтетический пакет без отправки." not in serialized
        assert "CommunicationBundleEnvelope" not in serialized


def test_retry_authorization_requires_closed_first_task_and_preserves_one_operation(
    tmp_path: pathlib.Path,
) -> None:
    service = FactoryMcpService(f"sqlite:///{tmp_path / 'factory.db'}")
    service.initialize()
    context = {
        "context_version": "c" * 64,
        "operation": "initial",
        "brief_snapshot": {"campaign_id": "cmp_retry_auth", "synthetic": True},
    }
    identity = {
        "run_id": "run_retry_auth_0001",
        "project_id": "project_retry_auth_0001",
        "campaign_id": "cmp_retry_auth",
        "operation": "initial",
        "iteration": 1,
        "idempotency_key": "retry-auth-idempotency-0001",
        "context_version": "c" * 64,
    }
    service.prepare_operation(
        **{key: value for key, value in identity.items() if key != "context_version"},
        task_id="task_retry_auth_0001",
        attempt_id="attempt_retry_auth_0001",
        context=context,
    )

    with pytest.raises(ValueError, match="not safely closed"):
        service.prepare_retry_operation(
            **identity,
            task_id="task_retry_auth_0002",
            attempt_id="attempt_retry_auth_0002",
        )

    service.close_operation("run_retry_auth_0001")
    service.prepare_retry_operation(
        **identity,
        task_id="task_retry_auth_0002",
        attempt_id="attempt_retry_auth_0002",
    )
    replay = service.prepare_retry_operation(
        **identity,
        task_id="task_retry_auth_0002",
        attempt_id="attempt_retry_auth_0002",
    )

    history = service.authorization_attempts("run_retry_auth_0001")
    assert replay["attempt_number"] == 2
    assert [(row["attempt_number"], row["task_id"], row["status"]) for row in history] == [
        (1, "task_retry_auth_0001", "CLOSED"),
        (2, "task_retry_auth_0002", "ACTIVE"),
    ]
    assert history[0]["closed_at"] is not None
    assert history[1]["closed_at"] is None


def test_retry_authorization_is_denied_after_a_draft_was_accepted(
    tmp_path: pathlib.Path,
) -> None:
    service = FactoryMcpService(f"sqlite:///{tmp_path / 'factory.db'}")
    service.initialize()
    campaign_id = "cmp_retry_after_save"
    context_version = "d" * 64
    idempotency_key = "retry-after-save-key-0001"
    context_payload = {
        "context_version": context_version,
        "operation": "initial",
        "brief_snapshot": {"campaign_id": campaign_id, "synthetic": True},
    }
    service.prepare_operation(
        run_id="run_retry_after_save_0001",
        task_id="task_retry_after_save_0001",
        attempt_id="attempt_retry_after_save_0001",
        project_id="project_retry_after_save_0001",
        campaign_id=campaign_id,
        operation="initial",
        iteration=1,
        idempotency_key=idempotency_key,
        context=context_payload,
    )
    context = service.context_get(
        ContextGetRequest(
            campaign_id=campaign_id,
            operation="initial",
            iteration=1,
            context_version=context_version,
            idempotency_key=idempotency_key,
        )
    )
    assert context.context_version == context_version
    saved = service.draft_save(
        DraftSaveRequest.model_validate(
            {
                "campaign_id": campaign_id,
                "operation": "initial",
                "iteration": 1,
                "context_version": context_version,
                "idempotency_key": idempotency_key,
                "draft": communication_bundle_envelope(
                    context_version,
                    campaign_id=campaign_id,
                ),
            }
        )
    )

    assert saved.persisted is True
    with pytest.raises(ValueError, match="not safely closed"):
        service.prepare_retry_operation(
            run_id="run_retry_after_save_0001",
            task_id="task_retry_after_save_0002",
            attempt_id="attempt_retry_after_save_0002",
            project_id="project_retry_after_save_0001",
            campaign_id=campaign_id,
            operation="initial",
            iteration=1,
            idempotency_key=idempotency_key,
            context_version=context_version,
        )


def test_initial_business_schema_survives_strict_normalization_without_ref_siblings(
    tmp_path: pathlib.Path,
) -> None:
    adapter = _load_strict_adapter()
    context = _business_initial_context("B04")
    schema = _initial_output_schema(context)

    email_properties = schema["$defs"]["EmailArtifact"]["properties"]
    section_properties = schema["$defs"]["EmailSection"]["properties"]
    for field in ("subject", "preheader", "headline"):
        assert "const" not in email_properties[field]
    assert "const" not in section_properties["heading"]
    assert schema["$defs"]["CommunicationBundle"]["properties"]["summary"]["const"] == (
        "Синтетический пакет без отправки."
    )

    normalized = adapter.normalize_parameter_schema("communication_bundle_initial", schema)

    assert adapter.strict_parameter_schema_issues(normalized) == []
    for node in _ref_nodes(normalized):
        assert set(node) == {"$ref"}, f"$ref node kept sibling keywords: {sorted(node)}"
    for path, node in _object_nodes(normalized):
        properties = node.get("properties")
        property_names = list(properties) if isinstance(properties, dict) else []
        assert node.get("additionalProperties") is False, path
        assert sorted(node.get("required", [])) == sorted(property_names), path
    normalized_email = normalized["$defs"]["EmailArtifact"]["properties"]
    normalized_sms = normalized["$defs"]["SmsArtifact"]["properties"]
    assert normalized_sms["text"]["maxLength"] == 201
    assert "не более 3 сегментов" in normalized_sms["text"]["description"]
    for field in ("subject", "preheader", "headline"):
        assert "const" not in normalized_email[field]
    assert "const" not in normalized["$defs"]["EmailSection"]["properties"]["heading"]
    normalized_sections = normalized_email["sections"]
    assert normalized_sections["minItems"] == normalized_sections["maxItems"] == 2
    normalized_evidence = normalized["$defs"]["CommunicationBundle"]["properties"]["claim_evidence"]
    assert normalized_evidence["minItems"] == normalized_evidence["maxItems"] == 7
    artifact_path = normalized["$defs"]["ClaimEvidence"]["properties"]["artifact_path"]
    assert (
        artifact_path["enum"]
        == schema["$defs"]["ClaimEvidence"]["properties"]["artifact_path"]["enum"]
    )


def test_initial_schema_uses_single_segment_unicode_bound() -> None:
    context = _business_initial_context("B04")
    context["channel_policies"]["sms"]["max_segments"] = 1

    schema = _initial_output_schema(context)

    sms_text = schema["$defs"]["SmsArtifact"]["properties"]["text"]
    assert sms_text["maxLength"] == 70
    assert "не более 1 сегмента" in sms_text["description"]


def test_revision_business_schema_locks_complete_unchanged_patch_fields() -> None:
    adapter = _load_strict_adapter()
    context = _business_revision_context()
    schema = _revision_output_schema(context)
    previous = context["previous_package"]
    assert isinstance(previous, dict)

    patch = schema["$defs"]["CommunicationPatch"]["properties"]
    assert patch["base_package_hash"]["const"] == hash_value(previous)
    assert patch["feedback_id"]["const"] == context["feedback"]["feedback_id"]
    assert set(patch["changed_paths"]["items"]["enum"]) == set(context["allowed_changed_paths"])
    assert patch["sms"]["const"] is None
    assert patch["email"] == {"$ref": "#/$defs/EmailArtifact"}
    assert patch["claim_evidence"]["const"] == previous["claim_evidence"]
    assert patch["warnings"]["const"] == previous["warnings"]
    assert patch["claim_evidence"]["minItems"] == patch["claim_evidence"]["maxItems"]

    email = schema["$defs"]["EmailArtifact"]["properties"]
    sms = schema["$defs"]["SmsArtifact"]["properties"]
    assert sms["text"]["maxLength"] == 201
    assert email["cta_label"]["const"] == previous["email"]["cta_label"]
    assert email["cta_url"]["const"] == previous["email"]["cta_url"]
    assert email["fact_refs"]["const"] == previous["email"]["fact_refs"]
    section_count = len(previous["email"]["sections"])
    assert email["sections"]["minItems"] == email["sections"]["maxItems"] == section_count
    section = schema["$defs"]["EmailSection"]["properties"]
    assert "const" not in section["fact_refs"]

    normalized = adapter.normalize_parameter_schema("communication_patch_revision", schema)

    assert adapter.strict_parameter_schema_issues(normalized) == []
    normalized_patch = normalized["$defs"]["CommunicationPatch"]["properties"]
    assert normalized_patch["claim_evidence"]["const"] == previous["claim_evidence"]
    assert normalized_patch["warnings"]["const"] == previous["warnings"]
    for node in _ref_nodes(normalized):
        assert set(node) == {"$ref"}, f"$ref node kept sibling keywords: {sorted(node)}"


@pytest.mark.parametrize(
    ("case_id", "expected_paths"),
    [
        (
            "B01",
            {
                "/sms/text",
                "/sms/cta_url",
                "/email/sections/0/body",
                "/email/sections/1/body",
                "/email/sections/2/body",
                "/email/plain_text",
                "/email/cta_url",
            },
        ),
        (
            "B09",
            {
                "/email/sections/0/body",
                "/email/sections/1/body",
                "/email/plain_text",
                "/email/cta_url",
            },
        ),
    ],
)
def test_initial_business_schema_bounds_evidence_to_existing_artifact_paths(
    case_id: str,
    expected_paths: set[str],
) -> None:
    schema = _initial_output_schema(_business_initial_context(case_id))

    artifact_path = schema["$defs"]["ClaimEvidence"]["properties"]["artifact_path"]

    assert set(artifact_path["enum"]) == expected_paths
    assert "Индексы sections нулевые" in artifact_path["description"]


def test_draft_save_request_rejects_missing_nested_field_and_extra_key() -> None:
    valid = communication_bundle_envelope("d" * 64)
    request = {
        "campaign_id": "cmp_contract_probe",
        "operation": "initial",
        "iteration": 1,
        "context_version": "d" * 64,
        "idempotency_key": "contract-probe-idempotency-0001",
        "draft": valid,
    }
    DraftSaveRequest.model_validate(request)

    missing_nested = copy.deepcopy(request)
    del missing_nested["draft"]["payload"]["sms"]["cta_url"]
    with pytest.raises(ValueError, match="cta_url"):
        DraftSaveRequest.model_validate(missing_nested)

    extra_key = copy.deepcopy(request)
    extra_key["draft"]["payload"]["unexpected_key"] = "x"
    with pytest.raises(ValueError, match="unexpected_key"):
        DraftSaveRequest.model_validate(extra_key)

    wrong_kind = copy.deepcopy(request)
    wrong_kind["draft"]["kind"] = "communication_patch"
    with pytest.raises(ValueError, match="communication_patch"):
        DraftSaveRequest.model_validate(wrong_kind)
