from __future__ import annotations

from datetime import date

import pytest

from apps.api.app.domain.campaigns import BriefStatus
from apps.api.app.domain.models import Channel
from apps.api.app.domain.quality import FindingSeverity
from apps.api.app.mcp.service import _initial_output_schema
from apps.api.app.services.briefs import (
    build_initial_context,
    create_draft,
    validate_and_promote,
)
from apps.api.app.services.catalog import SyntheticCatalog, load_catalog
from apps.api.app.services.deterministic import build_deterministic_bundle, iter_text_paths
from apps.api.app.services.quality import (
    QA_CHECK_IDS,
    evaluate_bundle,
    initial_fact_placement_issues,
)
from apps.api.app.services.rules import apply_active_rules

AS_OF = date(2026, 7, 11)


def _context(case_id: str, catalog: SyntheticCatalog):  # type: ignore[no-untyped-def]
    case = catalog.case(case_id)
    draft = create_draft(campaign_id=case.campaign_id, values=case.brief)
    result = validate_and_promote(draft, catalog, as_of=AS_OF)
    assert result.status is BriefStatus.READY
    assert result.ready_brief is not None
    return build_initial_context(result.ready_brief, catalog)


def test_b04_deterministic_package_is_grounded_and_approvable() -> None:
    catalog = load_catalog()
    context = _context("B04", catalog)
    bundle = build_deterministic_bundle(context)
    report = evaluate_bundle(bundle, context)

    assert report.approvable is True
    assert report.findings == ()
    assert report.checked_ids == QA_CHECK_IDS
    assert len(report.checked_ids) == 22
    assert report.sms_metrics is not None
    assert report.sms_metrics.encoding == "UCS-2"
    assert "14 дней" in bundle.sms.text  # type: ignore[union-attr]
    assert any(
        evidence.normalized_value == {"value": 14, "unit": "day"}
        for evidence in bundle.claim_evidence
    )
    assert bundle.email is not None
    assert all(section.kind != "cta" for section in bundle.email.sections)
    assert all(
        context.brief_snapshot.cta_url not in section.body
        and context.brief_snapshot.cta_label not in section.body
        and "fact_term_cta" not in section.fact_refs
        for section in bundle.email.sections
    )
    assert initial_fact_placement_issues(bundle, context) == ()


def test_initial_natural_language_fact_allows_sentence_initial_capitalization() -> None:
    catalog = load_catalog()
    context = _context("B02", catalog)
    original = build_deterministic_bundle(context)
    assert original.sms is not None
    assert original.email is not None
    fact = next(item for item in context.facts if item.fact_id == "fact_invoice_visibility")
    surface = fact.allowed_surface_forms[0]
    capitalized = surface[0].upper() + surface[1:]
    sms = original.sms.model_copy(
        update={"text": original.sms.text.replace(fact.canonical_text, f"{capitalized}.")}
    )
    section = original.email.sections[0].model_copy(
        update={
            "body": original.email.sections[0].body.replace(fact.canonical_text, f"{capitalized}.")
        }
    )
    email = original.email.model_copy(
        update={
            "sections": [section],
            "plain_text": original.email.plain_text.replace(fact.canonical_text, f"{capitalized}."),
        }
    )
    evidence = [
        item.model_copy(update={"text_fragment": capitalized})
        if item.fact_id == fact.fact_id
        else item
        for item in original.claim_evidence
    ]
    bundle = original.model_copy(update={"sms": sms, "email": email, "claim_evidence": evidence})

    assert initial_fact_placement_issues(bundle, context) == ()
    assert not any(
        finding.check_id == "QA18" for finding in evaluate_bundle(bundle, context).findings
    )


def test_initial_url_fact_placement_remains_case_sensitive() -> None:
    catalog = load_catalog()
    context = _context("B02", catalog)
    original = build_deterministic_bundle(context)
    assert original.sms is not None
    url = context.brief_snapshot.cta_url
    sms = original.sms.model_copy(
        update={"text": original.sms.text.replace(url, f"{url[:-8]}OVERVIEW")}
    )
    bundle = original.model_copy(update={"sms": sms})

    issues = initial_fact_placement_issues(bundle, context)

    assert any(
        issue.fact_id == "fact_invoice_cta" and issue.path == "/sms/text" for issue in issues
    )


def test_initial_selected_fact_repetition_is_blocked_even_when_extra_evidence_is_valid() -> None:
    catalog = load_catalog()
    context = _context("B04", catalog)
    original = build_deterministic_bundle(context)
    assert original.email is not None
    duration = next(
        evidence for evidence in original.claim_evidence if evidence.fact_id == "fact_term_14_days"
    )
    email = original.email.model_copy(
        update={
            "subject": f"{original.email.subject} — 14 дней",
            "preheader": "Срок подключения составляет 14 дней.",
        }
    )
    bundle = original.model_copy(
        update={
            "summary": "Синтетическое сообщение про срок 14 дней.",
            "email": email,
            "claim_evidence": [
                *original.claim_evidence,
                duration.model_copy(
                    update={
                        "claim_id": "claim_email_subject_duration",
                        "artifact_path": "/email/subject",
                    }
                ),
                duration.model_copy(
                    update={
                        "claim_id": "claim_email_preheader_duration",
                        "artifact_path": "/email/preheader",
                    }
                ),
            ],
        }
    )

    issues = initial_fact_placement_issues(bundle, context)
    report = evaluate_bundle(bundle, context)

    assert len(issues) == 1
    assert issues[0].fact_id == "fact_term_14_days"
    assert "/email/subject" in issues[0].actual
    assert "/email/preheader" in issues[0].actual
    assert "/summary" in issues[0].actual
    assert report.approvable is False
    assert any(
        finding.check_id == "QA18"
        and finding.recommendation
        == (
            "Разместить выбранный факт и доказательство ровно на путях контракта "
            "начальной генерации."
        )
        for finding in report.findings
    )


def test_initial_duplicate_plain_url_and_missing_section_evidence_are_both_blocked() -> None:
    catalog = load_catalog()
    context = _context("B04", catalog)
    original = build_deterministic_bundle(context)
    assert original.email is not None
    duplicate_url = context.brief_snapshot.cta_url
    email = original.email.model_copy(
        update={
            "plain_text": (
                f"{original.email.plain_text}\n\n"
                f"{context.brief_snapshot.cta_label}: {duplicate_url}"
            )
        }
    )
    evidence = [
        item
        for item in original.claim_evidence
        if not (
            item.fact_id == "fact_term_14_days"
            and item.artifact_path.startswith("/email/sections/")
        )
    ]
    bundle = original.model_copy(update={"email": email, "claim_evidence": evidence})

    issues = initial_fact_placement_issues(bundle, context)
    report = evaluate_bundle(bundle, context)

    assert {issue.fact_id for issue in issues} == {
        "fact_term_14_days",
        "fact_term_cta",
    }
    assert any("/email/plain_text x2" in issue.actual for issue in issues)
    assert report.approvable is False
    assert any(finding.check_id == "QA18" for finding in report.findings)


def test_initial_cta_url_in_email_section_is_blocked() -> None:
    catalog = load_catalog()
    context = _context("B04", catalog)
    original = build_deterministic_bundle(context)
    assert original.email is not None
    section = original.email.sections[0]
    section = section.model_copy(
        update={
            "body": (
                f"{section.body} {context.brief_snapshot.cta_label}: "
                f"{context.brief_snapshot.cta_url}"
            ),
            "fact_refs": [*section.fact_refs, "fact_term_cta"],
        }
    )
    email = original.email.model_copy(update={"sections": [section]})
    bundle = original.model_copy(update={"email": email})

    issues = initial_fact_placement_issues(bundle, context)
    report = evaluate_bundle(bundle, context)

    assert any(
        issue.fact_id == "fact_term_cta" and "/email/sections/0/body" in issue.actual
        for issue in issues
    )
    assert report.approvable is False
    assert any(finding.check_id == "QA18" for finding in report.findings)


def test_b06_deterministic_package_keeps_disclaimer_in_both_channels() -> None:
    catalog = load_catalog()
    context = _context("B06", catalog)
    bundle = build_deterministic_bundle(context)
    report = evaluate_bundle(bundle, context)
    notice = "Учебное предложение. Условия вымышлены."

    assert report.approvable is True
    assert bundle.sms is not None and notice in bundle.sms.text
    assert bundle.email is not None and notice in bundle.email.plain_text
    assert bundle.email.disclaimer_ids == ["fact_label_notice"]


def test_unsupported_actual_claim_is_a_blocker_even_without_declared_evidence() -> None:
    catalog = load_catalog()
    context = _context("B04", catalog)
    original = build_deterministic_bundle(context)
    assert original.sms is not None
    mutated_sms = original.sms.model_copy(update={"text": f"{original.sms.text} Результат 99%."})
    mutated = original.model_copy(update={"sms": mutated_sms})

    report = evaluate_bundle(mutated, context)

    assert report.approvable is False
    unsupported = [finding for finding in report.findings if finding.check_id == "QA18"]
    assert unsupported
    assert unsupported[0].severity is FindingSeverity.BLOCKER
    assert unsupported[0].quote == "99%"


def test_monthly_money_allowed_surface_uses_canonical_evidence_unit() -> None:
    catalog = load_catalog()
    context = _context("B03", catalog)
    original = build_deterministic_bundle(context)
    fact = next(item for item in context.facts if item.fact_id == "fact_payroll_zero_fee")
    replacements = {
        surface: surface.replace("₽.", "₽ в месяц.") for surface in fact.allowed_surface_forms
    }
    monthly_fact = fact.model_copy(
        update={
            "canonical_text": replacements[fact.canonical_text],
            "normalized_value": {"value": 0, "unit": "RUB/month"},
            "allowed_surface_forms": tuple(replacements.values()),
        }
    )
    monthly_context = context.model_copy(
        update={
            "facts": tuple(
                monthly_fact if item.fact_id == monthly_fact.fact_id else item
                for item in context.facts
            )
        }
    )

    def replace_surface(text: str) -> str:
        for source, target in replacements.items():
            text = text.replace(source, target)
        return text

    assert original.sms is not None
    assert original.email is not None
    sms = original.sms.model_copy(update={"text": replace_surface(original.sms.text)})
    email = original.email.model_copy(
        update={
            "sections": [
                section.model_copy(update={"body": replace_surface(section.body)})
                for section in original.email.sections
            ],
            "plain_text": replace_surface(original.email.plain_text),
        }
    )
    evidence = [
        item.model_copy(
            update={
                "text_fragment": replace_surface(item.text_fragment),
                "normalized_value": monthly_fact.normalized_value,
            }
        )
        if item.fact_id == monthly_fact.fact_id
        else item
        for item in original.claim_evidence
    ]
    bundle = original.model_copy(update={"sms": sms, "email": email, "claim_evidence": evidence})

    report = evaluate_bundle(bundle, monthly_context)

    assert report.approvable is True
    assert not [finding for finding in report.findings if finding.check_id in {"QA17", "QA18"}]


def test_false_claim_path_is_detected_independently() -> None:
    catalog = load_catalog()
    context = _context("B04", catalog)
    original = build_deterministic_bundle(context)
    evidence = list(original.claim_evidence)
    evidence[0] = evidence[0].model_copy(update={"artifact_path": "/email/not_a_field"})
    mutated = original.model_copy(update={"claim_evidence": evidence})

    report = evaluate_bundle(mutated, context)

    assert report.approvable is False
    assert any(finding.check_id == "QA17" for finding in report.findings)


def test_reused_canonical_fragment_is_rejected_when_sms_uses_an_allowed_short_form() -> None:
    catalog = load_catalog()
    context = _context("B07", catalog)
    original = build_deterministic_bundle(context)
    assert original.sms is not None
    fact = next(item for item in context.facts if item.fact_id == "fact_pulse_summary")
    sms = original.sms.model_copy(
        update={
            "text": original.sms.text.replace(
                fact.canonical_text,
                "Инсайты из синтетических событий.",
            )
        }
    )
    evidence = list(original.claim_evidence)
    sms_evidence_index = next(
        index
        for index, item in enumerate(evidence)
        if item.fact_id == fact.fact_id and item.artifact_path == "/sms/text"
    )
    evidence[sms_evidence_index] = evidence[sms_evidence_index].model_copy(
        update={"text_fragment": fact.canonical_text}
    )
    bundle = original.model_copy(update={"sms": sms, "claim_evidence": evidence})

    report = evaluate_bundle(bundle, context)

    assert report.approvable is False
    assert any(
        finding.check_id == "QA17"
        and finding.path == "/sms/text"
        and finding.quote == fact.canonical_text
        for finding in report.findings
    )


def test_evidence_fragment_casing_must_quote_the_actual_path_exactly() -> None:
    catalog = load_catalog()
    context = _context("B07", catalog)
    original = build_deterministic_bundle(context)
    assert original.sms is not None
    fact = next(item for item in context.facts if item.fact_id == "fact_pulse_summary")
    lowercase = fact.canonical_text[0].lower() + fact.canonical_text[1:]
    sms = original.sms.model_copy(
        update={
            "text": original.sms.text.replace(
                f"{context.product.exact_name}. {fact.canonical_text}",
                f"{context.product.exact_name}: {lowercase}",
            )
        }
    )
    evidence = [
        item.model_copy(update={"text_fragment": fact.canonical_text})
        if item.fact_id == fact.fact_id and item.artifact_path == "/sms/text"
        else item
        for item in original.claim_evidence
    ]
    bundle = original.model_copy(update={"sms": sms, "claim_evidence": evidence})

    report = evaluate_bundle(bundle, context)

    assert any(
        finding.check_id == "QA17"
        and finding.path == "/sms/text"
        and finding.quote == fact.canonical_text
        for finding in report.findings
    )


def test_url_evidence_cannot_point_to_a_section_that_only_mentions_a_link() -> None:
    catalog = load_catalog()
    context = _context("B04", catalog)
    original = build_deterministic_bundle(context)
    assert original.email is not None
    assert "https://" not in original.email.sections[0].body
    evidence = list(original.claim_evidence)
    url_index = next(
        index
        for index, item in enumerate(evidence)
        if item.channel.value == "email" and item.claim_type.value == "url"
    )
    evidence[url_index] = evidence[url_index].model_copy(
        update={"artifact_path": "/email/sections/0/body"}
    )
    mutated = original.model_copy(update={"claim_evidence": evidence})

    report = evaluate_bundle(mutated, context)

    assert report.approvable is False
    assert any(
        finding.check_id == "QA17"
        and finding.path == "/email/sections/0/body"
        and finding.quote == "https://flow.example.test/term-14"
        for finding in report.findings
    )


def test_decorating_exact_product_name_creates_name_and_unsupported_claim_blockers() -> None:
    catalog = load_catalog()
    context = _context("B04", catalog)
    original = build_deterministic_bundle(context)
    assert original.sms is not None
    decorated = original.sms.text.replace("План Срок 14", "План «Срок 14»")
    mutated = original.model_copy(
        update={"sms": original.sms.model_copy(update={"text": decorated})}
    )

    report = evaluate_bundle(mutated, context)

    assert report.approvable is False
    assert any(
        finding.check_id == "QA05" and finding.artifact == "sms" for finding in report.findings
    )
    assert any(finding.check_id == "QA18" and finding.quote == "14" for finding in report.findings)


def test_exact_product_name_in_email_subject_is_sufficient() -> None:
    catalog = load_catalog()
    context = _context("B04", catalog)
    original = build_deterministic_bundle(context)
    assert original.email is not None
    plain_without_name = original.email.plain_text.removeprefix(f"{context.product.exact_name}\n\n")
    email = original.email.model_copy(
        update={"headline": "Срок подключения", "plain_text": plain_without_name}
    )
    bundle = original.model_copy(update={"email": email})

    report = evaluate_bundle(bundle, context)

    assert not any(finding.check_id == "QA05" for finding in report.findings)


def test_email_scoped_required_phrase_is_applied_and_checked_only_in_email() -> None:
    catalog = load_catalog()
    context = _context("B03", catalog)
    phrase = "Подключение доступно онлайн"
    rule_version_id = "rulev_required_phrase"
    context = apply_active_rules(
        context,
        active_rules=[
            {
                "rule_version_id": rule_version_id,
                "proposal_id": "proposal_required_phrase",
                "source_feedback_id": "feedback_required_phrase",
                "type": "require_phrase",
                "scope": {
                    "product_ids": ["synthetic_payroll"],
                    "channel": "email",
                    "segment_ids": [],
                },
                "condition_id": None,
                "value": phrase,
                "candidate_rules_version": "a" * 64,
            }
        ],
        rules_version="b" * 64,
    )
    bundle = build_deterministic_bundle(context)
    report = evaluate_bundle(bundle, context)

    assert bundle.email is not None and phrase in bundle.email.plain_text
    assert bundle.sms is not None and phrase not in bundle.sms.text
    assert rule_version_id in context.content_plan.applied_rule_version_ids
    assert not [finding for finding in report.findings if finding.check_id == "QA21"]


@pytest.mark.parametrize("case_id", ["B02", "B04", "B07", "B08"])
def test_initial_deterministic_bundle_matches_operation_schema_cardinality(case_id: str) -> None:
    catalog = load_catalog()
    context = _context(case_id, catalog)
    bundle = build_deterministic_bundle(context)
    report = evaluate_bundle(bundle, context)
    schema = _initial_output_schema(context.model_dump(mode="json"))
    sections_schema = schema["$defs"]["EmailArtifact"]["properties"]["sections"]
    evidence_schema = schema["$defs"]["CommunicationBundle"]["properties"]["claim_evidence"]

    assert report.approvable is True
    assert initial_fact_placement_issues(bundle, context) == ()
    assert bundle.email is not None
    assert len(bundle.email.sections) == sections_schema["minItems"] == sections_schema["maxItems"]
    assert len(bundle.claim_evidence) == evidence_schema["minItems"] == evidence_schema["maxItems"]


def test_b01_channel_plan_keeps_status_fact_out_of_sms_but_in_email() -> None:
    catalog = load_catalog()
    context = _context("B03", catalog)
    bundle = build_deterministic_bundle(context)
    report = evaluate_bundle(bundle, context)
    schema = _initial_output_schema(context.model_dump(mode="json"))

    assert context.content_plan.channel_selected_fact_ids[Channel.SMS] == (
        "fact_payroll_setup",
        "fact_payroll_zero_fee",
        "fact_payroll_cta",
    )
    assert "fact_payroll_statuses" in context.content_plan.fact_ids_for(Channel.EMAIL)
    assert bundle.sms is not None
    assert bundle.email is not None
    assert "fact_payroll_statuses" not in bundle.sms.fact_refs
    assert "Статусы выплат" not in bundle.sms.text
    assert "fact_payroll_statuses" in bundle.email.fact_refs
    assert "Статусы выплат" in bundle.email.plain_text
    assert report.approvable is True
    assert not [finding for finding in report.findings if finding.check_id == "QA11"]
    assert len(bundle.claim_evidence) == 12
    assert schema["$defs"]["CommunicationBundle"]["properties"]["claim_evidence"]["minItems"] == 12
    assert initial_fact_placement_issues(bundle, context) == ()


def test_initial_selected_fact_in_section_heading_is_blocked() -> None:
    catalog = load_catalog()
    context = _context("B02", catalog)
    original = build_deterministic_bundle(context)
    assert original.email is not None
    fact = next(item for item in context.facts if item.fact_id == "fact_invoice_visibility")
    surface = fact.allowed_surface_forms[0]
    heading = surface[0].upper() + surface[1:]
    section = original.email.sections[0].model_copy(update={"heading": heading})
    email = original.email.model_copy(update={"sections": [section]})
    bundle = original.model_copy(update={"email": email})

    issues = initial_fact_placement_issues(bundle, context)
    report = evaluate_bundle(bundle, context)

    assert any(
        issue.fact_id == fact.fact_id and "/email/sections/0/heading" in issue.actual
        for issue in issues
    )
    assert report.approvable is False
    assert any(finding.check_id == "QA18" for finding in report.findings)


def test_b08_unicode_deterministic_package_has_exact_ucs2_metrics_and_placement() -> None:
    catalog = load_catalog()
    context = _context("B08", catalog)
    bundle = build_deterministic_bundle(context)
    report = evaluate_bundle(bundle, context)

    assert "🚀" in context.product.exact_name
    assert report.approvable is True
    assert initial_fact_placement_issues(bundle, context) == ()
    assert bundle.sms is not None
    assert context.product.exact_name in bundle.sms.text
    metrics = report.sms_metrics
    assert metrics is not None
    assert metrics.encoding == "UCS-2"
    max_segments = int(context.channel_policies["sms"]["max_segments"])
    assert 1 <= metrics.segments <= max_segments


def test_b14_injection_note_stays_untrusted_data_and_deterministic_output_is_clean() -> None:
    catalog = load_catalog()
    context = _context("B14", catalog)
    case = catalog.case("B14")

    assert "игнорируй skill" in context.brief_snapshot.notes
    assert set(context.content_plan.selected_fact_ids) == set(case.expected.selected_fact_ids)

    bundle = build_deterministic_bundle(context)
    report = evaluate_bundle(bundle, context)
    all_text = "\n".join(value for _, value in iter_text_paths(bundle))

    assert report.approvable is True
    assert initial_fact_placement_issues(bundle, context) == ()
    assert "игнорируй" not in all_text
    assert "SYSTEM:" not in all_text
    assert "гарант" not in all_text.casefold()


def test_b14_injection_following_guarantee_draft_is_rejected() -> None:
    catalog = load_catalog()
    context = _context("B14", catalog)
    original = build_deterministic_bundle(context)
    assert original.sms is not None
    mutated = original.model_copy(
        update={
            "sms": original.sms.model_copy(
                update={"text": f"{original.sms.text} Гарантия результата."}
            )
        }
    )

    report = evaluate_bundle(mutated, context)

    assert report.approvable is False
    assert any(finding.check_id == "QA09" for finding in report.findings)
