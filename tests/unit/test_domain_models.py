from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from apps.api.app.domain.models import (
    ChannelSuppression,
    ChannelSuppressionReason,
    ClaimEvidence,
    CommunicationBundle,
    DraftSaveRequest,
    DraftSaveResult,
    EmailArtifact,
    EmailSection,
    NormalizedValue,
    RuleProposalEnvelope,
    SmsArtifact,
)
from tests.factories import communication_bundle_envelope

CONTEXT_VERSION = "a" * 64


def test_api_models_serialize_naive_database_datetimes_as_explicit_utc() -> None:
    saved_at = datetime(2026, 7, 19, 12, 19, 0)
    result = DraftSaveResult(
        status="saved",
        persisted=True,
        campaign_id="cmp_timezone_probe",
        operation="initial",
        iteration=1,
        saved_at=saved_at,
    )

    assert result.saved_at == saved_at.replace(tzinfo=UTC)
    assert result.model_dump(mode="json")["saved_at"] == "2026-07-19T12:19:00Z"


def test_initial_envelope_accepts_explicit_sms_and_email() -> None:
    request = DraftSaveRequest.model_validate(
        {
            "campaign_id": "cmp_contract_probe",
            "operation": "initial",
            "iteration": 1,
            "context_version": CONTEXT_VERSION,
            "idempotency_key": "contract-probe-idempotency-0001",
            "draft": communication_bundle_envelope(CONTEXT_VERSION),
        }
    )

    assert request.draft.kind == "communication_bundle"
    assert request.draft.payload.sms is not None
    assert request.draft.payload.email is not None


def test_outer_and_envelope_versions_cannot_diverge() -> None:
    envelope = communication_bundle_envelope("b" * 64)

    with pytest.raises(ValidationError, match="context_version does not match"):
        DraftSaveRequest.model_validate(
            {
                "campaign_id": "cmp_contract_probe",
                "operation": "initial",
                "iteration": 1,
                "context_version": CONTEXT_VERSION,
                "idempotency_key": "contract-probe-idempotency-0001",
                "draft": envelope,
            }
        )


def test_missing_channel_requires_explicit_suppression() -> None:
    envelope = communication_bundle_envelope(CONTEXT_VERSION)
    envelope["payload"]["sms"] = None

    with pytest.raises(ValidationError, match="sms must be present or explicitly suppressed"):
        DraftSaveRequest.model_validate(
            {
                "campaign_id": "cmp_contract_probe",
                "operation": "initial",
                "iteration": 1,
                "context_version": CONTEXT_VERSION,
                "idempotency_key": "contract-probe-idempotency-0001",
                "draft": envelope,
            }
        )


def test_compact_structured_email_section_is_not_rejected_by_non_normative_limits() -> None:
    section = EmailSection(
        section_id="s1",
        kind="text",
        heading="Срок подключения",
        body="Срок подключения составляет 14 дней.",
        fact_refs=["fact_term_14_days"],
        personalization_refs=[],
    )

    assert section.section_id == "s1"
    assert section.kind == "text"


def test_agent_output_schema_requires_refs_and_explains_evidence_pointer_base() -> None:
    sms_schema = SmsArtifact.model_json_schema()
    email_schema = EmailArtifact.model_json_schema()
    section_schema = EmailSection.model_json_schema()
    bundle_schema = CommunicationBundle.model_json_schema()
    evidence_schema = ClaimEvidence.model_json_schema()
    suppression_schema = ChannelSuppression.model_json_schema()

    assert {"fact_refs", "personalization_refs"}.issubset(sms_schema["required"])
    assert {
        "headline",
        "cta_label",
        "cta_url",
        "disclaimer_ids",
        "plain_text",
        "fact_refs",
        "personalization_refs",
    }.issubset(email_schema["required"])
    assert {"channel_suppressions", "claim_evidence", "warnings"}.issubset(
        bundle_schema["required"]
    )
    pointer_description = evidence_schema["properties"]["artifact_path"]["description"]
    claim_description = evidence_schema["properties"]["claim_id"]["description"]
    fragment_description = evidence_schema["properties"]["text_fragment"]["description"]
    normalized_description = evidence_schema["properties"]["normalized_value"]["description"]
    assert "normalized_value" in evidence_schema["required"]
    assert "channel" in evidence_schema["required"]
    channel_description = evidence_schema["properties"]["channel"]["description"]
    assert "точным фактическим вхождением" in channel_description
    reason_ref = suppression_schema["properties"]["reason_code"]["$ref"]
    reason_schema = suppression_schema["$defs"][reason_ref.rsplit("/", 1)[-1]]
    assert set(reason_schema["enum"]) == {
        ChannelSuppressionReason.CHANNEL_NOT_REQUESTED.value,
        ChannelSuppressionReason.CHANNEL_CONSENT_BLOCKED.value,
    }
    assert "никогда не добавляй префикс /payload" in pointer_description
    assert "/sms/text" in pointer_description
    assert "cta_url" in pointer_description
    assert "не повторяй claim_id" in claim_description
    assert "exact_name" in fragment_description
    assert "на разных путях могут различаться" in fragment_description
    assert "не подставляй canonical_text" in fragment_description
    assert "normalized_value" in normalized_description
    assert "FactLedgerItem" in normalized_description
    assert "Для initial" in sms_schema["properties"]["text"]["description"]
    for field in ("subject", "preheader", "headline"):
        description = email_schema["properties"][field]["description"]
        assert "Для initial" in description
        assert "нельзя копировать" in description or "не копируй" in description
    assert "ровно один раз" in email_schema["properties"]["plain_text"]["description"]
    assert "canonical_text" in email_schema["properties"]["plain_text"]["description"]
    assert "не копируй" in section_schema["properties"]["heading"]["description"]
    section_body_description = section_schema["properties"]["body"]["description"]
    assert "закреплённый за секцией" in section_body_description
    assert "обязательна claim_evidence" in section_body_description
    assert "подпись CTA в секции запрещены" in section_body_description
    assert "не используй cta" in section_schema["properties"]["kind"]["description"]
    sections_description = email_schema["properties"]["sections"]["description"]
    assert "от двух до четырёх" in sections_description
    assert "может раскрывать несколько" in sections_description
    assert "не создавай cta-секцию" in sections_description.casefold()
    assert "не копируй" in bundle_schema["properties"]["summary"]["description"]
    evidence_list_description = bundle_schema["properties"]["claim_evidence"]["description"]
    assert "/email/preheader" not in evidence_list_description
    assert "выбранный каналом факт" in evidence_list_description
    assert "на каждом обязательном пути нужна ровно одна запись" in (
        evidence_list_description.casefold()
    )
    normalized_property = evidence_schema["properties"]["normalized_value"]
    normalized_ref = normalized_property["$ref"]
    normalized_schema = evidence_schema["$defs"][normalized_ref.rsplit("/", 1)[-1]]
    assert "anyOf" in normalized_schema
    measure_ref = next(row["$ref"] for row in normalized_schema["anyOf"] if "$ref" in row)
    measure_schema = evidence_schema["$defs"][measure_ref.rsplit("/", 1)[-1]]
    assert measure_schema["additionalProperties"] is False
    assert set(measure_schema["required"]) == {"value", "unit"}


@pytest.mark.parametrize(
    "value",
    ["exact scalar", 14, 14.5, True, None, {"value": 14, "unit": "day"}],
)
def test_normalized_fact_values_round_trip_exactly(value: object) -> None:
    adapter = TypeAdapter(NormalizedValue)
    normalized = adapter.validate_python(value)

    assert adapter.dump_python(normalized, mode="json") == value


def test_normalized_fact_value_rejects_unbounded_objects() -> None:
    adapter = TypeAdapter(NormalizedValue)

    with pytest.raises(ValidationError):
        adapter.validate_python({"value": 14, "unit": "day", "unexpected": True})
    with pytest.raises(ValidationError):
        adapter.validate_python({"nested": {"arbitrary": "value"}})


def test_rule_draft_schema_contains_only_agent_authored_fields() -> None:
    schema = RuleProposalEnvelope.model_json_schema()
    payload_ref = schema["properties"]["payload"]["$ref"]
    payload_schema = schema["$defs"][payload_ref.rsplit("/", 1)[-1]]

    assert set(payload_schema["properties"]) == {
        "condition_id",
        "rationale",
        "risk",
        "type",
        "value",
    }
    assert set(payload_schema["required"]) == {"rationale", "risk", "type", "value"}
    assert "proposal_id" not in payload_schema["properties"]
    assert "candidate_rules_version" not in payload_schema["properties"]
