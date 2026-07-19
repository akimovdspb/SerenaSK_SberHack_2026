from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from apps.api.app.domain.learning import FeedbackView
from apps.api.app.domain.models import RuleProposalDraft, RuleScope, RuleType
from apps.api.app.services.catalog import load_catalog
from apps.api.app.services.rules import (
    _candidate_hash,
    build_deterministic_rule_proposal,
    materialize_rule_proposal_draft,
    validate_rule_proposal,
)


def _feedback() -> FeedbackView:
    return FeedbackView(
        feedback_id="feedback_rule_unit",
        campaign_id="campaign_rule_unit",
        package_id="package_rule_unit",
        package_version=2,
        package_hash="a" * 64,
        artifact_path="/email/sections/0/body",
        comment="Добавьте payouts_via_online_bank.",
        scope="CURRENT_CHANNEL",
        author_id="rule_unit_editor",
        author_role="editor",
        created_at=datetime.now(UTC),
    )


def test_bounded_concept_rule_passes_target_regression_and_negative_matrix() -> None:
    catalog = load_catalog()
    feedback = _feedback()
    scope = RuleScope(product_ids=["synthetic_payroll"], channel="email", segment_ids=[])
    proposal = build_deterministic_rule_proposal(
        proposal_id="proposal_rule_unit",
        feedback=feedback,
        selected_scope=scope,
        base_rules_version=catalog.rules_version,
        catalog=catalog,
    )

    result = validate_rule_proposal(
        proposal=proposal,
        feedback=feedback,
        selected_scope=scope,
        current_rules_version=catalog.rules_version,
        catalog=catalog,
    )

    assert result.passed is True
    assert {item.test_kind for item in result.tests} == {
        "target",
        "regression",
        "out_of_scope",
    }
    assert all(item.passed for item in result.tests)
    assert sum(item.test_kind == "regression" for item in result.tests) == 4
    regression_cases = {item.case_id for item in result.tests if item.test_kind == "regression"}
    out_of_scope_cases = {item.case_id for item in result.tests if item.test_kind == "out_of_scope"}
    assert regression_cases.isdisjoint(out_of_scope_cases)
    assert proposal.rationale == (
        "Проверяемое правило переносит явно подтверждённое требование из замечания "
        "только на выбранную синтетическую область."
    )
    assert {item.detail for item in result.tests} == {
        "Правило должно примениться к подходящему синтетическому кейсу.",
        "Несвязанный синтетический кейс не должен получить правило.",
        "Явный отрицательный сценарий подтверждает границу области.",
    }


def test_agent_rule_draft_gets_deterministic_server_owned_provenance() -> None:
    catalog = load_catalog()
    feedback = _feedback()
    scope = RuleScope(product_ids=["synthetic_payroll"], channel="email", segment_ids=[])
    draft = RuleProposalDraft(
        type=RuleType.REQUIRE_CONCEPT_ID,
        value="payouts_via_online_bank",
        rationale="Перенести подтверждённое требование на ограниченный e-mail scope.",
        risk="low",
    )

    proposal = materialize_rule_proposal_draft(
        draft=draft,
        feedback=feedback,
        selected_scope=scope,
        base_rules_version=catalog.rules_version,
        context_version="c" * 64,
        catalog=catalog,
    )
    repeated = materialize_rule_proposal_draft(
        draft=draft,
        feedback=feedback,
        selected_scope=scope,
        base_rules_version=catalog.rules_version,
        context_version="c" * 64,
        catalog=catalog,
    )
    result = validate_rule_proposal(
        proposal=proposal,
        feedback=feedback,
        selected_scope=scope,
        current_rules_version=catalog.rules_version,
        catalog=catalog,
    )

    assert proposal == repeated
    assert proposal.proposal_id.startswith("proposal_")
    assert proposal.source_feedback_id == feedback.feedback_id
    assert proposal.scope == scope
    assert proposal.target_case_ids == ["B03"]
    assert proposal.base_rules_version == catalog.rules_version
    assert proposal.candidate_rules_version != catalog.rules_version
    assert result.passed is True


def test_rule_validation_rejects_unsafe_type_scope_and_candidate_drift() -> None:
    catalog = load_catalog()
    feedback = _feedback()
    scope = RuleScope(product_ids=["synthetic_payroll"], channel="email", segment_ids=[])
    proposal = build_deterministic_rule_proposal(
        proposal_id="proposal_rule_unsafe",
        feedback=feedback,
        selected_scope=scope,
        base_rules_version=catalog.rules_version,
        catalog=catalog,
    ).model_copy(
        update={
            "type": RuleType.REQUIRE_PHRASE,
            "value": "javascript:eval(tool)",
            "candidate_rules_version": "0" * 64,
        }
    )

    result = validate_rule_proposal(
        proposal=proposal,
        feedback=feedback,
        selected_scope=RuleScope(
            product_ids=["synthetic_payroll"],
            channel="sms",
            segment_ids=[],
        ),
        current_rules_version=catalog.rules_version,
        catalog=catalog,
    )

    assert {
        "RULE_SCOPE_MISMATCH",
        "RULE_CANDIDATE_HASH_MISMATCH",
        "RULE_VALUE_UNSAFE",
    }.issubset(result.errors)


@pytest.mark.parametrize(
    ("rule_type", "value"),
    [
        (RuleType.FORBID_PHRASE, "нежелательный оборот"),
        (RuleType.REQUIRE_PHRASE, "Подключение доступно онлайн"),
        (RuleType.REQUIRE_FACT, "fact_payroll_setup"),
        (RuleType.REQUIRE_CONCEPT_ID, "payouts_via_online_bank"),
        (RuleType.TONE_HINT, "спокойный и практичный"),
    ],
)
def test_all_allowlisted_rule_types_have_deterministic_validation(
    rule_type: RuleType,
    value: str,
) -> None:
    catalog = load_catalog()
    feedback = _feedback()
    scope = RuleScope(product_ids=["synthetic_payroll"], channel="email", segment_ids=[])
    base = build_deterministic_rule_proposal(
        proposal_id=f"proposal_{rule_type.value}",
        feedback=feedback,
        selected_scope=scope,
        base_rules_version=catalog.rules_version,
        catalog=catalog,
    )
    candidate = _candidate_hash(
        base_rules_version=base.base_rules_version,
        proposal_id=base.proposal_id,
        source_feedback_id=base.source_feedback_id,
        rule_type=rule_type,
        scope=scope,
        value=value,
    )
    proposal = base.model_copy(
        update={
            "type": rule_type,
            "value": value,
            "candidate_rules_version": candidate,
        }
    )

    result = validate_rule_proposal(
        proposal=proposal,
        feedback=feedback,
        selected_scope=scope,
        current_rules_version=catalog.rules_version,
        catalog=catalog,
    )

    assert result.passed is True


def test_phrase_rule_cannot_forbid_protected_product_content() -> None:
    catalog = load_catalog()
    feedback = _feedback()
    scope = RuleScope(product_ids=["synthetic_payroll"], channel="email", segment_ids=[])
    base = build_deterministic_rule_proposal(
        proposal_id="proposal_protected_phrase",
        feedback=feedback,
        selected_scope=scope,
        base_rules_version=catalog.rules_version,
        catalog=catalog,
    )
    value = "Пульс Выплат"
    proposal = base.model_copy(
        update={
            "type": RuleType.FORBID_PHRASE,
            "value": value,
            "candidate_rules_version": _candidate_hash(
                base_rules_version=base.base_rules_version,
                proposal_id=base.proposal_id,
                source_feedback_id=base.source_feedback_id,
                rule_type=RuleType.FORBID_PHRASE,
                scope=scope,
                value=value,
            ),
        }
    )

    result = validate_rule_proposal(
        proposal=proposal,
        feedback=feedback,
        selected_scope=scope,
        current_rules_version=catalog.rules_version,
        catalog=catalog,
    )

    assert "RULE_PROTECTED_CONTENT_CONFLICT" in result.errors


def test_global_rule_scope_is_schema_invalid() -> None:
    with pytest.raises(ValidationError, match="global rule scope is not allowed"):
        RuleScope()
