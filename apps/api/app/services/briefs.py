from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any

from pydantic_core import to_jsonable_python

from apps.api.app.domain.campaigns import (
    BriefQuestion,
    BriefStatus,
    BriefValidationResult,
    CampaignBriefDraft,
    CampaignBriefInput,
    ContentPlan,
    ContextBundle,
    OfferPeriod,
    ReadyCampaignBrief,
    SourceManifestItem,
)
from apps.api.app.domain.models import Channel, Operation
from apps.api.app.services.catalog import CatalogError, SyntheticCatalog

CATALOG_RETRIEVED_AT = datetime(2026, 7, 11, tzinfo=UTC)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        to_jsonable_python(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def hash_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def create_draft(
    *,
    campaign_id: str,
    values: CampaignBriefInput,
    version: int = 1,
) -> CampaignBriefDraft:
    payload = values.model_dump(mode="json")
    identity = {"campaign_id": campaign_id, "version": version, **payload}
    return CampaignBriefDraft.model_validate({**identity, "input_hash": hash_value(identity)})


def _question(
    question_id: str,
    path: str,
    reason: str,
    message: str,
    options: list[str] | None = None,
) -> BriefQuestion:
    return BriefQuestion(
        question_id=question_id,
        path=path,
        reason=reason,
        message=message,
        options=options or [],
    )


def _missing_questions(draft: CampaignBriefDraft, catalog: SyntheticCatalog) -> list[BriefQuestion]:
    questions: list[BriefQuestion] = []
    fields: list[tuple[str, Any, str]] = [
        ("name", draft.name, "Как назвать синтетическую кампанию?"),
        ("objective", draft.objective, "Какова цель коммуникации?"),
        ("product_id", draft.product_id, "Какой synthetic product выбран?"),
        ("segment_id", draft.segment_id, "Какой безопасный synthetic segment выбран?"),
        ("trigger_id", draft.trigger_id, "Какое lifecycle-событие запускает кампанию?"),
        ("channels", draft.channels, "Какие разрешённые каналы нужны?"),
        ("cta_label", draft.cta_label, "Как подписать ссылку действия?"),
        ("cta_url", draft.cta_url, "Какую разрешённую синтетическую ссылку использовать?"),
        ("tone", draft.tone, "Какой тон нужен?"),
    ]
    for field, value, message in fields:
        if value:
            continue
        options: list[str] = []
        if field == "product_id":
            options = sorted(catalog.products)[:5]
        elif field == "segment_id":
            options = sorted(catalog.personas)[:5]
        elif field == "channels":
            options = [Channel.SMS.value, Channel.EMAIL.value]
        questions.append(
            _question(
                f"missing_{field}",
                f"/{field}",
                f"Поле {field} обязательно для неизменяемого готового снимка.",
                message,
                options,
            )
        )
        if len(questions) == 5:
            break
    return questions


def validate_and_promote(
    draft: CampaignBriefDraft,
    catalog: SyntheticCatalog,
    *,
    as_of: date | None = None,
) -> BriefValidationResult:
    missing = _missing_questions(draft, catalog)
    if missing:
        return BriefValidationResult(
            status=BriefStatus.NEEDS_INPUT,
            campaign_id=draft.campaign_id,
            draft_version=draft.version,
            input_hash=draft.input_hash,
            questions=missing,
        )
    assert draft.product_id is not None
    assert draft.segment_id is not None
    product = catalog.products.get(draft.product_id)
    persona = catalog.personas.get(draft.segment_id)
    if product is None or persona is None:
        question = _question(
            "unknown_catalog_reference",
            "/product_id" if product is None else "/segment_id",
            "Выбранный идентификатор отсутствует в текущей версии каталога синтетических кейсов.",
            "Выберите доступный product и segment.",
        )
        return BriefValidationResult(
            status=BriefStatus.NEEDS_INPUT,
            campaign_id=draft.campaign_id,
            draft_version=draft.version,
            input_hash=draft.input_hash,
            questions=[question],
        )
    if product.fact_card.product_id in persona.connected_product_ids:
        return BriefValidationResult(
            status=BriefStatus.NOT_APPLICABLE,
            campaign_id=draft.campaign_id,
            draft_version=draft.version,
            input_hash=draft.input_hash,
            blockers=["PRODUCT_ALREADY_ACTIVE"],
        )
    card = product.fact_card
    effective_date = as_of or datetime.now(UTC).date()
    if not card.validity_start <= effective_date <= card.validity_end:
        return BriefValidationResult(
            status=BriefStatus.NEEDS_INPUT,
            campaign_id=draft.campaign_id,
            draft_version=draft.version,
            input_hash=draft.input_hash,
            questions=[
                _question(
                    "product_validity_missing",
                    "/product_id",
                    "Fact-card не действует на дату подготовки кампании.",
                    "Выберите действующую версию синтетической факт-карточки продукта.",
                )
            ],
            blockers=["PRODUCT_FACT_CARD_NOT_CURRENT"],
        )
    legal = catalog.legal_policies.get(card.legal_policy_id)
    contact = catalog.contact_policies.get(card.contact_policy_id)
    if legal is None or contact is None:
        raise CatalogError("ready promotion references an unavailable policy")
    permitted = [
        channel
        for channel in draft.channels
        if not contact.require_channel_consent or persona.channel_consent.get(channel)
    ]
    touch_count = int(catalog.touch_histories[persona.touch_history_id].get("touches", 0))
    frequency_blocked = (
        persona.frequency_cap_reached or touch_count >= contact.max_touches_per_window
    )
    if frequency_blocked or not permitted:
        return BriefValidationResult(
            status=BriefStatus.BLOCKED,
            campaign_id=draft.campaign_id,
            draft_version=draft.version,
            input_hash=draft.input_hash,
            blockers=[
                "CONTACT_FREQUENCY_BLOCKED" if frequency_blocked else "CONTACT_CHANNELS_BLOCKED"
            ],
        )
    fact_ids = {item.fact_id for item in product.facts}
    required_fact_ids = set(card.mandatory_fact_ids) | set(card.required_disclaimer_ids)
    missing_facts = sorted(required_fact_ids - fact_ids)
    if missing_facts:
        return BriefValidationResult(
            status=BriefStatus.NEEDS_INPUT,
            campaign_id=draft.campaign_id,
            draft_version=draft.version,
            input_hash=draft.input_hash,
            questions=[
                _question(
                    "critical_fact_missing",
                    "/product_id",
                    "Текущая карточка фактов не содержит обязательный подтверждённый факт.",
                    "Обновите синтетическую карточку фактов до запуска генерации.",
                )
            ],
            blockers=["CRITICAL_FACT_MISSING"],
        )
    if draft.cta_url not in card.allowed_cta_urls:
        return BriefValidationResult(
            status=BriefStatus.NEEDS_INPUT,
            campaign_id=draft.campaign_id,
            draft_version=draft.version,
            input_hash=draft.input_hash,
            questions=[
                _question(
                    "cta_url_not_allowed",
                    "/cta_url",
                    (
                        "Ссылка действия отсутствует в списке разрешённых адресов "
                        "выбранной факт-карточки."
                    ),
                    "Выберите разрешённую синтетическую HTTPS-ссылку.",
                    list(card.allowed_cta_urls),
                )
            ],
        )
    assert draft.name is not None
    assert draft.objective is not None
    assert draft.trigger_id is not None
    assert draft.cta_label is not None
    assert draft.cta_url is not None
    assert draft.tone is not None
    ready = ReadyCampaignBrief(
        campaign_id=draft.campaign_id,
        name=draft.name,
        objective=draft.objective,
        product_id=draft.product_id,
        segment_id=draft.segment_id,
        trigger_id=draft.trigger_id,
        channels=draft.channels,
        cta_label=draft.cta_label,
        cta_url=draft.cta_url,
        tone=draft.tone,
        mandatory_fact_ids=card.mandatory_fact_ids,
        mandatory_concept_ids=card.mandatory_concept_ids,
        prohibited_claim_ids=sorted(
            set(card.prohibited_claim_ids) | set(legal.prohibited_claim_ids)
        ),
        legal_policy_id=legal.policy_id,
        contact_policy_id=contact.policy_id,
        offer_period=draft.offer_period or OfferPeriod(),
        notes=draft.notes or "",
        version=draft.version,
        input_hash=draft.input_hash,
    )
    return BriefValidationResult(
        status=BriefStatus.READY,
        campaign_id=draft.campaign_id,
        draft_version=draft.version,
        input_hash=draft.input_hash,
        ready_brief=ready,
    )


def build_initial_context(
    ready: ReadyCampaignBrief,
    catalog: SyntheticCatalog,
) -> ContextBundle:
    product = catalog.products[ready.product_id]
    persona = catalog.personas[ready.segment_id]
    contact = catalog.contact_policies[ready.contact_policy_id]
    legal = catalog.legal_policies[ready.legal_policy_id]
    facts = [item for item in product.facts if item.fact_id in product.fact_card.allowed_fact_ids]
    selected = list(product.fact_card.mandatory_fact_ids)
    concept_ids = set(product.fact_card.mandatory_concept_ids) | set(
        product.fact_card.optional_concept_ids
    )
    concepts = [catalog.concepts[concept_id] for concept_id in sorted(concept_ids)]
    plan = ContentPlan(
        selected_fact_ids=selected,
        channel_selected_fact_ids={
            channel: product.fact_card.channel_required_fact_ids.get(
                channel,
                tuple(selected),
            )
            for channel in ready.channels
        },
        selected_concept_ids=list(product.fact_card.mandatory_concept_ids),
        available_optional_fact_ids=sorted(set(product.fact_card.allowed_fact_ids) - set(selected)),
        available_optional_concept_ids=list(product.fact_card.optional_concept_ids),
        selection_sources=["base_policy"],
        applied_rule_version_ids=[],
    )
    source_ids = sorted({item.source_id for item in facts})
    manifest = [
        SourceManifestItem(
            source_id=source_id,
            version=(
                str(product.fact_card.version)
                if product.fact_card.product_id.startswith("custom_")
                else "1"
            ),
            retrieved_at=CATALOG_RETRIEVED_AT,
        )
        for source_id in source_ids
    ]
    payload = {
        "classification": "untrusted_data",
        "context_version": "0" * 64,
        "operation": Operation.INITIAL,
        "brief_snapshot": ready,
        "product": product.fact_card,
        "facts": facts,
        "concepts": concepts,
        "persona": persona,
        "touch_history": catalog.touch_histories[persona.touch_history_id],
        "contact_policy": contact,
        "channel_policies": catalog.channel_policies,
        "legal_policy": legal,
        "active_rules": [],
        "source_manifest": manifest,
        "prompt_version": "2.0.0",
        "rules_version": catalog.rules_version,
        "content_plan": plan,
        "previous_package": None,
        "feedback": None,
        "allowed_changed_paths": [],
        "protected_paths": [],
        "protected_hashes": {},
        "output_schema_id": "communication_bundle:1.0",
    }
    payload["context_version"] = hash_value(
        {key: value for key, value in payload.items() if key != "context_version"}
    )
    return ContextBundle.model_validate(payload)
