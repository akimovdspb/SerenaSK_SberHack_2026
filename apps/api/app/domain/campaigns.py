from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from apps.api.app.domain.models import (
    Channel,
    ClaimType,
    Identifier,
    JsonPointer,
    NonEmptyText,
    NormalizedValue,
    Operation,
    Sha256,
    StrictModel,
    SyntheticHttpsUrl,
)


class FrozenStrictModel(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


class OfferPeriod(FrozenStrictModel):
    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def chronological(self) -> OfferPeriod:
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("offer period end cannot precede start")
        return self


class CampaignBriefInput(StrictModel):
    name: str | None = Field(default=None, max_length=200)
    objective: str | None = Field(default=None, max_length=1_000)
    product_id: Identifier | None = None
    segment_id: Identifier | None = None
    trigger_id: Identifier | None = None
    channels: list[Channel] = Field(default_factory=list, max_length=2)
    cta_label: str | None = Field(default=None, max_length=120)
    cta_url: str | None = Field(default=None, max_length=500)
    tone: str | None = Field(default=None, max_length=200)
    offer_period: OfferPeriod | None = None
    notes: str | None = Field(default=None, max_length=4_000)
    synthetic: Literal[True] = True

    @field_validator("channels")
    @classmethod
    def channels_are_unique(cls, value: list[Channel]) -> list[Channel]:
        if len(value) != len(set(value)):
            raise ValueError("channels must be unique")
        return value


class CampaignBriefDraft(CampaignBriefInput):
    campaign_id: Identifier
    version: int = Field(ge=1)
    input_hash: Sha256


class ReadyCampaignBrief(FrozenStrictModel):
    campaign_id: Identifier
    name: NonEmptyText
    objective: NonEmptyText
    product_id: Identifier
    segment_id: Identifier
    trigger_id: Identifier
    channels: tuple[Channel, ...] = Field(min_length=1, max_length=2)
    cta_label: Annotated[str, Field(min_length=1, max_length=120)]
    cta_url: SyntheticHttpsUrl
    tone: Annotated[str, Field(min_length=1, max_length=200)]
    mandatory_fact_ids: tuple[Identifier, ...]
    mandatory_concept_ids: tuple[Identifier, ...]
    prohibited_claim_ids: tuple[Identifier, ...]
    legal_policy_id: Identifier
    contact_policy_id: Identifier
    offer_period: OfferPeriod
    notes: str = Field(max_length=4_000)
    synthetic: Literal[True] = True
    version: int = Field(ge=1)
    input_hash: Sha256

    @field_validator("channels", "mandatory_fact_ids", "mandatory_concept_ids")
    @classmethod
    def identifiers_are_unique(cls, value: tuple[Any, ...]) -> tuple[Any, ...]:
        if len(value) != len(set(value)):
            raise ValueError("ready brief lists must contain unique values")
        return value


class BriefStatus(StrEnum):
    NEEDS_INPUT = "NEEDS_INPUT"
    READY = "READY"
    BLOCKED = "BLOCKED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class BriefQuestion(StrictModel):
    question_id: Identifier
    path: JsonPointer
    reason: NonEmptyText
    message: NonEmptyText
    options: list[str] = Field(default_factory=list, max_length=20)


class BriefValidationResult(StrictModel):
    status: BriefStatus
    campaign_id: Identifier
    draft_version: int = Field(ge=1)
    input_hash: Sha256
    questions: list[BriefQuestion] = Field(default_factory=list, max_length=5)
    blockers: list[Identifier] = Field(default_factory=list, max_length=20)
    ready_brief: ReadyCampaignBrief | None = None
    llm_calls: Literal[0] = 0

    @model_validator(mode="after")
    def ready_payload_matches_status(self) -> BriefValidationResult:
        if self.status is BriefStatus.READY and self.ready_brief is None:
            raise ValueError("READY requires an immutable ready brief")
        if self.status is not BriefStatus.READY and self.ready_brief is not None:
            raise ValueError("non-ready status cannot carry a ready brief")
        return self


class FactLedgerItem(FrozenStrictModel):
    fact_id: Identifier
    source_id: Identifier
    kind: ClaimType
    canonical_text: NonEmptyText
    normalized_value: NormalizedValue = None
    allowed_surface_forms: tuple[NonEmptyText, ...] = Field(min_length=1, max_length=20)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    synthetic: Literal[True] = True

    @model_validator(mode="after")
    def validity_is_chronological(self) -> FactLedgerItem:
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("fact valid_to cannot precede valid_from")
        return self


class ProductFactCard(FrozenStrictModel):
    product_id: Identifier
    version: int = Field(ge=1)
    exact_name: NonEmptyText
    allowed_fact_ids: tuple[Identifier, ...]
    mandatory_fact_ids: tuple[Identifier, ...]
    mandatory_concept_ids: tuple[Identifier, ...]
    optional_concept_ids: tuple[Identifier, ...]
    required_disclaimer_ids: tuple[Identifier, ...]
    prohibited_claim_ids: tuple[Identifier, ...]
    allowed_cta_urls: tuple[SyntheticHttpsUrl, ...] = Field(min_length=1)
    legal_policy_id: Identifier
    contact_policy_id: Identifier
    channel_required_fact_ids: dict[Channel, tuple[Identifier, ...]] = Field(default_factory=dict)
    validity_start: date
    validity_end: date
    synthetic: Literal[True] = True

    @model_validator(mode="after")
    def fact_references_are_bounded(self) -> ProductFactCard:
        if not set(self.mandatory_fact_ids).issubset(self.allowed_fact_ids):
            raise ValueError("mandatory facts must be in the allowed fact set")
        if not set(self.required_disclaimer_ids).issubset(self.mandatory_fact_ids):
            raise ValueError("required disclaimers must be mandatory facts")
        for values in (
            self.allowed_fact_ids,
            self.mandatory_fact_ids,
            self.mandatory_concept_ids,
            self.optional_concept_ids,
            self.required_disclaimer_ids,
            self.prohibited_claim_ids,
            self.allowed_cta_urls,
        ):
            if len(values) != len(set(values)):
                raise ValueError("fact-card lists must contain unique values")
        if set(self.mandatory_concept_ids) & set(self.optional_concept_ids):
            raise ValueError("mandatory and optional concepts must not overlap")
        for channel, fact_ids in self.channel_required_fact_ids.items():
            if not set(fact_ids).issubset(self.mandatory_fact_ids):
                raise ValueError(f"{channel.value} required facts must be mandatory facts")
            if len(fact_ids) != len(set(fact_ids)):
                raise ValueError(f"{channel.value} required facts must be unique")
        return self


class PersonaContext(FrozenStrictModel):
    persona_id: Identifier
    segment_id: Identifier
    business_stage: NonEmptyText
    company_size_band: NonEmptyText
    region_category: NonEmptyText
    connected_product_ids: tuple[Identifier, ...]
    trigger_id: Identifier
    touch_history_id: Identifier
    channel_consent: dict[Channel, bool]
    frequency_cap_reached: bool = False
    tone_preference: NonEmptyText
    signals: tuple[NonEmptyText, ...] = Field(default_factory=tuple, max_length=20)
    synthetic: Literal[True] = True


class ContactPolicy(FrozenStrictModel):
    policy_id: Identifier
    version: int = Field(ge=1)
    max_touches_per_window: int = Field(ge=0, le=100)
    window_days: int = Field(ge=1, le=365)
    require_channel_consent: bool = True
    synthetic: Literal[True] = True


class LegalPolicy(FrozenStrictModel):
    policy_id: Identifier
    version: int = Field(ge=1)
    required_disclaimer_ids: tuple[Identifier, ...]
    prohibited_claim_ids: tuple[Identifier, ...]
    synthetic: Literal[True] = True


class ConceptDefinition(FrozenStrictModel):
    concept_id: Identifier
    accepted_surface_forms: tuple[NonEmptyText, ...] = Field(min_length=1, max_length=20)
    synthetic: Literal[True] = True


class SyntheticProduct(FrozenStrictModel):
    fact_card: ProductFactCard
    facts: tuple[FactLedgerItem, ...]


class CaseExpectation(FrozenStrictModel):
    status: BriefStatus
    selected_fact_ids: tuple[Identifier, ...] = Field(default_factory=tuple)
    required_disclaimer_ids: tuple[Identifier, ...] = Field(default_factory=tuple)
    blocker_codes: tuple[Identifier, ...] = Field(default_factory=tuple)


class SyntheticCase(FrozenStrictModel):
    case_id: Identifier
    title: NonEmptyText
    campaign_id: Identifier
    brief: CampaignBriefInput
    expected: CaseExpectation
    synthetic: Literal[True] = True


class ContentPlan(FrozenStrictModel):
    selected_fact_ids: tuple[Identifier, ...]
    channel_selected_fact_ids: dict[Channel, tuple[Identifier, ...]] = Field(default_factory=dict)
    selected_concept_ids: tuple[Identifier, ...]
    available_optional_fact_ids: tuple[Identifier, ...]
    available_optional_concept_ids: tuple[Identifier, ...]
    selection_sources: tuple[Literal["base_policy", "feedback", "rule"], ...]
    applied_rule_version_ids: tuple[Identifier, ...]

    @model_validator(mode="after")
    def channel_selection_is_bounded(self) -> ContentPlan:
        selected = set(self.selected_fact_ids)
        for channel, fact_ids in self.channel_selected_fact_ids.items():
            if not set(fact_ids).issubset(selected):
                raise ValueError(f"{channel.value} selection must be in selected_fact_ids")
            if len(fact_ids) != len(set(fact_ids)):
                raise ValueError(f"{channel.value} selection must be unique")
        return self

    def fact_ids_for(self, channel: Channel) -> tuple[Identifier, ...]:
        return self.channel_selected_fact_ids.get(channel, self.selected_fact_ids)


class SourceManifestItem(FrozenStrictModel):
    source_id: Identifier
    version: str
    retrieved_at: datetime
    synthetic: Literal[True] = True


class ContextBundle(FrozenStrictModel):
    classification: Literal["untrusted_data"] = "untrusted_data"
    context_version: Sha256
    operation: Operation
    brief_snapshot: ReadyCampaignBrief
    product: ProductFactCard
    facts: tuple[FactLedgerItem, ...]
    concepts: tuple[ConceptDefinition, ...]
    persona: PersonaContext
    touch_history: dict[str, Any]
    contact_policy: ContactPolicy
    channel_policies: dict[str, Any]
    legal_policy: LegalPolicy
    active_rules: tuple[dict[str, Any], ...]
    source_manifest: tuple[SourceManifestItem, ...]
    prompt_version: str
    rules_version: Sha256
    content_plan: ContentPlan
    previous_package: dict[str, Any] | None = None
    feedback: dict[str, Any] | None = None
    allowed_changed_paths: tuple[JsonPointer, ...] = Field(default_factory=tuple)
    protected_paths: tuple[JsonPointer, ...] = Field(default_factory=tuple)
    protected_hashes: dict[str, Sha256] = Field(default_factory=dict)
    output_schema_id: Identifier
