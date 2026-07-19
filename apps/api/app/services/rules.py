from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from apps.api.app.domain.campaigns import ContextBundle, ReadyCampaignBrief
from apps.api.app.domain.learning import FeedbackView, RuleTestResult
from apps.api.app.domain.models import RuleProposal, RuleProposalDraft, RuleScope, RuleType
from apps.api.app.services.briefs import hash_value
from apps.api.app.services.catalog import SyntheticCatalog

UNSAFE_RULE_VALUE = re.compile(
    r"(?:https?://|javascript:|<script|\b(?:exec|eval|import|subprocess|tool|shell)\b|[{};])",
    re.IGNORECASE,
)
FACTUAL_PHRASE_VALUE = re.compile(
    r"(?:\d|%|₽|\bруб(?:\.|лей)?\b)",  # noqa: RUF001
    re.IGNORECASE,
)
PHRASE_RULE_TYPES = {RuleType.FORBID_PHRASE, RuleType.REQUIRE_PHRASE}


@dataclass(frozen=True)
class RuleValidationResult:
    errors: tuple[str, ...]
    tests: tuple[RuleTestResult, ...]

    @property
    def passed(self) -> bool:
        return not self.errors and all(result.passed for result in self.tests)


def _scope_dict(scope: RuleScope) -> dict[str, Any]:
    return scope.model_dump(mode="json")


def _candidate_hash(
    *,
    base_rules_version: str,
    proposal_id: str,
    source_feedback_id: str,
    rule_type: RuleType,
    scope: RuleScope,
    value: str,
) -> str:
    return hash_value(
        {
            "base_rules_version": base_rules_version,
            "proposal_id": proposal_id,
            "source_feedback_id": source_feedback_id,
            "type": rule_type.value,
            "scope": _scope_dict(scope),
            "value": value,
        }
    )


def requested_concept_id(feedback: FeedbackView, catalog: SyntheticCatalog) -> str | None:
    comment = feedback.comment.casefold()
    matches = [
        concept.concept_id
        for concept in catalog.concepts.values()
        if concept.concept_id.casefold() in comment
        or any(surface.casefold() in comment for surface in concept.accepted_surface_forms)
    ]
    return sorted(matches)[0] if matches else None


def _target_case_ids(selected_scope: RuleScope, catalog: SyntheticCatalog) -> tuple[str, ...]:
    return tuple(
        sorted(
            case.case_id
            for case in catalog.cases.values()
            if case.brief.product_id in selected_scope.product_ids
            and (selected_scope.channel is None or selected_scope.channel in case.brief.channels)
            and (
                not selected_scope.segment_ids
                or case.brief.segment_id in selected_scope.segment_ids
            )
            and case.case_id != "B01"
        )
    )


def materialize_rule_proposal_draft(
    *,
    draft: RuleProposalDraft,
    feedback: FeedbackView,
    selected_scope: RuleScope,
    base_rules_version: str,
    context_version: str,
    catalog: SyntheticCatalog,
) -> RuleProposal:
    target_case_ids = _target_case_ids(selected_scope, catalog)
    if not target_case_ids:
        raise ValueError("selected rule scope has no target synthetic case")
    proposal_digest = hash_value(
        {
            "context_version": context_version,
            "source_feedback_id": feedback.feedback_id,
            "type": draft.type.value,
            "scope": _scope_dict(selected_scope),
            "condition_id": draft.condition_id,
            "value": draft.value,
            "rationale": draft.rationale,
            "risk": draft.risk,
        }
    )
    proposal_id = f"proposal_{proposal_digest[:32]}"
    candidate = _candidate_hash(
        base_rules_version=base_rules_version,
        proposal_id=proposal_id,
        source_feedback_id=feedback.feedback_id,
        rule_type=draft.type,
        scope=selected_scope,
        value=draft.value,
    )
    return RuleProposal(
        proposal_id=proposal_id,
        source_feedback_id=feedback.feedback_id,
        type=draft.type,
        scope=selected_scope,
        condition_id=draft.condition_id,
        value=draft.value,
        rationale=draft.rationale,
        target_case_ids=target_case_ids,
        base_rules_version=base_rules_version,
        candidate_rules_version=candidate,
        risk=draft.risk,
    )


def build_deterministic_rule_proposal(
    *,
    proposal_id: str,
    feedback: FeedbackView,
    selected_scope: RuleScope,
    base_rules_version: str,
    catalog: SyntheticCatalog,
) -> RuleProposal:
    concept_id = requested_concept_id(feedback, catalog)
    if concept_id is None:
        raise ValueError("feedback does not select an allowlisted concept")
    target_case_ids = _target_case_ids(selected_scope, catalog)
    if not target_case_ids:
        raise ValueError("selected rule scope has no target synthetic case")
    candidate = _candidate_hash(
        base_rules_version=base_rules_version,
        proposal_id=proposal_id,
        source_feedback_id=feedback.feedback_id,
        rule_type=RuleType.REQUIRE_CONCEPT_ID,
        scope=selected_scope,
        value=concept_id,
    )
    return RuleProposal(
        proposal_id=proposal_id,
        source_feedback_id=feedback.feedback_id,
        type=RuleType.REQUIRE_CONCEPT_ID,
        scope=selected_scope,
        condition_id=None,
        value=concept_id,
        rationale=(
            "Проверяемое правило переносит явно подтверждённое требование из замечания "
            "только на выбранную синтетическую область."
        ),
        target_case_ids=target_case_ids,
        base_rules_version=base_rules_version,
        candidate_rules_version=candidate,
        risk="low",
    )


def rule_matches_brief(rule: RuleProposal, brief: ReadyCampaignBrief) -> bool:
    scope = rule.scope
    if scope.product_ids and brief.product_id not in scope.product_ids:
        return False
    if scope.segment_ids and brief.segment_id not in scope.segment_ids:
        return False
    return scope.channel is None or scope.channel in brief.channels


def _case_matches_scope(rule: RuleProposal, case_id: str, catalog: SyntheticCatalog) -> bool:
    case = catalog.cases[case_id]
    brief = case.brief
    scope = rule.scope
    if scope.product_ids and brief.product_id not in scope.product_ids:
        return False
    if scope.segment_ids and brief.segment_id not in scope.segment_ids:
        return False
    return scope.channel is None or scope.channel in brief.channels


def _scoped_product_ids(proposal: RuleProposal, catalog: SyntheticCatalog) -> set[str]:
    if proposal.scope.product_ids:
        return set(proposal.scope.product_ids)
    return {
        str(case.brief.product_id)
        for case in catalog.cases.values()
        if case.brief.product_id is not None
        and (not proposal.scope.segment_ids or case.brief.segment_id in proposal.scope.segment_ids)
        and (proposal.scope.channel is None or proposal.scope.channel in case.brief.channels)
    }


def _protected_rule_texts(proposal: RuleProposal, catalog: SyntheticCatalog) -> set[str]:
    protected: set[str] = set()
    for product_id in _scoped_product_ids(proposal, catalog):
        product = catalog.products.get(product_id)
        if product is None:
            continue
        protected.add(product.fact_card.exact_name)
        for fact in product.facts:
            protected.add(fact.canonical_text)
            protected.update(fact.allowed_surface_forms)
        concept_ids = {
            *product.fact_card.mandatory_concept_ids,
            *product.fact_card.optional_concept_ids,
        }
        for concept_id in concept_ids:
            concept = catalog.concepts.get(concept_id)
            if concept is not None:
                protected.update(concept.accepted_surface_forms)
    for case in catalog.cases.values():
        if case.brief.cta_label and case.brief.product_id in _scoped_product_ids(proposal, catalog):
            protected.add(case.brief.cta_label)
    return {item.strip() for item in protected if item.strip()}


def _validate_rule_value(
    proposal: RuleProposal,
    catalog: SyntheticCatalog,
) -> list[str]:
    errors: list[str] = []
    scoped_products = _scoped_product_ids(proposal, catalog)
    if proposal.condition_id is not None:
        errors.append("RULE_CONDITION_NOT_ALLOWLISTED")
    if proposal.type is RuleType.REQUIRE_CONCEPT_ID:
        concept = catalog.concepts.get(proposal.value)
        if concept is None:
            errors.append("RULE_CONCEPT_NOT_ALLOWLISTED")
        for product_id in scoped_products:
            product = catalog.products.get(product_id)
            if product is None:
                continue
            if proposal.value not in {
                *product.fact_card.optional_concept_ids,
                *product.fact_card.mandatory_concept_ids,
            }:
                errors.append("RULE_CONCEPT_OUTSIDE_PRODUCT")
    elif proposal.type is RuleType.REQUIRE_FACT:
        for product_id in scoped_products:
            product = catalog.products.get(product_id)
            if product is None or proposal.value not in product.fact_card.allowed_fact_ids:
                errors.append("RULE_FACT_OUTSIDE_PRODUCT")
    elif proposal.type in PHRASE_RULE_TYPES:
        if FACTUAL_PHRASE_VALUE.search(proposal.value):
            errors.append("RULE_PHRASE_FACTUAL_VALUE_UNSAFE")
        if proposal.type is RuleType.FORBID_PHRASE:
            value = proposal.value.casefold()
            if any(
                value in protected.casefold() or protected.casefold() in value
                for protected in _protected_rule_texts(proposal, catalog)
            ):
                errors.append("RULE_PROTECTED_CONTENT_CONFLICT")
    elif proposal.type is RuleType.TONE_HINT:
        allowed_tones = {
            persona.tone_preference.casefold() for persona in catalog.personas.values()
        } | {str(case.brief.tone).casefold() for case in catalog.cases.values() if case.brief.tone}
        if proposal.value.casefold() not in allowed_tones:
            errors.append("RULE_TONE_NOT_ALLOWLISTED")
    return errors


def validate_rule_proposal(
    *,
    proposal: RuleProposal,
    feedback: FeedbackView,
    selected_scope: RuleScope,
    current_rules_version: str,
    catalog: SyntheticCatalog,
) -> RuleValidationResult:
    errors: list[str] = []
    if proposal.source_feedback_id != feedback.feedback_id:
        errors.append("RULE_SOURCE_FEEDBACK_MISMATCH")
    if proposal.scope != selected_scope:
        errors.append("RULE_SCOPE_MISMATCH")
    if proposal.base_rules_version != current_rules_version:
        errors.append("RULE_BASE_VERSION_STALE")
    expected_candidate = _candidate_hash(
        base_rules_version=proposal.base_rules_version,
        proposal_id=proposal.proposal_id,
        source_feedback_id=proposal.source_feedback_id,
        rule_type=proposal.type,
        scope=proposal.scope,
        value=proposal.value,
    )
    if proposal.candidate_rules_version != expected_candidate:
        errors.append("RULE_CANDIDATE_HASH_MISMATCH")
    if UNSAFE_RULE_VALUE.search(proposal.value):
        errors.append("RULE_VALUE_UNSAFE")
    errors.extend(_validate_rule_value(proposal, catalog))
    for product_id in proposal.scope.product_ids:
        if product_id not in catalog.products:
            errors.append("RULE_PRODUCT_NOT_FOUND")
    if not set(proposal.target_case_ids).issubset(catalog.cases):
        errors.append("RULE_TARGET_CASE_NOT_FOUND")

    tests: list[RuleTestResult] = []
    for case_id in proposal.target_case_ids:
        if case_id not in catalog.cases:
            continue
        actual = _case_matches_scope(proposal, case_id, catalog)
        tests.append(
            RuleTestResult(
                case_id=case_id,
                test_kind="target",
                expected_applied=True,
                actual_applied=actual,
                passed=actual,
                detail="Правило должно примениться к подходящему синтетическому кейсу.",
            )
        )
    regression_ids = [
        case_id
        for case_id in ("B04", "B06", "B11", "B13")
        if case_id in catalog.cases and case_id not in proposal.target_case_ids
    ][:4]
    for case_id in regression_ids:
        actual = _case_matches_scope(proposal, case_id, catalog)
        tests.append(
            RuleTestResult(
                case_id=case_id,
                test_kind="regression",
                expected_applied=False,
                actual_applied=actual,
                passed=not actual,
                detail="Несвязанный синтетический кейс не должен получить правило.",
            )
        )
    out_of_scope_id = next(
        (
            case_id
            for case_id in sorted(catalog.cases)
            if case_id not in proposal.target_case_ids
            and case_id not in regression_ids
            and not _case_matches_scope(proposal, case_id, catalog)
        ),
        None,
    )
    if out_of_scope_id is None:
        errors.append("RULE_OUT_OF_SCOPE_FIXTURE_MISSING")
    else:
        actual = _case_matches_scope(proposal, out_of_scope_id, catalog)
        tests.append(
            RuleTestResult(
                case_id=out_of_scope_id,
                test_kind="out_of_scope",
                expected_applied=False,
                actual_applied=actual,
                passed=not actual,
                detail="Явный отрицательный сценарий подтверждает границу области.",
            )
        )
    if len(regression_ids) < 3:
        errors.append("RULE_REGRESSION_FIXTURES_INSUFFICIENT")
    if any(not test.passed for test in tests):
        errors.append("RULE_TEST_FAILED")
    return RuleValidationResult(tuple(dict.fromkeys(errors)), tuple(tests))


def active_rule_payload(*, rule_version_id: str, proposal: RuleProposal) -> dict[str, Any]:
    return {
        "rule_version_id": rule_version_id,
        "proposal_id": proposal.proposal_id,
        "type": proposal.type.value,
        "scope": proposal.scope.model_dump(mode="json"),
        "condition_id": proposal.condition_id,
        "value": proposal.value,
        "source_feedback_id": proposal.source_feedback_id,
        "candidate_rules_version": proposal.candidate_rules_version,
    }


def active_rules_hash(active_rules: list[dict[str, Any]]) -> str:
    return hash_value(sorted(active_rules, key=lambda item: str(item.get("rule_version_id") or "")))


def apply_active_rules(
    context: ContextBundle,
    *,
    active_rules: list[dict[str, Any]],
    rules_version: str,
) -> ContextBundle:
    selected_facts = list(context.content_plan.selected_fact_ids)
    selected_concepts = list(context.content_plan.selected_concept_ids)
    applied_ids = list(context.content_plan.applied_rule_version_ids)
    matching: list[dict[str, Any]] = []
    for raw in active_rules:
        try:
            proposal = RuleProposal.model_validate(
                {
                    "proposal_id": raw["proposal_id"],
                    "source_feedback_id": raw["source_feedback_id"],
                    "type": raw["type"],
                    "scope": raw["scope"],
                    "condition_id": raw.get("condition_id"),
                    "value": raw["value"],
                    "rationale": "Approved bounded rule.",
                    "target_case_ids": ["B03"],
                    "base_rules_version": context.rules_version,
                    "candidate_rules_version": raw["candidate_rules_version"],
                    "risk": "low",
                }
            )
        except (KeyError, ValueError):
            continue
        if not rule_matches_brief(proposal, context.brief_snapshot):
            continue
        if proposal.condition_id is not None or UNSAFE_RULE_VALUE.search(proposal.value):
            continue
        if proposal.type is RuleType.REQUIRE_CONCEPT_ID:
            if proposal.value not in {
                *context.product.mandatory_concept_ids,
                *context.content_plan.available_optional_concept_ids,
            }:
                continue
            if proposal.value not in selected_concepts:
                selected_concepts.append(proposal.value)
        elif proposal.type is RuleType.REQUIRE_FACT:
            if proposal.value not in context.product.allowed_fact_ids:
                continue
            if proposal.value not in selected_facts:
                selected_facts.append(proposal.value)
        elif proposal.type in PHRASE_RULE_TYPES:
            if FACTUAL_PHRASE_VALUE.search(proposal.value):
                continue
        elif proposal.type is RuleType.TONE_HINT:
            if len(proposal.value) > 200:
                continue
        rule_version_id = str(raw.get("rule_version_id") or "")
        if rule_version_id and rule_version_id not in applied_ids:
            applied_ids.append(rule_version_id)
        matching.append(raw)
    selection_sources = list(context.content_plan.selection_sources)
    if matching and "rule" not in selection_sources:
        selection_sources.append("rule")
    payload = context.model_dump(mode="json")
    payload.update(
        {
            "context_version": "0" * 64,
            "active_rules": matching,
            "rules_version": rules_version,
            "content_plan": {
                **context.content_plan.model_dump(mode="json"),
                "selected_fact_ids": selected_facts,
                "selected_concept_ids": selected_concepts,
                "selection_sources": selection_sources,
                "applied_rule_version_ids": applied_ids,
            },
        }
    )
    payload["context_version"] = hash_value(
        {key: value for key, value in payload.items() if key != "context_version"}
    )
    return ContextBundle.model_validate(payload)
