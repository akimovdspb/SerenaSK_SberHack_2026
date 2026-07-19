# ruff: noqa: RUF001 -- Russian schema guidance intentionally names Latin JSON fields.
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator, with_config

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")]
SectionIdentifier = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"),
]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
JsonPointer = Annotated[str, Field(pattern=r"^(?:/(?:[^~/]|~[01])*)*$", max_length=512)]
NonEmptyText = Annotated[str, Field(min_length=1, max_length=4_000)]
SyntheticHttpsUrl = Annotated[
    str,
    Field(pattern=r"^https://(?:[A-Za-z0-9-]+\.)+(?:test|invalid)(?:/[^\s]*)?$"),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


@with_config(ConfigDict(extra="forbid", str_strip_whitespace=True))
class NormalizedMeasure(TypedDict):
    value: int | float
    unit: Annotated[str, Field(min_length=1, max_length=64)]


type NormalizedValue = str | int | float | bool | None | NormalizedMeasure


class Operation(StrEnum):
    INITIAL = "initial"
    REVISION = "revision"
    RULE_PROPOSAL = "rule_proposal"


class Channel(StrEnum):
    SMS = "sms"
    EMAIL = "email"


class ChannelSuppressionReason(StrEnum):
    CHANNEL_NOT_REQUESTED = "CHANNEL_NOT_REQUESTED"
    CHANNEL_CONSENT_BLOCKED = "CHANNEL_CONSENT_BLOCKED"


class ClaimType(StrEnum):
    TEXT = "text"
    NUMBER = "number"
    PERCENTAGE = "percentage"
    MONEY = "money"
    DATE = "date"
    DURATION = "duration"
    URL = "url"
    CONDITION = "condition"
    CONCEPT = "concept"


class ContextGetRequest(StrictModel):
    campaign_id: Identifier
    operation: Operation
    iteration: int = Field(ge=1, le=100)
    context_version: Sha256 | None = None
    idempotency_key: Annotated[str, Field(min_length=16, max_length=128)]


class ToolQuestion(StrictModel):
    question_id: Identifier
    path: JsonPointer
    message: NonEmptyText


class ContextToolResult(StrictModel):
    ready: bool
    status: Identifier
    campaign_id: Identifier
    operation: Operation
    iteration: int = Field(ge=1, le=100)
    context_version: Sha256 | None = None
    questions: list[ToolQuestion] = Field(default_factory=list, max_length=20)
    context_bundle: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None


class ClaimEvidence(StrictModel):
    claim_id: Identifier = Field(
        description=(
            "Уникальный ID одной пары факт + artifact_path, например "
            "claim_sms_duration_001. "
            "Не используй fact_id как claim_id и не повторяй claim_id на другом пути."
        )
    )
    channel: Channel = Field(
        description=(
            "Канал с этим точным фактическим вхождением: sms для artifact_path, "
            "начинающегося с /sms, и email для пути, начинающегося с /email."
        )
    )
    artifact_path: JsonPointer = Field(
        description=(
            "JSON Pointer внутри payload CommunicationBundle, начинающийся с /sms или /email; "
            "никогда не добавляй префикс /payload. Примеры: /sms/text, /email/cta_url, "
            "/email/sections/0/body, /email/plain_text. Индексы sections начинаются с нуля. "
            "Один и тот же URL в text и "
            "cta_url — это два отдельных пути и две evidence-записи."
        )
    )
    text_fragment: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1_000,
            description=(
                "Точная подстрока, скопированная из окончательного текста по artifact_path. "
                "Для одного факта фрагменты на разных путях могут различаться; не подставляй "
                "canonical_text, если его точной подстроки на этом пути нет. Не создавай "
                "evidence для exact_name продукта или подписи CTA, если они сами не являются "
                "выбранным FactLedgerItem."
            ),
        ),
    ]
    claim_type: ClaimType = Field(
        description=(
            "Точная копия kind указанного FactLedgerItem. Не используй text "
            "для exact_name продукта."
        )
    )
    normalized_value: NormalizedValue = Field(
        description=(
            "Обязательная точная JSON-копия normalized_value указанного "
            "FactLedgerItem, включая object или null. Не вычисляй и не пропускай."
        )
    )
    fact_id: Identifier = Field(description="Существующий выбранный fact_id из FactLedgerItem.")
    source_id: Identifier = Field(description="Точный source_id указанного факта.")


class SmsArtifact(StrictModel):
    text: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1_000,
            description=(
                "Самостоятельный текст SMS с точным названием продукта и точной подписью CTA. "
                "Для initial вырази каждый выбранный для SMS не-URL факт ровно один раз целой "
                "строкой из canonical_text или allowed_surface_forms. Выбранный URL выведи "
                "ровно один раз: точная brief_snapshot.cta_label, двоеточие с пробелом и точная "
                "brief_snapshot.cta_url. canonical_text URL-факта отдельно не выводи. Каждому "
                "фактическому вхождению нужен claim_evidence."
            ),
        ),
    ]
    cta_url: Annotated[
        SyntheticHttpsUrl,
        Field(
            description=(
                "Точный CTA URL из ready brief. Для initial это второе и последнее "
                "SMS-вхождение выбранного URL после /sms/text."
            )
        ),
    ]
    fact_refs: list[Identifier] = Field(
        max_length=50,
        description="Все выбранные fact ID, использованные в SMS, включая URL-факты.",
    )
    personalization_refs: list[Identifier] = Field(
        max_length=50,
        description="Разрешённые видимые ссылки персонализации; укажи [], если их нет.",
    )


class EmailSection(StrictModel):
    section_id: SectionIdentifier
    kind: Literal["intro", "body", "benefits", "cta", "disclaimer", "text"] = Field(
        description=(
            "Один из закрытых типов структурной e-mail секции. Для initial не используй "
            "cta: сервер строит кнопку из верхнеуровневых полей e-mail."
        )
    )
    heading: Annotated[
        str,
        Field(
            max_length=200,
            description=(
                "Краткий содержательный заголовок секции. Для initial не копируй сюда целую "
                "разрешённую формулировку выбранного факта, точное числовое значение или URL."
            ),
        ),
    ] = ""
    body: Annotated[
        str,
        Field(
            min_length=1,
            max_length=4_000,
            description=(
                "Текст секции. Для initial каждый закреплённый за секцией не-URL факт вырази "
                "ровно один раз целой строкой из canonical_text или allowed_surface_forms; "
                "добавляй только подтверждённый клиентский контекст и связующий текст. Для "
                "каждого факта обязательна claim_evidence; URL и подпись CTA в секции запрещены."
            ),
        ),
    ]
    fact_refs: list[Identifier] = Field(
        max_length=50,
        description="Выбранные fact ID, явно использованные в секции; укажи [], если их нет.",
    )
    personalization_refs: list[Identifier] = Field(
        max_length=50,
        description="Разрешённые видимые ссылки персонализации; укажи [], если их нет.",
    )


class EmailArtifact(StrictModel):
    subject: Annotated[
        str,
        Field(
            min_length=1,
            max_length=200,
            description=(
                "Естественная тема письма; точное название продукта должно быть в subject или "
                "headline. Для initial можно использовать явно переданный клиентский сценарий, "
                "но нельзя копировать целую разрешённую формулировку факта, число или URL."
            ),
        ),
    ]
    preheader: Annotated[
        str,
        Field(
            min_length=1,
            max_length=300,
            description=(
                "Краткий предварительный текст, дополняющий тему. Для initial можно использовать "
                "явно переданный клиентский сценарий, но нельзя копировать целую разрешённую "
                "формулировку факта, точное числовое значение или URL."
            ),
        ),
    ]
    headline: Annotated[
        str,
        Field(
            min_length=1,
            max_length=300,
            description=(
                "Содержательный заголовок письма; точное название продукта должно быть здесь или "
                "в subject. Для initial не копируй целую разрешённую формулировку факта, точное "
                "числовое значение или URL."
            ),
        ),
    ]
    sections: list[EmailSection] = Field(
        min_length=1,
        max_length=20,
        description=(
            "Для initial создай от двух до четырёх содержательных секций в точном количестве, "
            "заданном схемой. Одна секция может раскрывать несколько выбранных для e-mail "
            "не-URL фактов. Письмо должно быть самостоятельным каналом, а не расширенной копией "
            "SMS. Не создавай CTA-секцию: URL и подпись CTA принадлежат верхнеуровневым полям "
            "и plain_text."
        ),
    )
    cta_label: Annotated[
        str,
        Field(
            min_length=1,
            max_length=120,
            description="Точная подпись CTA из ReadyCampaignBrief.",
        ),
    ]
    cta_url: Annotated[
        SyntheticHttpsUrl,
        Field(
            description=(
                "Точный CTA URL из ready brief. Для initial это второе и последнее "
                "e-mail-вхождение выбранного URL после /email/plain_text."
            )
        ),
    ]
    disclaimer_ids: list[Identifier] = Field(
        max_length=20,
        description="Все обязательные disclaimer fact ID; укажи [], если политика их не требует.",
    )
    plain_text: Annotated[
        str,
        Field(
            min_length=1,
            max_length=10_000,
            description=(
                "Полная связная текстовая версия самостоятельного письма. Для initial вырази "
                "каждый выбранный для e-mail не-URL факт ровно один раз целой строкой из "
                "canonical_text или allowed_surface_forms и помести выбранный URL ровно один "
                "раз. URL выведи только после точной "
                "brief_snapshot.cta_label и двоеточия с пробелом; canonical_text URL-факта "
                "отдельно не выводи."
            ),
        ),
    ]
    fact_refs: list[Identifier] = Field(
        max_length=100,
        description="Все выбранные fact ID, использованные в e-mail, включая URL-факты.",
    )
    personalization_refs: list[Identifier] = Field(
        max_length=100,
        description="Разрешённые видимые ссылки персонализации; укажи [], если их нет.",
    )


class ChannelSuppression(StrictModel):
    channel: Channel
    reason_code: ChannelSuppressionReason = Field(
        description=(
            "Точный закрытый код: CHANNEL_NOT_REQUESTED для невыбранного канала или "
            "CHANNEL_CONSENT_BLOCKED для канала без согласия."
        )
    )
    reason: NonEmptyText


class CommunicationBundle(StrictModel):
    summary: Annotated[
        str,
        Field(
            min_length=1,
            max_length=2_000,
            description=(
                "Краткое описание назначения пакета. Для initial не копируй сюда значения "
                "выбранных фактов или URL; они принадлежат только путям артефактов."
            ),
        ),
    ]
    personalization_rationale: list[NonEmptyText] = Field(default_factory=list, max_length=20)
    sms: SmsArtifact | None = None
    email: EmailArtifact | None = None
    channel_suppressions: list[ChannelSuppression] = Field(
        max_length=2,
        description="Явное подавление каждого отсутствующего канала; укажи [], если оба есть.",
    )
    claim_evidence: list[ClaimEvidence] = Field(
        max_length=200,
        description=(
            "Одна запись на каждую пару выбранный каналом факт + обязательный artifact path. "
            "Для initial не-URL факт SMS имеет evidence в /sms/text, а не-URL факт e-mail — "
            "в body одной секции и /email/plain_text. URL имеет записи в text и cta_url "
            "соответствующего канала. На каждом обязательном пути нужна ровно одна запись. "
            "Не добавляй evidence для exact_name продукта или CTA label без "
            "соответствующего выбранного факта."
        ),
    )
    warnings: list[NonEmptyText] = Field(
        max_length=50,
        description="Видимые предупреждения о содержании; укажи [], если их нет.",
    )

    @model_validator(mode="after")
    def channels_are_explicit(self) -> CommunicationBundle:
        suppressed = {item.channel for item in self.channel_suppressions}
        if self.sms is None and Channel.SMS not in suppressed:
            raise ValueError("sms must be present or explicitly suppressed")
        if self.email is None and Channel.EMAIL not in suppressed:
            raise ValueError("email must be present or explicitly suppressed")
        if self.sms is not None and Channel.SMS in suppressed:
            raise ValueError("sms cannot be present and suppressed")
        if self.email is not None and Channel.EMAIL in suppressed:
            raise ValueError("email cannot be present and suppressed")
        return self


class CommunicationPatch(StrictModel):
    base_package_hash: Sha256
    feedback_id: Identifier
    changed_paths: list[JsonPointer] = Field(min_length=1, max_length=20)
    sms: SmsArtifact | None = None
    email: EmailArtifact | None = None
    claim_evidence: list[ClaimEvidence] = Field(default_factory=list, max_length=200)
    warnings: list[NonEmptyText] = Field(default_factory=list, max_length=50)


class RuleType(StrEnum):
    FORBID_PHRASE = "forbid_phrase"
    REQUIRE_PHRASE = "require_phrase"
    REQUIRE_FACT = "require_fact"
    REQUIRE_CONCEPT_ID = "require_concept_id"
    TONE_HINT = "tone_hint"


class RuleScope(StrictModel):
    product_ids: list[Identifier] = Field(default_factory=list, max_length=20)
    channel: Channel | None = None
    segment_ids: list[Identifier] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def is_bounded(self) -> RuleScope:
        if not self.product_ids and self.channel is None and not self.segment_ids:
            raise ValueError("global rule scope is not allowed")
        return self


class RuleProposal(StrictModel):
    proposal_id: Identifier
    source_feedback_id: Identifier
    type: RuleType
    scope: RuleScope
    condition_id: Identifier | None = None
    value: Annotated[str, Field(min_length=1, max_length=500)]
    rationale: Annotated[str, Field(min_length=1, max_length=2_000)]
    target_case_ids: list[Identifier] = Field(min_length=1, max_length=50)
    base_rules_version: Sha256
    candidate_rules_version: Sha256
    risk: Literal["low", "medium"]


class RuleProposalDraft(StrictModel):
    type: RuleType
    condition_id: Identifier | None = None
    value: Annotated[str, Field(min_length=1, max_length=500)]
    rationale: Annotated[str, Field(min_length=1, max_length=2_000)]
    risk: Literal["low", "medium"]


class EnvelopeBase(StrictModel):
    schema_version: Literal["1.0"]
    campaign_id: Identifier
    iteration: int = Field(ge=1, le=100)
    context_version: Sha256


class CommunicationBundleEnvelope(EnvelopeBase):
    kind: Literal["communication_bundle"]
    operation: Literal[Operation.INITIAL]
    payload: CommunicationBundle


class CommunicationPatchEnvelope(EnvelopeBase):
    kind: Literal["communication_patch"]
    operation: Literal[Operation.REVISION]
    payload: CommunicationPatch


class RuleProposalEnvelope(EnvelopeBase):
    kind: Literal["rule_proposal"]
    operation: Literal[Operation.RULE_PROPOSAL]
    payload: RuleProposalDraft


DraftEnvelope = Annotated[
    CommunicationBundleEnvelope | CommunicationPatchEnvelope | RuleProposalEnvelope,
    Field(discriminator="kind"),
]


class DraftSaveRequest(StrictModel):
    campaign_id: Identifier
    operation: Operation
    iteration: int = Field(ge=1, le=100)
    context_version: Sha256
    idempotency_key: Annotated[str, Field(min_length=16, max_length=128)]
    draft: DraftEnvelope

    @model_validator(mode="after")
    def envelope_matches_request(self) -> DraftSaveRequest:
        if self.draft.campaign_id != self.campaign_id:
            raise ValueError("draft campaign_id does not match request")
        if self.draft.operation != self.operation:
            raise ValueError("draft operation does not match request")
        if self.draft.iteration != self.iteration:
            raise ValueError("draft iteration does not match request")
        if self.draft.context_version != self.context_version:
            raise ValueError("draft context_version does not match request")
        return self


class DraftSaveResult(StrictModel):
    status: Identifier
    persisted: bool
    idempotent_replay: bool = False
    campaign_id: Identifier
    operation: Operation
    iteration: int = Field(ge=1, le=100)
    draft_id: Identifier | None = None
    draft_hash: Sha256 | None = None
    blockers: list[Identifier] = Field(default_factory=list, max_length=50)
    warnings: list[NonEmptyText] = Field(default_factory=list, max_length=50)
    saved_at: datetime | None = None
