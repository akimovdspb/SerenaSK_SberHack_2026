from __future__ import annotations

import json
import pathlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import Field, ValidationError

from apps.api.app.domain.campaigns import (
    ConceptDefinition,
    ContactPolicy,
    LegalPolicy,
    PersonaContext,
    SyntheticCase,
    SyntheticProduct,
)
from apps.api.app.domain.models import StrictModel

DEFAULT_DATA_DIR = pathlib.Path(__file__).resolve().parents[4] / "data" / "synthetic"


class CatalogError(RuntimeError):
    pass


class _ProductsDocument(StrictModel):
    schema_version: int
    seed: str
    products: list[SyntheticProduct] = Field(min_length=5)


class _PersonasDocument(StrictModel):
    schema_version: int
    personas: list[PersonaContext] = Field(min_length=8)


class _PoliciesDocument(StrictModel):
    schema_version: int
    contact_policies: list[ContactPolicy]
    legal_policies: list[LegalPolicy]
    concepts: list[ConceptDefinition]
    channel_policies: dict[str, Any]
    rules_version: str


class _CasesDocument(StrictModel):
    schema_version: int
    cases: list[SyntheticCase] = Field(min_length=5)


class _HistoryDocument(StrictModel):
    schema_version: int
    histories: list[dict[str, Any]] = Field(min_length=8)


def _unique_map(items: Iterable[Any], field: str, label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in items:
        key = str(getattr(item, field, "") or "")
        if not key or key in result:
            raise CatalogError(f"synthetic {label} identifiers must be non-empty and unique")
        result[key] = item
    return result


def _load(path: pathlib.Path) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"synthetic data file is unreadable: {path.name}") from exc
    if not isinstance(value, dict):
        raise CatalogError(f"synthetic data file must contain an object: {path.name}")
    return value


@dataclass(frozen=True)
class SyntheticCatalog:
    seed: str
    products: dict[str, SyntheticProduct]
    personas: dict[str, PersonaContext]
    cases: dict[str, SyntheticCase]
    contact_policies: dict[str, ContactPolicy]
    legal_policies: dict[str, LegalPolicy]
    concepts: dict[str, ConceptDefinition]
    channel_policies: dict[str, Any]
    touch_histories: dict[str, dict[str, Any]]
    rules_version: str

    def case(self, case_id: str) -> SyntheticCase:
        try:
            return self.cases[case_id]
        except KeyError as exc:
            raise CatalogError("unknown synthetic case") from exc


def load_catalog(data_dir: pathlib.Path = DEFAULT_DATA_DIR) -> SyntheticCatalog:
    try:
        products_doc = _ProductsDocument.model_validate(
            _load(data_dir / "products" / "products.json")
        )
        personas_doc = _PersonasDocument.model_validate(
            _load(data_dir / "segments" / "personas.json")
        )
        policies_doc = _PoliciesDocument.model_validate(
            _load(data_dir / "policies" / "policies.json")
        )
        cases_doc = _CasesDocument.model_validate(_load(data_dir / "cases" / "gate1.json"))
        history_doc = _HistoryDocument.model_validate(
            _load(data_dir / "touch_history" / "history.json")
        )
    except ValidationError as exc:
        raise CatalogError("synthetic catalog does not match its strict schema") from exc

    products = {item.fact_card.product_id: item for item in products_doc.products}
    if len(products) != len(products_doc.products):
        raise CatalogError("synthetic product identifiers must be unique")
    personas = _unique_map(personas_doc.personas, "segment_id", "segment")
    cases = _unique_map(cases_doc.cases, "case_id", "case")
    contact_policies = _unique_map(policies_doc.contact_policies, "policy_id", "contact policy")
    legal_policies = _unique_map(policies_doc.legal_policies, "policy_id", "legal policy")
    concepts = _unique_map(policies_doc.concepts, "concept_id", "concept")
    histories: dict[str, dict[str, Any]] = {}
    for raw in history_doc.histories:
        history_id = str(raw.get("history_id") or "")
        if not history_id or history_id in histories or raw.get("synthetic") is not True:
            raise CatalogError("synthetic touch history identifiers or flags are invalid")
        histories[history_id] = raw

    for product in products.values():
        card = product.fact_card
        if (
            card.legal_policy_id not in legal_policies
            or card.contact_policy_id not in contact_policies
        ):
            raise CatalogError("synthetic product references an unknown policy")
        fact_ids = [fact.fact_id for fact in product.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise CatalogError("synthetic product fact identifiers must be unique")
        if not set(fact_ids).issubset(card.allowed_fact_ids):
            raise CatalogError("synthetic product contains a fact outside its allowlist")
        legal = legal_policies[card.legal_policy_id]
        if not set(legal.required_disclaimer_ids).issubset(card.required_disclaimer_ids):
            raise CatalogError("fact-card omits a disclaimer required by its legal policy")
        concept_ids = set(card.mandatory_concept_ids) | set(card.optional_concept_ids)
        if not concept_ids.issubset(concepts):
            raise CatalogError("synthetic product references an unknown concept")
    for persona in personas.values():
        if persona.touch_history_id not in histories:
            raise CatalogError("synthetic persona references an unknown touch history")
    for case in cases.values():
        product_id = str(case.brief.product_id or "")
        segment_id = str(case.brief.segment_id or "")
        if product_id not in products or segment_id not in personas:
            raise CatalogError("synthetic case references an unknown product or segment")
    return SyntheticCatalog(
        seed=products_doc.seed,
        products=products,
        personas=personas,
        cases=cases,
        contact_policies=contact_policies,
        legal_policies=legal_policies,
        concepts=concepts,
        channel_policies=policies_doc.channel_policies,
        touch_histories=histories,
        rules_version=policies_doc.rules_version,
    )
