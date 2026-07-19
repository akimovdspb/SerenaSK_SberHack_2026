from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from apps.api.app.domain.campaigns import CampaignBriefInput, FrozenStrictModel, SyntheticProduct
from apps.api.app.domain.models import ClaimType, Identifier, NormalizedValue, StrictModel

SafeAuthoringText = Annotated[str, Field(min_length=1, max_length=1_000)]
SafeShortLabel = Annotated[str, Field(min_length=1, max_length=160)]

_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_PHONE = re.compile(r"(?<!\d)(?:\+7|8)[\s()\-]*\d{3}[\s()\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
_URL = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)


def _reject_contact_pii(value: str) -> str:
    if _EMAIL.search(value) or _PHONE.search(value):
        raise ValueError("authoring text must not contain contact PII")
    return value


class CustomFactInput(StrictModel):
    label: SafeShortLabel
    canonical_text: SafeAuthoringText
    kind: ClaimType
    source_label: SafeShortLabel
    normalized_value: NormalizedValue = None
    allowed_surface_forms: list[SafeAuthoringText] = Field(default_factory=list, max_length=12)

    @field_validator("label", "canonical_text", "source_label")
    @classmethod
    def contains_no_contact_pii(cls, value: str) -> str:
        return _reject_contact_pii(value)

    @field_validator("source_label")
    @classmethod
    def source_is_a_safe_label(cls, value: str) -> str:
        if _URL.search(value) or "/" in value or "\\" in value:
            raise ValueError("source_label must be a safe human-readable label, not a URL or path")
        return value

    @field_validator("allowed_surface_forms")
    @classmethod
    def forms_are_safe_and_unique(cls, values: list[str]) -> list[str]:
        cleaned = [_reject_contact_pii(value) for value in values]
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("allowed_surface_forms must be unique")
        return cleaned

    @model_validator(mode="after")
    def exact_value_matches_kind(self) -> CustomFactInput:
        value = self.normalized_value
        if self.kind is ClaimType.URL:
            raise ValueError("URL facts are server-derived from the product CTA")
        if self.kind is ClaimType.NUMBER:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError("number facts require a numeric normalized_value")
        elif self.kind is ClaimType.PERCENTAGE:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError("percentage facts require a numeric normalized_value")
        elif self.kind in {ClaimType.MONEY, ClaimType.DURATION}:
            if not isinstance(value, dict) or set(value) != {"value", "unit"}:
                raise ValueError("money and duration facts require {value, unit}")
            measure = value.get("value")
            if isinstance(measure, bool) or not isinstance(measure, int | float):
                raise ValueError("measure value must be numeric")
        elif self.kind is ClaimType.DATE:
            if not isinstance(value, str):
                raise ValueError("date facts require an ISO date string")
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("date normalized_value must use YYYY-MM-DD") from exc
        elif value is not None and not isinstance(value, str | int | float | bool):
            raise ValueError("textual facts require a scalar normalized_value or null")
        return self


class CustomProductCreateRequest(StrictModel):
    exact_name: SafeShortLabel
    cta_label: SafeShortLabel
    cta_url: Annotated[
        str,
        Field(
            pattern=r"^https://(?:[A-Za-z0-9-]+\.)+(?:test|invalid)(?:/[^\s]*)?$",
            max_length=500,
        ),
    ]
    facts: list[CustomFactInput] = Field(min_length=1, max_length=12)
    synthetic_confirmed: Literal[True]
    no_pii_confirmed: Literal[True]

    @field_validator("exact_name", "cta_label")
    @classmethod
    def contains_no_contact_pii(cls, value: str) -> str:
        return _reject_contact_pii(value)

    @model_validator(mode="after")
    def fact_rows_are_distinct(self) -> CustomProductCreateRequest:
        labels = [item.label.casefold() for item in self.facts]
        formulations = [item.canonical_text.casefold() for item in self.facts]
        if len(labels) != len(set(labels)):
            raise ValueError("fact labels must be unique within a product version")
        if len(formulations) != len(set(formulations)):
            raise ValueError("canonical fact formulations must be unique")
        return self


class AuthoringFactView(FrozenStrictModel):
    fact_id: Identifier
    source_id: Identifier
    label: SafeShortLabel
    canonical_text: SafeAuthoringText
    kind: ClaimType
    source_label: SafeShortLabel
    normalized_value: NormalizedValue = None


class AuthoringProductView(FrozenStrictModel):
    product_id: Identifier
    version: int = Field(ge=1)
    exact_name: SafeShortLabel
    cta_label: SafeShortLabel
    cta_url: str
    facts: tuple[AuthoringFactView, ...]
    origin: Literal["catalog", "custom"]
    synthetic: Literal[True] = True


class CustomProductRecord(FrozenStrictModel):
    product: SyntheticProduct
    cta_label: SafeShortLabel
    fact_labels: dict[Identifier, SafeShortLabel]
    source_labels: dict[Identifier, SafeShortLabel]
    request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class AuthoringPersonaView(FrozenStrictModel):
    segment_id: Identifier
    trigger_id: Identifier
    label: SafeShortLabel
    tone_hint: SafeShortLabel
    connected_product_ids: tuple[Identifier, ...]
    available_channels: tuple[Literal["sms", "email"], ...]
    synthetic: Literal[True] = True


class EditorialReferencePrefill(FrozenStrictModel):
    reference_id: Identifier
    title: SafeShortLabel
    description: SafeAuthoringText
    label: Literal["EDITORIAL_REFERENCE_NOT_LIVE_NOT_RELEASE_EVIDENCE"]
    brief: CampaignBriefInput
    custom_product: dict[str, Any] | None = None


class AuthoringCatalogView(FrozenStrictModel):
    products: tuple[AuthoringProductView, ...]
    personas: tuple[AuthoringPersonaView, ...]
    references: tuple[EditorialReferencePrefill, ...]
    synthetic: Literal[True] = True
    no_send: Literal[True] = True


class RecentCampaignView(FrozenStrictModel):
    campaign_id: Identifier
    name: str | None
    product_name: str | None
    channels: tuple[Literal["sms", "email"], ...]
    state: str
    updated_at: datetime
    synthetic: Literal[True] = True
