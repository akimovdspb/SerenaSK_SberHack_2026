from __future__ import annotations

import hashlib
import json
import pathlib
from datetime import date
from typing import Any

from pydantic import TypeAdapter, ValidationError

from apps.api.app.domain.authoring import (
    AuthoringFactView,
    AuthoringPersonaView,
    AuthoringProductView,
    CustomProductCreateRequest,
    CustomProductRecord,
    EditorialReferencePrefill,
)
from apps.api.app.domain.campaigns import (
    FactLedgerItem,
    ProductFactCard,
    SyntheticProduct,
)
from apps.api.app.domain.models import Channel, ClaimType
from apps.api.app.services.briefs import hash_value
from apps.api.app.services.catalog import SyntheticCatalog

EDITORIAL_REFERENCE_LABEL = "EDITORIAL_REFERENCE_NOT_LIVE_NOT_RELEASE_EVIDENCE"


class EditorialReferenceError(RuntimeError):
    pass


def custom_product_id(exact_name: str, *, version: int) -> str:
    normalized = " ".join(exact_name.casefold().split())
    family = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:18]
    return f"custom_{family}_v{version}"


def _stable_id(prefix: str, *parts: str, size: int = 20) -> str:
    value = "\x1f".join(parts)
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:size]}"


def materialize_custom_product(
    request: CustomProductCreateRequest,
    *,
    version: int,
) -> CustomProductRecord:
    product_id = custom_product_id(request.exact_name, version=version)
    facts: list[FactLedgerItem] = []
    fact_labels: dict[str, str] = {}
    source_labels: dict[str, str] = {}
    for item in request.facts:
        source_id = _stable_id(
            "source_custom",
            product_id,
            str(version),
            item.source_label.casefold(),
        )
        fact_id = _stable_id(
            "fact_custom",
            product_id,
            str(version),
            item.label.casefold(),
            item.canonical_text,
        )
        surfaces = tuple(dict.fromkeys((item.canonical_text, *item.allowed_surface_forms)))
        facts.append(
            FactLedgerItem(
                fact_id=fact_id,
                source_id=source_id,
                kind=item.kind,
                canonical_text=item.canonical_text,
                normalized_value=item.normalized_value,
                allowed_surface_forms=surfaces,
            )
        )
        fact_labels[fact_id] = item.label
        source_labels[source_id] = item.source_label

    cta_source_id = _stable_id(
        "source_custom",
        product_id,
        str(version),
        "cta",
    )
    cta_fact_id = _stable_id(
        "fact_custom",
        product_id,
        str(version),
        "cta",
        request.cta_url,
    )
    facts.append(
        FactLedgerItem(
            fact_id=cta_fact_id,
            source_id=cta_source_id,
            kind=ClaimType.URL,
            canonical_text=request.cta_url,
            normalized_value=request.cta_url,
            allowed_surface_forms=(request.cta_url,),
        )
    )
    fact_labels[cta_fact_id] = "Ссылка действия"
    source_labels[cta_source_id] = "Карточка действия"

    fact_ids = tuple(item.fact_id for item in facts)
    sms_ids = tuple([*(item.fact_id for item in facts[:-1][:2]), cta_fact_id])
    card = ProductFactCard(
        product_id=product_id,
        version=version,
        exact_name=request.exact_name,
        allowed_fact_ids=fact_ids,
        mandatory_fact_ids=fact_ids,
        mandatory_concept_ids=(),
        optional_concept_ids=(),
        required_disclaimer_ids=(),
        prohibited_claim_ids=("claim_guaranteed_result", "claim_best_market"),
        allowed_cta_urls=(request.cta_url,),
        legal_policy_id="legal_base_v1",
        contact_policy_id="contact_standard_v1",
        channel_required_fact_ids={
            Channel.SMS: sms_ids,
            Channel.EMAIL: fact_ids,
        },
        validity_start=date(2026, 1, 1),
        validity_end=date(2099, 12, 31),
    )
    request_payload = request.model_dump(
        mode="json",
        exclude={"synthetic_confirmed", "no_pii_confirmed"},
    )
    return CustomProductRecord(
        product=SyntheticProduct(fact_card=card, facts=tuple(facts)),
        cta_label=request.cta_label,
        fact_labels=fact_labels,
        source_labels=source_labels,
        request_hash=hash_value(request_payload),
    )


def custom_product_view(record: CustomProductRecord) -> AuthoringProductView:
    card = record.product.fact_card
    by_id = {fact.fact_id: fact for fact in record.product.facts}
    return AuthoringProductView(
        product_id=card.product_id,
        version=card.version,
        exact_name=card.exact_name,
        cta_label=record.cta_label,
        cta_url=card.allowed_cta_urls[0],
        facts=tuple(
            AuthoringFactView(
                fact_id=fact_id,
                source_id=by_id[fact_id].source_id,
                label=record.fact_labels[fact_id],
                canonical_text=by_id[fact_id].canonical_text,
                kind=by_id[fact_id].kind,
                source_label=record.source_labels[by_id[fact_id].source_id],
                normalized_value=by_id[fact_id].normalized_value,
            )
            for fact_id in card.allowed_fact_ids
        ),
        origin="custom",
    )


def catalog_product_view(product_id: str, catalog: SyntheticCatalog) -> AuthoringProductView:
    product = catalog.products[product_id]
    card = product.fact_card
    case = next(
        (
            item
            for item in catalog.cases.values()
            if item.brief.product_id == product_id and item.brief.cta_url in card.allowed_cta_urls
        ),
        None,
    )
    cta_label = case.brief.cta_label if case and case.brief.cta_label else "Подробнее"
    cta_url = case.brief.cta_url if case and case.brief.cta_url else card.allowed_cta_urls[0]
    return AuthoringProductView(
        product_id=product_id,
        version=card.version,
        exact_name=card.exact_name,
        cta_label=cta_label,
        cta_url=cta_url,
        facts=tuple(
            AuthoringFactView(
                fact_id=fact.fact_id,
                source_id=fact.source_id,
                label=fact.canonical_text,
                canonical_text=fact.canonical_text,
                kind=fact.kind,
                source_label="Проверенная синтетическая факт-карточка",
                normalized_value=fact.normalized_value,
            )
            for fact in product.facts
            if fact.kind is not ClaimType.URL
        ),
        origin="catalog",
    )


def persona_views(catalog: SyntheticCatalog) -> tuple[AuthoringPersonaView, ...]:
    return tuple(
        AuthoringPersonaView(
            segment_id=item.segment_id,
            trigger_id=item.trigger_id,
            label=f"{item.business_stage} · {item.company_size_band}",
            tone_hint=item.tone_preference,
            connected_product_ids=item.connected_product_ids,
            available_channels=tuple(
                channel.value for channel, allowed in item.channel_consent.items() if allowed
            ),
        )
        for item in sorted(catalog.personas.values(), key=lambda value: value.segment_id)
    )


def load_reference_prefills(path: pathlib.Path) -> tuple[EditorialReferencePrefill, ...]:
    if not path.exists():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise EditorialReferenceError("editorial reference document has an invalid version")
        raw_references = payload.get("references")
        if not isinstance(raw_references, list):
            raise EditorialReferenceError("editorial reference document has no reference list")
        public: list[dict[str, Any]] = []
        for item in raw_references:
            if not isinstance(item, dict):
                raise EditorialReferenceError("editorial reference entry is not an object")
            if item.get("label") != EDITORIAL_REFERENCE_LABEL:
                raise EditorialReferenceError("editorial reference label is missing")
            saved = item.get("saved_draft")
            if not isinstance(saved, dict) or not {"sms", "email"}.issubset(saved):
                raise EditorialReferenceError("editorial reference draft is incomplete")
            public.append(
                {
                    key: item.get(key)
                    for key in (
                        "reference_id",
                        "title",
                        "description",
                        "label",
                        "brief",
                        "custom_product",
                    )
                }
            )
        return tuple(TypeAdapter(list[EditorialReferencePrefill]).validate_python(public))
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise EditorialReferenceError("editorial reference document is invalid") from exc
