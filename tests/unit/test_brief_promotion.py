from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from apps.api.app.domain.campaigns import BriefStatus, CampaignBriefInput
from apps.api.app.domain.models import ClaimType, Operation
from apps.api.app.services.briefs import (
    build_initial_context,
    create_draft,
    validate_and_promote,
)
from apps.api.app.services.catalog import SyntheticCatalog, load_catalog

AS_OF = date(2026, 7, 11)


@pytest.fixture(scope="module")
def catalog() -> SyntheticCatalog:
    return load_catalog()


def _validate_case(case_id: str, catalog: SyntheticCatalog):  # type: ignore[no-untyped-def]
    case = catalog.case(case_id)
    draft = create_draft(campaign_id=case.campaign_id, values=case.brief)
    return case, draft, validate_and_promote(draft, catalog, as_of=AS_OF)


@pytest.mark.parametrize("case_id", ["B11", "B12", "B13"])
def test_controlled_gate1_outcomes_never_call_llm(
    case_id: str,
    catalog: SyntheticCatalog,
) -> None:
    case, _, result = _validate_case(case_id, catalog)

    assert result.status is case.expected.status
    assert set(result.blockers) == set(case.expected.blocker_codes)
    assert result.ready_brief is None
    assert result.llm_calls == 0


def test_b04_promotes_exact_grounded_duration_and_stable_context(
    catalog: SyntheticCatalog,
) -> None:
    case, _, result = _validate_case("B04", catalog)

    assert result.status is BriefStatus.READY
    assert result.ready_brief is not None
    first = build_initial_context(result.ready_brief, catalog)
    second = build_initial_context(result.ready_brief, catalog)
    duration = next(item for item in first.facts if item.fact_id == "fact_term_14_days")

    assert first.operation is Operation.INITIAL
    assert first.context_version == second.context_version
    assert set(first.content_plan.selected_fact_ids) == set(case.expected.selected_fact_ids)
    assert first.content_plan.selected_concept_ids == ()
    assert "concept_online_connection" in first.content_plan.available_optional_concept_ids
    assert duration.kind is ClaimType.DURATION
    assert duration.normalized_value == {"value": 14, "unit": "day"}
    assert "14 дней" in duration.allowed_surface_forms


def test_b06_selects_required_synthetic_disclaimer(catalog: SyntheticCatalog) -> None:
    case, _, result = _validate_case("B06", catalog)

    assert result.status is BriefStatus.READY
    assert result.ready_brief is not None
    context = build_initial_context(result.ready_brief, catalog)
    selected = set(context.content_plan.selected_fact_ids)
    facts = {item.fact_id: item for item in context.facts}

    assert set(case.expected.required_disclaimer_ids).issubset(selected)
    assert facts["fact_label_notice"].canonical_text == ("Учебное предложение. Условия вымышлены.")
    assert context.legal_policy.required_disclaimer_ids == ("fact_label_notice",)


def test_semantically_incomplete_draft_returns_at_most_five_questions(
    catalog: SyntheticCatalog,
) -> None:
    draft = create_draft(
        campaign_id="campaign_incomplete",
        values=CampaignBriefInput(),
    )

    result = validate_and_promote(draft, catalog, as_of=AS_OF)

    assert result.status is BriefStatus.NEEDS_INPUT
    assert len(result.questions) == 5
    assert result.llm_calls == 0


def test_non_allowlisted_cta_is_a_domain_question_not_schema_failure(
    catalog: SyntheticCatalog,
) -> None:
    case = catalog.case("B04")
    values = case.brief.model_copy(update={"cta_url": "http://outside.example.com/path"})
    draft = create_draft(campaign_id="campaign_bad_cta", values=values)

    result = validate_and_promote(draft, catalog, as_of=AS_OF)

    assert result.status is BriefStatus.NEEDS_INPUT
    assert [question.question_id for question in result.questions] == ["cta_url_not_allowed"]
    assert result.llm_calls == 0


def test_ready_snapshot_and_nested_offer_period_are_immutable(
    catalog: SyntheticCatalog,
) -> None:
    _, _, result = _validate_case("B04", catalog)
    assert result.ready_brief is not None

    with pytest.raises(ValidationError):
        result.ready_brief.name = "Изменённое имя"
    with pytest.raises(ValidationError):
        result.ready_brief.offer_period.start = date(2026, 1, 1)
    with pytest.raises(TypeError):
        result.ready_brief.channels[0] = result.ready_brief.channels[0]  # type: ignore[index]


def test_draft_hash_is_deterministic_and_source_version_is_preserved(
    catalog: SyntheticCatalog,
) -> None:
    case = catalog.case("B04")

    first = create_draft(campaign_id=case.campaign_id, values=case.brief, version=3)
    second = create_draft(campaign_id=case.campaign_id, values=case.brief, version=3)
    result = validate_and_promote(first, catalog, as_of=AS_OF)

    assert first.input_hash == second.input_hash
    assert result.ready_brief is not None
    assert result.ready_brief.version == 3
    assert result.ready_brief.input_hash == first.input_hash
