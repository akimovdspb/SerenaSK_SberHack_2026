from __future__ import annotations

import json
import re

import pytest
from pydantic import TypeAdapter, ValidationError

from apps.api.app.domain.campaigns import FactLedgerItem
from apps.api.app.domain.models import NormalizedValue
from apps.api.app.services.catalog import DEFAULT_DATA_DIR, load_catalog


def test_catalog_has_fixed_synthetic_minimum_and_gate1_cases() -> None:
    catalog = load_catalog()

    assert catalog.seed == "communication-factory-p0-v1"
    assert len(catalog.products) == 6
    assert len(catalog.personas) == 9
    assert set(catalog.cases) == {f"B{ordinal:02d}" for ordinal in range(1, 16)}
    assert all(product.fact_card.synthetic for product in catalog.products.values())
    assert all(persona.synthetic for persona in catalog.personas.values())
    assert all(case.synthetic and case.brief.synthetic for case in catalog.cases.values())


def test_catalog_uses_only_reserved_urls_and_contains_no_contact_pii() -> None:
    catalog = load_catalog()
    serialized = json.dumps(
        {
            "products": [item.model_dump(mode="json") for item in catalog.products.values()],
            "personas": [item.model_dump(mode="json") for item in catalog.personas.values()],
            "cases": [item.model_dump(mode="json") for item in catalog.cases.values()],
        },
        ensure_ascii=False,
    )

    urls = re.findall(r"https://[^\s\"']+", serialized)
    assert urls
    assert all(re.match(r"^https://[^/]+\.(?:test|invalid)(?:/|$)", url) for url in urls)
    assert not re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", serialized)
    assert not re.search(r"(?:\+7|8)[\s()-]*\d{3}[\s()-]*\d{3}", serialized)


def test_every_catalog_document_is_strict_json() -> None:
    paths = sorted(DEFAULT_DATA_DIR.glob("*/*.json"))

    assert len(paths) == 7
    assert all(isinstance(json.loads(path.read_text(encoding="utf-8")), dict) for path in paths)


def test_every_catalog_fact_uses_the_closed_p0_normalized_value_union() -> None:
    catalog = load_catalog()
    adapter = TypeAdapter(NormalizedValue)
    values = [
        fact.normalized_value for product in catalog.products.values() for fact in product.facts
    ]

    assert values
    assert all(adapter.validate_python(value) == value for value in values)


def test_catalog_fact_rejects_unsupported_normalized_object_at_admission() -> None:
    product = next(iter(load_catalog().products.values()))
    source = product.facts[0].model_dump(mode="json")
    source["normalized_value"] = {"arbitrary": {"nested": "value"}}

    with pytest.raises(ValidationError):
        FactLedgerItem.model_validate(source)
