from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from apps.api.app.domain.campaigns import ContextBundle, FactLedgerItem
from apps.api.app.domain.models import Channel, ClaimType, CommunicationBundle
from apps.api.app.domain.quality import (
    Finding,
    FindingArtifact,
    FindingSeverity,
    QualityReport,
)
from apps.api.app.services.briefs import hash_value
from apps.api.app.services.deterministic import iter_text_paths
from apps.api.app.services.rendering import render_email_html, sms_metrics

QA_CHECKS: tuple[tuple[str, str], ...] = (
    ("QA01", "brief_required_fields"),
    ("QA02", "brief_fact_conflicts"),
    ("QA03", "eligibility_product_active"),
    ("QA04", "contact_channel_frequency"),
    ("QA05", "exact_product_name"),
    ("QA06", "numeric_date_duration_allowlist"),
    ("QA07", "allowed_https_url"),
    ("QA08", "required_disclaimer"),
    ("QA09", "forbidden_claims"),
    ("QA10", "mandatory_cta"),
    ("QA11", "required_fact_concept"),
    ("QA12", "sms_encoding_segments"),
    ("QA13", "email_structure_lengths"),
    ("QA14", "html_sanitation"),
    ("QA15", "placeholder_control_label"),
    ("QA16", "pii_internal_secret"),
    ("QA17", "declared_claim_consistency"),
    ("QA18", "actual_claim_fact_match"),
    ("QA19", "personalization_refs"),
    ("QA20", "revision_scope"),
    ("QA21", "active_rule_scope"),
    ("QA22", "approval_export_integrity"),
)
QA_CHECK_IDS = tuple(check_id for check_id, _ in QA_CHECKS)
REGISTRY_HASH = hash_value(QA_CHECKS)

URL_RE = re.compile(r"https://[^\s<>\"']+")
DURATION_RE = re.compile(
    r"(?<![\w])(?P<value>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>день|дня|дней|дн\.|час|часа|часов|месяц|месяца|месяцев)(?![\w])",
    re.IGNORECASE,
)
PERCENT_RE = re.compile(r"(?<![\w])(?P<value>\d+(?:[.,]\d+)?)\s*%")
MONEY_RE = re.compile(
    r"(?<![\w])(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>₽|руб(?:\.|лей)?)(?![\w])",  # noqa: RUF001
    re.IGNORECASE,
)
DATE_RE = re.compile(r"(?<!\d)(?P<value>\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4})(?!\d)")
NUMBER_RE = re.compile(r"(?<![\w])(?P<value>\d+(?:[.,]\d+)?)(?![\w])")
PLACEHOLDER_RE = re.compile(
    r"(?:\{\{|\}\}|<placeholder>|\bTODO\b|FINAL ANSWER|content_plan|context_version)",
    re.IGNORECASE,
)
PII_SECRET_RE = re.compile(
    r"(?:\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|(?:\+7|8)[\s()-]*\d{3}[\s()-]*\d{3}|"
    r"\bsk-[A-Za-z0-9_-]{8,}\b|\bBearer\s+[A-Za-z0-9._-]+|https?://[^/\s]+\.local\b)",
    re.IGNORECASE,
)
FORBIDDEN_PATTERNS: dict[str, re.Pattern[str]] = {
    "claim_guaranteed_result": re.compile(
        r"\bгарант(?:ия|ирован|ируем)",  # noqa: RUF001
        re.IGNORECASE,
    ),
    "claim_best_market": re.compile(r"\bлучший\s+на\s+рынке\b", re.IGNORECASE),  # noqa: RUF001
    "claim_instant_activation": re.compile(r"\bмгновенн", re.IGNORECASE),  # noqa: RUF001
    "claim_instant_alert": re.compile(r"\bмгновенн", re.IGNORECASE),  # noqa: RUF001
    "claim_zero_errors": re.compile(r"\bбез\s+ошибок\b", re.IGNORECASE),  # noqa: RUF001
}


@dataclass(frozen=True)
class ActualClaim:
    path: str
    fragment: str
    kind: ClaimType
    normalized_value: Any


@dataclass(frozen=True)
class InitialFactPlacementIssue:
    fact_id: str
    source_id: str
    path: str | None
    quote: str | None
    expected: str
    actual: str


def _numeric(value: str) -> int | float:
    normalized = value.replace(",", ".")
    parsed = float(normalized)
    return int(parsed) if parsed.is_integer() else parsed


def _overlaps(span: tuple[int, int], occupied: list[tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in occupied)


def extract_actual_claims(path: str, text: str, *, product_name: str) -> list[ActualClaim]:
    claims: list[ActualClaim] = []
    occupied = [match.span() for match in re.finditer(re.escape(product_name), text)]
    patterns: tuple[tuple[re.Pattern[str], ClaimType], ...] = (
        (URL_RE, ClaimType.URL),
        (DURATION_RE, ClaimType.DURATION),
        (PERCENT_RE, ClaimType.PERCENTAGE),
        (MONEY_RE, ClaimType.MONEY),
        (DATE_RE, ClaimType.DATE),
    )
    for pattern, kind in patterns:
        for match in pattern.finditer(text):
            if _overlaps(match.span(), occupied):
                continue
            fragment = match.group(0).rstrip(".,;:!?") if kind is ClaimType.URL else match.group(0)
            if kind is ClaimType.URL:
                normalized: Any = fragment
            elif kind is ClaimType.DURATION:
                unit = match.group("unit").lower()
                normalized = {
                    "value": _numeric(match.group("value")),
                    "unit": "day"
                    if unit.startswith("д")
                    else "hour"
                    if unit.startswith("час")
                    else "month",
                }
            elif kind is ClaimType.PERCENTAGE:
                normalized = _numeric(match.group("value"))
            elif kind is ClaimType.MONEY:
                normalized = {"value": _numeric(match.group("value")), "unit": "RUB"}
            else:
                normalized = match.group("value")
            claims.append(ActualClaim(path, fragment, kind, normalized))
            occupied.append(match.span())
    for match in NUMBER_RE.finditer(text):
        if _overlaps(match.span(), occupied):
            continue
        claims.append(
            ActualClaim(path, match.group(0), ClaimType.NUMBER, _numeric(match.group("value")))
        )
    return claims


def _resolve_pointer(bundle: CommunicationBundle, pointer: str) -> Any:
    current: Any = bundle.model_dump(mode="json")
    for raw_part in pointer.split("/")[1:]:
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(pointer)
    return current


def _artifact_for_path(path: str) -> FindingArtifact:
    if path.startswith("/sms"):
        return FindingArtifact.SMS
    if path.startswith("/email"):
        return FindingArtifact.EMAIL
    return FindingArtifact.PACKAGE


def _finding(
    findings: list[Finding],
    *,
    check_id: str,
    artifact: FindingArtifact,
    recommendation: str,
    path: str | None = None,
    quote: str | None = None,
    expected: str | None = None,
    actual: str | None = None,
    source_ids: tuple[str, ...] = (),
    severity: FindingSeverity = FindingSeverity.BLOCKER,
) -> None:
    ordinal = 1 + sum(item.check_id == check_id for item in findings)
    findings.append(
        Finding(
            finding_id=f"finding_{check_id.lower()}_{ordinal:03d}",
            check_id=check_id,
            severity=severity,
            artifact=artifact,
            path=path,
            quote=quote,
            expected=expected,
            actual=actual,
            source_ids=source_ids,
            recommendation=recommendation,
            blocking=severity is FindingSeverity.BLOCKER,
        )
    )


def _all_text(bundle: CommunicationBundle) -> str:
    return "\n".join(value for _, value in iter_text_paths(bundle))


def _fact_matches(claim: ActualClaim, fact: FactLedgerItem) -> bool:
    if claim.kind is not fact.kind:
        return False
    return claim.normalized_value == fact.normalized_value or any(
        claim.fragment in surface for surface in fact.allowed_surface_forms
    )


def _fact_surface_occurrences(
    text: str,
    fact: FactLedgerItem,
    *,
    product_name: str,
) -> tuple[tuple[int, int, str], ...]:
    occupied = [match.span() for match in re.finditer(re.escape(product_name), text)]
    matches: list[tuple[int, int, str]] = []
    surfaces = sorted(
        {fact.canonical_text, *fact.allowed_surface_forms},
        key=lambda value: (-len(value), value),
    )
    flags = 0 if fact.kind is ClaimType.URL else re.IGNORECASE
    for surface in surfaces:
        for match in re.finditer(re.escape(surface), text, flags=flags):
            if _overlaps(match.span(), occupied):
                continue
            start, end = match.span()
            matches.append((start, end, match.group(0)))
            occupied.append((start, end))
    return tuple(sorted(matches))


def _placement_counts_text(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{path} x{count}" for path, count in sorted(counts.items()))


def initial_fact_placement_issues(
    bundle: CommunicationBundle,
    context: ContextBundle,
) -> tuple[InitialFactPlacementIssue, ...]:
    """Validate the model-visible initial placement table without rewriting content."""
    if context.operation.value != "initial":
        return ()

    selected_ids = set(context.content_plan.selected_fact_ids)
    sms_selected_ids = set(context.content_plan.fact_ids_for(Channel.SMS))
    email_selected_ids = set(context.content_plan.fact_ids_for(Channel.EMAIL))
    selected_facts = [fact for fact in context.facts if fact.fact_id in selected_ids]
    text_paths = [*iter_text_paths(bundle), ("/summary", bundle.summary)]
    issues: list[InitialFactPlacementIssue] = []
    body_pattern = re.compile(r"^/email/sections/\d+/body$")

    for fact in selected_facts:
        occurrences: dict[str, list[str]] = {}
        for path, text in text_paths:
            found = _fact_surface_occurrences(
                text,
                fact,
                product_name=context.product.exact_name,
            )
            if found:
                occurrences[path] = [surface for _, _, surface in found]
        text_counts = {path: len(values) for path, values in occurrences.items()}
        evidence_counts: dict[str, int] = {}
        for evidence in bundle.claim_evidence:
            if evidence.fact_id == fact.fact_id:
                evidence_counts[evidence.artifact_path] = (
                    evidence_counts.get(evidence.artifact_path, 0) + 1
                )

        fixed_expected: set[str] = set()
        if bundle.sms is not None and fact.fact_id in sms_selected_ids:
            fixed_expected.add("/sms/text")
            if fact.kind is ClaimType.URL:
                fixed_expected.add("/sms/cta_url")
        expects_email_body = (
            bundle.email is not None
            and fact.fact_id in email_selected_ids
            and fact.kind is not ClaimType.URL
        )
        if bundle.email is not None and fact.fact_id in email_selected_ids:
            fixed_expected.add("/email/plain_text")
            if fact.kind is ClaimType.URL:
                fixed_expected.add("/email/cta_url")

        body_text_paths = {
            path: count for path, count in text_counts.items() if body_pattern.fullmatch(path)
        }
        body_evidence_paths = {
            path: count for path, count in evidence_counts.items() if body_pattern.fullmatch(path)
        }
        dynamic_body_path = (
            next(iter(body_text_paths))
            if expects_email_body
            and len(body_text_paths) == 1
            and sum(body_text_paths.values()) == 1
            else None
        )
        permitted_paths = fixed_expected | (set(body_text_paths) if expects_email_body else set())
        unexpected_text = sorted(set(text_counts) - permitted_paths)
        unexpected_evidence = sorted(
            path
            for path in evidence_counts
            if path not in fixed_expected
            and not (expects_email_body and body_pattern.fullmatch(path))
        )
        fixed_text_invalid = sorted(
            path for path in fixed_expected if text_counts.get(path, 0) != 1
        )
        fixed_evidence_invalid = sorted(
            path for path in fixed_expected if evidence_counts.get(path, 0) != 1
        )
        body_text_invalid = expects_email_body and sum(body_text_paths.values()) != 1
        body_evidence_invalid = expects_email_body and (
            sum(body_evidence_paths.values()) != 1
            or dynamic_body_path is None
            or body_evidence_paths.get(dynamic_body_path, 0) != 1
        )
        if not any(
            (
                unexpected_text,
                unexpected_evidence,
                fixed_text_invalid,
                fixed_evidence_invalid,
                body_text_invalid,
                body_evidence_invalid,
            )
        ):
            continue

        expected_parts = [f"{path} x1" for path in sorted(fixed_expected)]
        if expects_email_body:
            expected_parts.append("one /email/sections/{index}/body x1")
        issue_path: str | None = (
            unexpected_text[0]
            if unexpected_text
            else fixed_text_invalid[0]
            if fixed_text_invalid
            else next(iter(body_text_paths), "/email/sections")
            if body_text_invalid
            else unexpected_evidence[0]
            if unexpected_evidence
            else fixed_evidence_invalid[0]
            if fixed_evidence_invalid
            else dynamic_body_path
        )
        matched_surfaces = occurrences.get(issue_path or "")
        quote = matched_surfaces[0] if matched_surfaces else None
        issues.append(
            InitialFactPlacementIssue(
                fact_id=fact.fact_id,
                source_id=fact.source_id,
                path=issue_path,
                quote=quote,
                expected="; ".join(expected_parts),
                actual=(
                    f"text: {_placement_counts_text(text_counts)}; "
                    f"evidence: {_placement_counts_text(evidence_counts)}"
                ),
            )
        )
    return tuple(issues)


def _rule_channel_outputs(
    bundle: CommunicationBundle,
    channel: str,
) -> list[tuple[str, set[str]]]:
    outputs: list[tuple[str, set[str]]] = []
    if channel in {"", Channel.SMS.value} and bundle.sms is not None:
        outputs.append((bundle.sms.text, set(bundle.sms.fact_refs)))
    if channel in {"", Channel.EMAIL.value} and bundle.email is not None:
        outputs.append((bundle.email.plain_text, set(bundle.email.fact_refs)))
    return outputs


def _active_rule_output_satisfied(
    *,
    rule: dict[str, Any],
    bundle: CommunicationBundle,
    context: ContextBundle,
) -> bool:
    rule_type = str(rule.get("type") or "")
    value = str(rule.get("value") or "")
    scope = rule.get("scope")
    scope = scope if isinstance(scope, dict) else {}
    outputs = _rule_channel_outputs(bundle, str(scope.get("channel") or ""))
    if not outputs:
        return True
    if rule_type == "forbid_phrase":
        return all(value.casefold() not in text.casefold() for text, _ in outputs)
    if rule_type == "require_phrase":
        return all(value.casefold() in text.casefold() for text, _ in outputs)
    if rule_type == "require_fact":
        fact = next((item for item in context.facts if item.fact_id == value), None)
        return (
            fact is not None
            and value in context.content_plan.selected_fact_ids
            and all(
                value in refs
                and (
                    fact.canonical_text in text
                    or any(surface in text for surface in fact.allowed_surface_forms)
                )
                for text, refs in outputs
            )
        )
    if rule_type == "require_concept_id":
        concept = next((item for item in context.concepts if item.concept_id == value), None)
        return (
            concept is not None
            and value in context.content_plan.selected_concept_ids
            and all(
                any(surface in text for surface in concept.accepted_surface_forms)
                for text, _ in outputs
            )
        )
    return rule_type == "tone_hint" and bool(value)


def evaluate_bundle(bundle: CommunicationBundle, context: ContextBundle) -> QualityReport:
    findings: list[Finding] = []
    brief = context.brief_snapshot
    text_paths = list(iter_text_paths(bundle))
    all_text = "\n".join(value for _, value in text_paths)
    facts = {fact.fact_id: fact for fact in context.facts}

    if brief.cta_url not in context.product.allowed_cta_urls:
        _finding(
            findings,
            check_id="QA02",
            artifact=FindingArtifact.BRIEF,
            path="/cta_url",
            actual=brief.cta_url,
            expected="synthetic fact-card CTA allowlist",
            recommendation="Повторно провалидировать ready brief.",
        )
    if context.product.product_id in context.persona.connected_product_ids:
        _finding(
            findings,
            check_id="QA03",
            artifact=FindingArtifact.BRIEF,
            path="/product_id",
            actual=context.product.product_id,
            recommendation="Пакет для уже подключённого синтетического продукта запрещён.",
        )

    rendered_channels = {
        channel
        for channel, artifact in ((Channel.SMS, bundle.sms), (Channel.EMAIL, bundle.email))
        if artifact is not None
    }
    for channel in rendered_channels:
        if (
            context.contact_policy.require_channel_consent
            and not context.persona.channel_consent.get(channel, False)
        ):
            _finding(
                findings,
                check_id="QA04",
                artifact=FindingArtifact.SMS if channel is Channel.SMS else FindingArtifact.EMAIL,
                recommendation="Подавить запрещённый канал.",
                actual=channel.value,
            )

    if bundle.sms is not None and context.product.exact_name not in bundle.sms.text:
        _finding(
            findings,
            check_id="QA05",
            artifact=FindingArtifact.SMS,
            path="/sms/text",
            expected=context.product.exact_name,
            recommendation="Использовать точное название продукта.",
        )
    if bundle.email is not None and not any(
        context.product.exact_name in value
        for value in (bundle.email.subject, bundle.email.headline, bundle.email.plain_text)
    ):
        _finding(
            findings,
            check_id="QA05",
            artifact=FindingArtifact.EMAIL,
            expected=context.product.exact_name,
            recommendation="Использовать точное название продукта в e-mail.",
        )

    for path, value in text_paths:
        for url in URL_RE.findall(value):
            normalized_url = url.rstrip(".,;:!?")
            if normalized_url not in context.product.allowed_cta_urls:
                _finding(
                    findings,
                    check_id="QA07",
                    artifact=_artifact_for_path(path),
                    path=path,
                    quote=normalized_url,
                    recommendation="Использовать только HTTPS URL из fact-card allowlist.",
                )

    required_disclaimers = set(context.product.required_disclaimer_ids) | set(
        context.legal_policy.required_disclaimer_ids
    )
    for fact_id in sorted(required_disclaimers):
        fact = facts.get(fact_id)
        for channel, artifact_text in (
            (Channel.SMS, bundle.sms.text if bundle.sms else None),
            (Channel.EMAIL, bundle.email.plain_text if bundle.email else None),
        ):
            if artifact_text is not None and (
                fact is None or fact.canonical_text not in artifact_text
            ):
                _finding(
                    findings,
                    check_id="QA08",
                    artifact=FindingArtifact.SMS
                    if channel is Channel.SMS
                    else FindingArtifact.EMAIL,
                    expected=fact_id,
                    source_ids=(fact.source_id,) if fact else (),
                    recommendation="Добавить обязательную маркировку из fact ledger.",
                )
    if bundle.email is not None and not required_disclaimers.issubset(bundle.email.disclaimer_ids):
        _finding(
            findings,
            check_id="QA08",
            artifact=FindingArtifact.EMAIL,
            path="/email/disclaimer_ids",
            expected=", ".join(sorted(required_disclaimers)),
            recommendation="Указать все обязательные disclaimer IDs.",
        )

    for claim_id in context.product.prohibited_claim_ids:
        pattern = FORBIDDEN_PATTERNS.get(claim_id)
        match = pattern.search(all_text) if pattern else None
        if match:
            _finding(
                findings,
                check_id="QA09",
                artifact=FindingArtifact.PACKAGE,
                quote=match.group(0),
                expected=f"forbidden claim {claim_id} absent",
                recommendation="Удалить запрещённое утверждение.",
            )

    if bundle.sms is not None and (
        brief.cta_label not in bundle.sms.text or bundle.sms.cta_url != brief.cta_url
    ):
        _finding(
            findings,
            check_id="QA10",
            artifact=FindingArtifact.SMS,
            recommendation="Вернуть обязательные CTA label и URL из ready brief.",
        )
    if bundle.email is not None and (
        bundle.email.cta_label != brief.cta_label or bundle.email.cta_url != brief.cta_url
    ):
        _finding(
            findings,
            check_id="QA10",
            artifact=FindingArtifact.EMAIL,
            recommendation="Вернуть обязательные CTA label и URL из ready brief.",
        )

    for channel, refs in (
        (Channel.SMS, set(bundle.sms.fact_refs) if bundle.sms else None),
        (Channel.EMAIL, set(bundle.email.fact_refs) if bundle.email else None),
    ):
        channel_facts = set(context.content_plan.fact_ids_for(channel))
        if refs is not None and not channel_facts.issubset(refs):
            _finding(
                findings,
                check_id="QA11",
                artifact=FindingArtifact.SMS if channel is Channel.SMS else FindingArtifact.EMAIL,
                expected=", ".join(sorted(channel_facts)),
                actual=", ".join(sorted(refs)),
                recommendation="Применить и сослаться на каждый выбранный обязательный факт.",
            )
    selected_concepts = set(context.content_plan.selected_concept_ids)
    concept_by_id = {concept.concept_id: concept for concept in context.concepts}
    for concept_id in selected_concepts:
        concept = concept_by_id.get(concept_id)
        if concept is None or not any(
            surface in all_text for surface in concept.accepted_surface_forms
        ):
            _finding(
                findings,
                check_id="QA11",
                artifact=FindingArtifact.PACKAGE,
                expected=concept_id,
                recommendation="Применить выбранное понятие в разрешённой формулировке.",
            )

    metrics = sms_metrics(bundle.sms.text) if bundle.sms is not None else None
    if metrics is not None:
        max_segments = int(context.channel_policies.get("sms", {}).get("max_segments", 3))
        if metrics.segments > max_segments:
            _finding(
                findings,
                check_id="QA12",
                artifact=FindingArtifact.SMS,
                path="/sms/text",
                expected=f"at most {max_segments} segments",
                actual=str(metrics.segments),
                recommendation="Сократить SMS без потери обязательных фактов.",
            )

    if bundle.email is not None:
        email_policy = context.channel_policies.get("email", {})
        subject_max = int(email_policy.get("subject_max_chars", 78))
        preheader_max = int(email_policy.get("preheader_max_chars", 140))
        if len(bundle.email.subject) > subject_max or len(bundle.email.preheader) > preheader_max:
            _finding(
                findings,
                check_id="QA13",
                artifact=FindingArtifact.EMAIL,
                expected=f"subject<={subject_max}, preheader<={preheader_max}",
                actual=(
                    f"subject={len(bundle.email.subject)}, preheader={len(bundle.email.preheader)}"
                ),
                recommendation="Сократить заголовочные поля e-mail.",
            )

    rendered_html = render_email_html(bundle.email) if bundle.email is not None else ""
    if re.search(r"<script|javascript:|\son[a-z]+\s*=", rendered_html, re.IGNORECASE):
        _finding(
            findings,
            check_id="QA14",
            artifact=FindingArtifact.EMAIL,
            recommendation="Отклонить небезопасный HTML.",
        )

    for path, value in text_paths:
        match = PLACEHOLDER_RE.search(value)
        if match:
            _finding(
                findings,
                check_id="QA15",
                artifact=_artifact_for_path(path),
                path=path,
                quote=match.group(0),
                recommendation="Удалить placeholder или внутреннюю управляющую метку.",
            )
        pii_match = PII_SECRET_RE.search(value)
        if pii_match:
            _finding(
                findings,
                check_id="QA16",
                artifact=_artifact_for_path(path),
                path=path,
                quote="redacted-pattern-match",
                recommendation="Удалить PII, внутренний домен или secret-like значение.",
            )

    claim_ids: set[str] = set()
    for evidence in bundle.claim_evidence:
        if evidence.claim_id in claim_ids:
            _finding(
                findings,
                check_id="QA17",
                artifact=_artifact_for_path(evidence.artifact_path),
                path=evidence.artifact_path,
                recommendation="Использовать уникальный claim ID.",
            )
        claim_ids.add(evidence.claim_id)
        fact = facts.get(evidence.fact_id)
        try:
            actual_value = _resolve_pointer(bundle, evidence.artifact_path)
        except (KeyError, IndexError, TypeError, ValueError):
            actual_value = None
        if (
            not isinstance(actual_value, str)
            or evidence.text_fragment not in actual_value
            or fact is None
            or evidence.source_id != fact.source_id
            or evidence.claim_type is not fact.kind
            or evidence.normalized_value != fact.normalized_value
        ):
            _finding(
                findings,
                check_id="QA17",
                artifact=_artifact_for_path(evidence.artifact_path),
                path=evidence.artifact_path,
                quote=evidence.text_fragment,
                source_ids=(evidence.source_id,),
                recommendation="Исправить claim fragment/path/fact/source mapping.",
            )

    actual_claims = [
        claim
        for path, value in text_paths
        for claim in extract_actual_claims(path, value, product_name=context.product.exact_name)
    ]
    for claim in actual_claims:
        matched_facts = [fact for fact in facts.values() if _fact_matches(claim, fact)]
        declared = any(
            evidence.artifact_path == claim.path
            and evidence.fact_id in {fact.fact_id for fact in matched_facts}
            for evidence in bundle.claim_evidence
        )
        if not matched_facts or not declared:
            _finding(
                findings,
                check_id="QA18",
                artifact=_artifact_for_path(claim.path),
                path=claim.path,
                quote=claim.fragment,
                source_ids=tuple(fact.source_id for fact in matched_facts),
                recommendation="Удалить неподтверждённый claim или добавить точное evidence.",
            )

    for issue in initial_fact_placement_issues(bundle, context):
        _finding(
            findings,
            check_id="QA18",
            artifact=_artifact_for_path(issue.path or ""),
            path=issue.path,
            quote=issue.quote,
            expected=issue.expected,
            actual=issue.actual,
            source_ids=(issue.source_id,),
            recommendation=(
                "Разместить выбранный факт и доказательство ровно на путях контракта "
                "начальной генерации."
            ),
        )

    if (bundle.sms and bundle.sms.personalization_refs) or (
        bundle.email and bundle.email.personalization_refs
    ):
        _finding(
            findings,
            check_id="QA19",
            artifact=FindingArtifact.PACKAGE,
            recommendation="Использовать только типизированные и видимые personalization refs.",
        )
    if context.operation.value == "initial" and (
        context.allowed_changed_paths or context.protected_paths
    ):
        _finding(
            findings,
            check_id="QA20",
            artifact=FindingArtifact.PACKAGE,
            recommendation="Область правок допустима только в контексте новой версии.",
        )

    active_by_id = {
        str(rule.get("rule_version_id") or ""): rule
        for rule in context.active_rules
        if isinstance(rule, dict)
    }
    for rule_version_id in context.content_plan.applied_rule_version_ids:
        rule = active_by_id.get(rule_version_id)
        scope = rule.get("scope") if isinstance(rule, dict) else None
        scope = scope if isinstance(scope, dict) else {}
        value = str(rule.get("value") or "") if isinstance(rule, dict) else ""
        product_ids = {str(item) for item in scope.get("product_ids") or []}
        segment_ids = {str(item) for item in scope.get("segment_ids") or []}
        rule_channel = str(scope.get("channel") or "")
        scope_matches = (
            (not product_ids or brief.product_id in product_ids)
            and (not segment_ids or brief.segment_id in segment_ids)
            and (not rule_channel or rule_channel in {item.value for item in brief.channels})
        )
        if (
            rule is None
            or not scope_matches
            or not _active_rule_output_satisfied(
                rule=rule,
                bundle=bundle,
                context=context,
            )
        ):
            _finding(
                findings,
                check_id="QA21",
                artifact=FindingArtifact.PACKAGE,
                expected=rule_version_id,
                recommendation="Применять только активное правило внутри точной области действия.",
            )

    blocker_count = sum(finding.blocking for finding in findings)
    warning_count = sum(finding.severity is FindingSeverity.WARNING for finding in findings)
    package_hash = hash_value(bundle.model_dump(mode="json"))
    return QualityReport(
        registry_hash=REGISTRY_HASH,
        package_hash=package_hash,
        context_version=context.context_version,
        findings=findings,
        approvable=blocker_count == 0,
        checked_ids=QA_CHECK_IDS,
        checked_fact_ids=tuple(sorted(facts)),
        checked_claim_ids=tuple(sorted(claim_ids)),
        checked_policy_ids=(context.contact_policy.policy_id, context.legal_policy.policy_id),
        sms_metrics=metrics,
        deterministic_score=max(0, 100 - blocker_count * 20 - warning_count * 5),
        evidence_hashes={
            "package": package_hash,
            "context": context.context_version,
            "registry": REGISTRY_HASH,
            "email_html": hash_value(rendered_html),
        },
    )
