from __future__ import annotations

from collections.abc import Iterable

from apps.api.app.domain.campaigns import ContextBundle, FactLedgerItem
from apps.api.app.domain.models import (
    Channel,
    ChannelSuppression,
    ChannelSuppressionReason,
    ClaimEvidence,
    ClaimType,
    CommunicationBundle,
    EmailArtifact,
    EmailSection,
    SmsArtifact,
)


def _active_rule_values(
    context: ContextBundle,
    *,
    rule_type: str,
    channel: Channel | None = None,
) -> list[str]:
    applied = set(context.content_plan.applied_rule_version_ids)
    values: list[str] = []
    for rule in context.active_rules:
        if str(rule.get("rule_version_id") or "") not in applied or rule.get("type") != rule_type:
            continue
        scope = rule.get("scope")
        scope = scope if isinstance(scope, dict) else {}
        scoped_channel = str(scope.get("channel") or "")
        if channel is not None and scoped_channel and scoped_channel != channel.value:
            continue
        value = str(rule.get("value") or "").strip()
        if value and value not in values:
            values.append(value)
    return values


def _fragment(fact: FactLedgerItem) -> str:
    if fact.kind is ClaimType.URL:
        return str(fact.normalized_value)
    for surface in fact.allowed_surface_forms:
        if surface in fact.canonical_text:
            return surface
    return fact.canonical_text


def _compact_fragment(fact: FactLedgerItem) -> str:
    if fact.kind is ClaimType.URL:
        return str(fact.normalized_value)
    candidates = tuple(dict.fromkeys((fact.canonical_text, *fact.allowed_surface_forms)))
    return min(candidates, key=lambda value: (len(value), value))


def _evidence(
    *,
    channel: Channel,
    path: str,
    fact: FactLedgerItem,
    ordinal: int,
    text_fragment: str | None = None,
) -> ClaimEvidence:
    return ClaimEvidence(
        claim_id=f"claim_{channel.value}_{ordinal:03d}",
        channel=channel,
        artifact_path=path,
        text_fragment=text_fragment or _fragment(fact),
        claim_type=fact.kind,
        normalized_value=fact.normalized_value,
        fact_id=fact.fact_id,
        source_id=fact.source_id,
    )


def _selected_facts(
    context: ContextBundle,
    channel: Channel | None = None,
) -> list[FactLedgerItem]:
    by_id = {fact.fact_id: fact for fact in context.facts}
    fact_ids = (
        context.content_plan.fact_ids_for(channel)
        if channel is not None
        else context.content_plan.selected_fact_ids
    )
    return [by_id[fact_id] for fact_id in fact_ids]


def _selected_concept_sentences(context: ContextBundle, channel: Channel) -> list[str]:
    concepts = {item.concept_id: item for item in context.concepts}
    sentences: list[str] = []
    for concept_id in context.content_plan.selected_concept_ids:
        concept = concepts.get(concept_id)
        if concept is None:
            continue
        scoped_channels = {
            str((rule.get("scope") or {}).get("channel") or "")
            for rule in context.active_rules
            if rule.get("type") == "require_concept_id" and rule.get("value") == concept_id
        }
        if scoped_channels and channel.value not in scoped_channels and "" not in scoped_channels:
            continue
        sentences.append(f"Доступно {concept.accepted_surface_forms[0]}.")
    return sentences


def _sms(
    context: ContextBundle,
    facts: list[FactLedgerItem],
) -> tuple[SmsArtifact, list[ClaimEvidence]]:
    brief = context.brief_snapshot
    non_url = [fact for fact in facts if fact.kind is not ClaimType.URL]
    compact_facts = [(fact, _compact_fragment(fact)) for fact in non_url]
    parts = [f"{context.product.exact_name}."]
    parts.extend(surface for _, surface in compact_facts)
    parts.extend(_selected_concept_sentences(context, Channel.SMS))
    parts.extend(_active_rule_values(context, rule_type="require_phrase", channel=Channel.SMS))
    parts.append(f"{brief.cta_label}: {brief.cta_url}")
    text = " ".join(parts)
    artifact = SmsArtifact(
        text=text,
        cta_url=brief.cta_url,
        fact_refs=[fact.fact_id for fact in facts],
        personalization_refs=[],
    )
    evidence: list[ClaimEvidence] = []
    ordinal = 1
    for fact, surface in compact_facts:
        evidence.append(
            _evidence(
                channel=Channel.SMS,
                path="/sms/text",
                fact=fact,
                ordinal=ordinal,
                text_fragment=surface,
            )
        )
        ordinal += 1
    url_facts = [fact for fact in facts if fact.kind is ClaimType.URL]
    for fact in url_facts:
        evidence.extend(
            [
                _evidence(
                    channel=Channel.SMS,
                    path="/sms/text",
                    fact=fact,
                    ordinal=ordinal,
                ),
                _evidence(
                    channel=Channel.SMS,
                    path="/sms/cta_url",
                    fact=fact,
                    ordinal=ordinal + 1,
                ),
            ]
        )
        ordinal += 2
    return artifact, evidence


def _email(
    context: ContextBundle,
    facts: list[FactLedgerItem],
) -> tuple[EmailArtifact, list[ClaimEvidence]]:
    brief = context.brief_snapshot
    disclaimer_ids = set(context.product.required_disclaimer_ids)
    non_url = [fact for fact in facts if fact.kind is not ClaimType.URL]
    url_facts = [fact for fact in facts if fact.kind is ClaimType.URL]
    concept_sentences = _selected_concept_sentences(context, Channel.EMAIL)
    required_phrases = _active_rule_values(
        context,
        rule_type="require_phrase",
        channel=Channel.EMAIL,
    )
    sections: list[EmailSection] = []
    fact_paths: list[tuple[FactLedgerItem, str]] = []
    section_count = min(4, max(2, len(non_url)))
    grouped_facts: list[list[FactLedgerItem]] = [[] for _ in range(section_count)]
    for index, fact in enumerate(non_url):
        grouped_facts[min(index, section_count - 1)].append(fact)
    headings = ("Контекст", "Возможности", "Условия", "Следующий шаг")
    for index, group in enumerate(grouped_facts):
        is_disclaimer = any(fact.fact_id in disclaimer_ids for fact in group)
        body_parts = [fact.canonical_text for fact in group]
        if index == 0:
            body_parts.extend(concept_sentences)
            body_parts.extend(required_phrases)
        if not body_parts:
            body_parts.append("Сценарий подготовлен по выбранному синтетическому брифу.")
        sections.append(
            EmailSection(
                section_id=f"section_{index + 1}",
                kind="disclaimer" if is_disclaimer else "body",
                heading="Важно" if is_disclaimer else headings[index],
                body=" ".join(body_parts),
                fact_refs=[fact.fact_id for fact in group],
                personalization_refs=[],
            )
        )
        fact_paths.extend((fact, f"/email/sections/{index}/body") for fact in group)
    plain_parts = [context.product.exact_name]
    plain_parts.extend(fact.canonical_text for fact in non_url)
    plain_parts.extend(concept_sentences)
    plain_parts.extend(required_phrases)
    plain_parts.append(f"{brief.cta_label}: {brief.cta_url}")
    artifact = EmailArtifact(
        subject=f"{context.product.exact_name}: синтетическое предложение",
        preheader="Синтетическая коммуникация без отправки.",
        headline=context.product.exact_name,
        sections=sections,
        cta_label=brief.cta_label,
        cta_url=brief.cta_url,
        disclaimer_ids=sorted(disclaimer_ids),
        plain_text="\n\n".join(plain_parts),
        fact_refs=[fact.fact_id for fact in facts],
        personalization_refs=[],
    )
    evidence: list[ClaimEvidence] = []
    ordinal = 1
    for fact, path in fact_paths:
        evidence.extend(
            [
                _evidence(channel=Channel.EMAIL, path=path, fact=fact, ordinal=ordinal),
                _evidence(
                    channel=Channel.EMAIL,
                    path="/email/plain_text",
                    fact=fact,
                    ordinal=ordinal + 1,
                ),
            ]
        )
        ordinal += 2
    for fact in url_facts:
        evidence.extend(
            [
                _evidence(
                    channel=Channel.EMAIL,
                    path="/email/cta_url",
                    fact=fact,
                    ordinal=ordinal,
                ),
                _evidence(
                    channel=Channel.EMAIL,
                    path="/email/plain_text",
                    fact=fact,
                    ordinal=ordinal + 1,
                ),
            ]
        )
        ordinal += 2
    return artifact, evidence


def _is_permitted(context: ContextBundle, channel: Channel) -> bool:
    return (
        not context.contact_policy.require_channel_consent
        or context.persona.channel_consent.get(channel, False)
    )


def _suppression(
    channel: Channel,
    reason_code: ChannelSuppressionReason,
    reason: str,
) -> ChannelSuppression:
    return ChannelSuppression(channel=channel, reason_code=reason_code, reason=reason)


def build_deterministic_bundle(context: ContextBundle) -> CommunicationBundle:
    requested = set(context.brief_snapshot.channels)
    suppressions: list[ChannelSuppression] = []
    evidence: list[ClaimEvidence] = []
    sms: SmsArtifact | None = None
    email: EmailArtifact | None = None

    for channel in (Channel.SMS, Channel.EMAIL):
        if channel not in requested:
            suppressions.append(
                _suppression(
                    channel,
                    ChannelSuppressionReason.CHANNEL_NOT_REQUESTED,
                    "Канал не выбран в готовом брифе.",
                )
            )
        elif not _is_permitted(context, channel):
            suppressions.append(
                _suppression(
                    channel,
                    ChannelSuppressionReason.CHANNEL_CONSENT_BLOCKED,
                    "Синтетический профиль не разрешает этот канал.",
                )
            )
        elif channel is Channel.SMS:
            sms, sms_evidence = _sms(context, _selected_facts(context, Channel.SMS))
            evidence.extend(sms_evidence)
        else:
            email, email_evidence = _email(context, _selected_facts(context, Channel.EMAIL))
            evidence.extend(email_evidence)

    rationale = ["Выбраны только безопасные агрегированные признаки синтетического сегмента."]
    rationale.extend(
        f"Учтён утверждённый tone hint: {value}."
        for value in _active_rule_values(context, rule_type="tone_hint")
    )
    return CommunicationBundle(
        summary="Детерминированный синтетический пакет для проверки доменного контура.",
        personalization_rationale=rationale,
        sms=sms,
        email=email,
        channel_suppressions=suppressions,
        claim_evidence=evidence,
        warnings=[],
    )


def iter_text_paths(bundle: CommunicationBundle) -> Iterable[tuple[str, str]]:
    if bundle.sms is not None:
        yield "/sms/text", bundle.sms.text
        yield "/sms/cta_url", bundle.sms.cta_url
    if bundle.email is not None:
        yield "/email/subject", bundle.email.subject
        yield "/email/preheader", bundle.email.preheader
        yield "/email/headline", bundle.email.headline
        yield "/email/cta_label", bundle.email.cta_label
        yield "/email/cta_url", bundle.email.cta_url
        yield "/email/plain_text", bundle.email.plain_text
        for index, section in enumerate(bundle.email.sections):
            yield f"/email/sections/{index}/heading", section.heading
            yield f"/email/sections/{index}/body", section.body
