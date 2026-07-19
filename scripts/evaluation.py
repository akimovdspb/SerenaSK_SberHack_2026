from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import tempfile
from datetime import UTC, datetime
from typing import Any

from apps.api.app.domain.campaigns import CampaignBriefInput, ContextBundle
from apps.api.app.domain.learning import (
    FeedbackCreateRequest,
    RuleApprovalRequest,
    RuleRollbackRequest,
)
from apps.api.app.domain.models import CommunicationBundle, RuleScope
from apps.api.app.domain.workflow import CampaignState, PackageView
from apps.api.app.services.catalog import DEFAULT_DATA_DIR
from apps.api.app.workflow.store import WorkflowStore

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXPECTED_PATH = ROOT / "data" / "synthetic" / "evaluation" / "business_expected.json"
REPLAY_ROOT = ROOT / "runtime" / "evaluation" / "replay"
ONLINE_BANK_CONCEPT = "payouts_via_online_bank"
ONLINE_BANK_FRAGMENT = "подготовка выплат в онлайн-банке"


class EvaluationError(RuntimeError):
    pass


def _load_object(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"evaluation fixture is unreadable: {path.name}") from exc
    if not isinstance(value, dict):
        raise EvaluationError(f"evaluation fixture must be an object: {path.name}")
    return {str(key): item for key, item in value.items()}


def expected_cases(path: pathlib.Path = EXPECTED_PATH) -> dict[str, dict[str, Any]]:
    document = _load_object(path)
    rows = document.get("cases")
    if document.get("schema_version") != 1 or not isinstance(rows, list):
        raise EvaluationError("business expected fixture schema is invalid")
    result: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            raise EvaluationError("business expected row must be an object")
        row = {str(key): value for key, value in raw.items()}
        case_id = str(row.get("case_id") or "")
        assertions = row.get("hard_assertions")
        if (
            not case_id
            or case_id in result
            or not isinstance(assertions, list)
            or not all(isinstance(item, str) for item in assertions)
        ):
            raise EvaluationError("business expected case identity/assertions are invalid")
        result[case_id] = row
    canonical_ids = {f"B{ordinal:02d}" for ordinal in range(1, 16)}
    if set(result) != canonical_ids:
        raise EvaluationError("business expected fixture must contain exactly B01-B15")
    return result


def review_packet_case_ids(path: pathlib.Path = EXPECTED_PATH) -> tuple[str, ...]:
    document = _load_object(path)
    raw = document.get("review_packet_case_ids")
    if not isinstance(raw, list) or len(raw) != 6 or not all(isinstance(item, str) for item in raw):
        raise EvaluationError("business expected fixture must preselect six review packet cases")
    case_ids = tuple(raw)
    if len(set(case_ids)) != 6 or any(case_id not in expected_cases(path) for case_id in case_ids):
        raise EvaluationError("review packet case selection is invalid")
    return case_ids


def _all_copy(bundle: CommunicationBundle) -> str:
    return "\n".join(
        (
            bundle.sms.text if bundle.sms is not None else "",
            bundle.email.plain_text if bundle.email is not None else "",
        )
    )


def _channels(package: PackageView | None) -> dict[str, str]:
    if package is None:
        return {"sms": "NOT_RUN", "email": "NOT_RUN"}
    suppressions = {item.channel.value for item in package.bundle.channel_suppressions}
    return {
        "sms": "GENERATED"
        if package.bundle.sms is not None
        else "SUPPRESSED"
        if "sms" in suppressions
        else "NOT_RUN",
        "email": "GENERATED"
        if package.bundle.email is not None
        else "SUPPRESSED"
        if "email" in suppressions
        else "NOT_RUN",
    }


def _base_assertions(package: PackageView | None) -> dict[str, bool]:
    return {
        "grounded_package": bool(
            package is not None
            and package.quality_report.approvable
            and not package.quality_report.findings
            and len(package.quality_report.checked_ids) == 22
        ),
        "no_unsupported_actual_claim": bool(
            package is not None
            and not any(finding.check_id == "QA18" for finding in package.quality_report.findings)
        ),
    }


def _outcome(
    *,
    expected: dict[str, Any],
    initial_state: str,
    terminal_state: str,
    package: PackageView | None,
    assertions: dict[str, bool],
) -> dict[str, Any]:
    required = [str(item) for item in expected["hard_assertions"]]
    expected_channels = expected.get("expected_channels")
    actual_channels = _channels(package)
    assertion_results = {name: bool(assertions.get(name)) for name in required}
    passed = (
        initial_state == expected.get("expected_initial")
        and terminal_state == expected.get("expected_terminal")
        and actual_channels == expected_channels
        and all(assertion_results.values())
    )
    return {
        "case_id": expected["case_id"],
        "expected_initial": expected["expected_initial"],
        "actual_initial": initial_state,
        "expected_terminal": expected["expected_terminal"],
        "actual_terminal": terminal_state,
        "expected_channels": expected_channels,
        "actual_channels": actual_channels,
        "live_target": bool(expected.get("live_target")),
        "mode": package.mode if package is not None else "validation_only",
        "assertions": assertion_results,
        "passed": passed,
        "package": package.model_dump(mode="json") if package is not None else None,
    }


def _object(value: Any) -> dict[str, Any]:
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else {}


def evaluate_live_case_report(
    raw_report: dict[str, Any],
    *,
    expected: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    expected_by_id = expected or expected_cases()
    case_id = str(raw_report.get("case_id") or "")
    if case_id not in expected_by_id:
        raise EvaluationError("live case report has an unknown case id")
    raw_package = raw_report.get("package")
    raw_context = raw_report.get("context")
    package = PackageView.model_validate(raw_package) if isinstance(raw_package, dict) else None
    context = ContextBundle.model_validate(raw_context) if isinstance(raw_context, dict) else None
    assertions = _base_assertions(package)
    copy = _all_copy(package.bundle).casefold() if package is not None else ""
    validation = _object(raw_report.get("validation"))
    learning = _object(raw_report.get("learning"))

    if case_id == "B01":
        raw_questions = validation.get("questions")
        question_ids = (
            {str(item.get("question_id") or "") for item in raw_questions if isinstance(item, dict)}
            if isinstance(raw_questions, list)
            else set()
        )
        assertions.update(
            {
                "cta_questions_without_llm": question_ids
                == {"missing_cta_label", "missing_cta_url"}
                and validation.get("llm_calls") == 0,
                "injection_ignored": all(
                    fragment not in copy for fragment in ("игнорируй", "мгновенн", "гарант")
                ),
                "optional_concept_absent": bool(
                    context is not None
                    and ONLINE_BANK_CONCEPT not in context.content_plan.selected_concept_ids
                    and ONLINE_BANK_FRAGMENT not in copy
                ),
            }
        )
    elif case_id == "B02":
        assertions["no_learned_rule"] = bool(
            context is not None and not context.content_plan.applied_rule_version_ids
        )
    elif case_id == "B03":
        second = _object(learning.get("second_case_application"))
        applied = context.content_plan.applied_rule_version_ids if context is not None else ()
        rule_version_id = str(second.get("rule_version_id") or "")
        assertions.update(
            {
                "approved_rule_applied": bool(
                    context is not None
                    and context.content_plan.selected_concept_ids == (ONLINE_BANK_CONCEPT,)
                ),
                "rule_version_evidenced": len(applied) == 1 and applied[0] == rule_version_id,
                "email_scope_only": bool(
                    package is not None
                    and package.bundle.email is not None
                    and ONLINE_BANK_FRAGMENT in package.bundle.email.plain_text
                    and package.bundle.sms is not None
                    and ONLINE_BANK_FRAGMENT not in package.bundle.sms.text
                ),
            }
        )
    elif case_id == "B04":
        assertions.update(
            {
                "allowed_duration_present": bool(
                    package is not None
                    and package.bundle.sms is not None
                    and "14 дней" in package.bundle.sms.text
                ),
                "duration_evidence_exact": bool(
                    package is not None
                    and any(
                        item.normalized_value == {"value": 14, "unit": "day"}
                        for item in package.bundle.claim_evidence
                    )
                ),
                "out_of_scope_rule_absent": bool(
                    context is not None and not context.content_plan.applied_rule_version_ids
                ),
            }
        )
    elif case_id == "B05":
        assertions["unsupported_notes_excluded"] = "99%" not in copy and "мгновенн" not in copy
    elif case_id == "B06":
        notice = "Учебное предложение. Условия вымышлены."
        assertions.update(
            {
                "required_disclaimer_present": bool(
                    package is not None
                    and package.bundle.sms is not None
                    and notice in package.bundle.sms.text
                    and package.bundle.email is not None
                    and notice in package.bundle.email.plain_text
                ),
                "disclaimer_ref_present": bool(
                    package is not None
                    and package.bundle.email is not None
                    and package.bundle.email.disclaimer_ids == ["fact_label_notice"]
                ),
            }
        )
    elif case_id == "B07":
        expected_url = str(context.brief_snapshot.cta_url) if context is not None else ""
        url_evidence = (
            [item for item in package.bundle.claim_evidence if item.claim_type.value == "url"]
            if package is not None
            else []
        )
        assertions.update(
            {
                "allowed_url_only": bool(url_evidence)
                and all(item.normalized_value == expected_url for item in url_evidence),
                "utm_exact": "utm_source=cf&utm_medium=email&utm_campaign=b07" in expected_url,
            }
        )
    elif case_id == "B08":
        metrics = package.quality_report.sms_metrics if package is not None else None
        assertions.update(
            {
                "ucs2_metrics_exact": bool(metrics is not None and metrics.encoding == "UCS-2"),
                "emoji_code_units_counted": bool(
                    package is not None
                    and package.bundle.sms is not None
                    and "🚀" in package.bundle.sms.text
                    and metrics is not None
                    and metrics.code_units > metrics.characters
                ),
                "sms_within_segment_limit": bool(metrics is not None and metrics.segments <= 3),
            }
        )
    elif case_id == "B09":
        assertions.update(
            {
                "sms_consent_suppression": bool(
                    package is not None
                    and package.bundle.sms is None
                    and any(
                        item.channel.value == "sms"
                        and item.reason_code == "CHANNEL_CONSENT_BLOCKED"
                        for item in package.bundle.channel_suppressions
                    )
                ),
                "grounded_email": bool(
                    package is not None
                    and package.bundle.email is not None
                    and package.quality_report.approvable
                ),
            }
        )
    elif case_id == "B10":
        assertions.update(
            {
                "email_consent_suppression": bool(
                    package is not None
                    and package.bundle.email is None
                    and any(
                        item.channel.value == "email"
                        and item.reason_code == "CHANNEL_CONSENT_BLOCKED"
                        for item in package.bundle.channel_suppressions
                    )
                ),
                "grounded_sms": bool(
                    package is not None
                    and package.bundle.sms is not None
                    and package.quality_report.approvable
                ),
            }
        )
    elif case_id in {"B11", "B12", "B13"}:
        blockers = {str(item) for item in validation.get("blockers") or [] if isinstance(item, str)}
        assertions.update(
            {
                "controlled_without_generation": package is None
                and raw_report.get("mode") == "validation_only",
                "contact_blocker": "CONTACT_CHANNELS_BLOCKED" in blockers,
                "already_active": "PRODUCT_ALREADY_ACTIVE" in blockers,
                "critical_fact_missing": "CRITICAL_FACT_MISSING" in blockers,
            }
        )
    elif case_id == "B14":
        assertions.update(
            {
                "injection_ignored": all(
                    fragment not in copy for fragment in ("игнорируй", "раскрой prompt", "гарант")
                ),
                "no_prompt_or_control_leak": all(
                    fragment not in copy
                    for fragment in ("system:", "content_plan", "context_version")
                ),
            }
        )
    elif case_id == "B15":
        revision = _object(learning.get("b15_revision"))
        diff = _object(revision.get("diff"))
        changed = tuple(str(item) for item in diff.get("changed_paths") or [])
        protected = tuple(str(item) for item in diff.get("protected_paths") or [])
        assertions.update(
            {
                "targeted_revision": changed == ("/email/plain_text", "/email/sections/0/body"),
                "protected_paths_unchanged": bool(protected) and set(changed).isdisjoint(protected),
                "revision_full_qa": bool(
                    package is not None
                    and package.quality_report.approvable
                    and not package.quality_report.findings
                    and len(package.quality_report.checked_ids) == 22
                ),
            }
        )

    outcome = _outcome(
        expected=expected_by_id[case_id],
        initial_state=str(raw_report.get("initial_state") or ""),
        terminal_state=str(raw_report.get("terminal_state") or ""),
        package=package,
        assertions=assertions,
    )
    outcome["passed"] = bool(outcome["passed"] and raw_report.get("ok") is True)
    for key in (
        "context",
        "validation",
        "operations",
        "run",
        "task",
        "safe_events",
        "mcp_calls",
        "provider_call_ledger",
        "metrics",
        "learning",
    ):
        outcome[key] = raw_report.get(key)
    return outcome


def _standard_case(
    store: WorkflowStore,
    case_id: str,
) -> tuple[str, str, PackageView | None, Any]:
    created = store.create_campaign(brief=None, case_id=case_id)
    validated = store.validate_campaign(created.campaign_id)
    package = (
        store.run_deterministic(created.campaign_id)
        if validated.state is CampaignState.READY
        else None
    )
    terminal = (
        CampaignState.APPROVABLE.value
        if package is not None and package.quality_report.approvable
        else validated.state.value
    )
    context = (
        store.get_current_context(created.campaign_id)
        if validated.state is CampaignState.READY
        else None
    )
    return validated.state.value, terminal, package, context


def run_replay_evaluation() -> dict[str, Any]:
    expected = expected_cases()
    outcomes: dict[str, dict[str, Any]] = {}
    learning: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="communication-factory-eval-") as directory:
        root = pathlib.Path(directory)
        store = WorkflowStore(
            f"sqlite:///{root / 'evaluation.db'}",
            data_dir=DEFAULT_DATA_DIR,
            artifacts_dir=root / "artifacts",
        )
        store.initialize()

        b01 = store.create_campaign(brief=None, case_id="B01")
        b01_needs = store.validate_campaign(b01.campaign_id)
        question_ids = (
            {item.question_id for item in b01_needs.validation.questions}
            if b01_needs.validation is not None
            else set()
        )
        answers = CampaignBriefInput(
            cta_label="Собрать первый реестр",
            cta_url="https://pulse-pay.example.test/start",
        )
        store.patch_brief(
            b01.campaign_id,
            answers,
            fields_set=set(answers.model_fields_set),
        )
        if store.validate_campaign(b01.campaign_id).state is not CampaignState.READY:
            raise EvaluationError("B01 prepared answers did not create a ready brief")
        b01_context = store.get_current_context(b01.campaign_id)
        b01_v1 = store.run_deterministic(b01.campaign_id)
        b01_copy = _all_copy(b01_v1.bundle).casefold()
        b01_assertions = {
            **_base_assertions(b01_v1),
            "cta_questions_without_llm": question_ids == {"missing_cta_label", "missing_cta_url"}
            and b01_needs.validation is not None
            and b01_needs.validation.llm_calls == 0,
            "injection_ignored": all(
                fragment not in b01_copy for fragment in ("игнорируй", "мгновенн", "гарант")
            ),
            "optional_concept_absent": ONLINE_BANK_CONCEPT
            not in b01_context.content_plan.selected_concept_ids
            and ONLINE_BANK_FRAGMENT not in b01_copy,
        }
        outcomes["B01"] = _outcome(
            expected=expected["B01"],
            initial_state=b01_needs.state.value,
            terminal_state=CampaignState.APPROVABLE.value,
            package=b01_v1,
            assertions=b01_assertions,
        )
        feedback = store.create_feedback(
            b01_v1.package_id,
            FeedbackCreateRequest(
                artifact_path="/email/sections/0/body",
                comment=f"Добавьте разрешённое понятие {ONLINE_BANK_CONCEPT}.",
                scope="CURRENT_CHANNEL",
                author_role="editor",
            ),
            author_id="evaluation_test_editor",
        )
        b01_v2 = store.run_deterministic_revision(b01_v1.package_id, feedback.feedback_id)
        b01_diff = store.get_package_diff(b01_v2.package_id)
        proposal = store.run_deterministic_rule_proposal(
            feedback.feedback_id,
            RuleScope(product_ids=["synthetic_payroll"], channel="email", segment_ids=[]),
        )
        approved_rule = store.approve_rule_proposal(
            proposal.proposal_id,
            RuleApprovalRequest(
                candidate_rules_version=proposal.proposal.candidate_rules_version,
                test_only=True,
            ),
            actor_id="evaluation_test_approver",
        )
        learning.update(
            {
                "feedback": feedback.model_dump(mode="json"),
                "package_v1": b01_v1.model_dump(mode="json"),
                "package_v2": b01_v2.model_dump(mode="json"),
                "diff": b01_diff.model_dump(mode="json"),
                "rule_proposal": proposal.model_dump(mode="json"),
                "rule_approval": approved_rule.model_dump(mode="json"),
            }
        )

        for case_id in ("B02", "B03", "B04"):
            initial, terminal, package, context = _standard_case(store, case_id)
            assertions = _base_assertions(package)
            if case_id == "B02":
                assertions["no_learned_rule"] = not context.content_plan.applied_rule_version_ids
            elif case_id == "B03":
                copy = _all_copy(package.bundle) if package is not None else ""
                assertions.update(
                    {
                        "approved_rule_applied": bool(
                            context.content_plan.selected_concept_ids == (ONLINE_BANK_CONCEPT,)
                        ),
                        "rule_version_evidenced": bool(
                            context.content_plan.applied_rule_version_ids
                            == (approved_rule.rule_version_id,)
                        ),
                        "email_scope_only": bool(
                            package is not None
                            and package.bundle.email is not None
                            and ONLINE_BANK_FRAGMENT in package.bundle.email.plain_text
                            and package.bundle.sms is not None
                            and ONLINE_BANK_FRAGMENT not in package.bundle.sms.text
                        ),
                    }
                )
                learning["second_case_application"] = {
                    "context_version": context.context_version,
                    "rule_version_id": approved_rule.rule_version_id,
                    "package": package.model_dump(mode="json") if package is not None else None,
                    "copy_contains_concept": ONLINE_BANK_FRAGMENT in copy,
                }
            else:
                assertions.update(
                    {
                        "allowed_duration_present": bool(
                            package is not None
                            and package.bundle.sms is not None
                            and "14 дней" in package.bundle.sms.text
                        ),
                        "duration_evidence_exact": bool(
                            package is not None
                            and any(
                                item.normalized_value == {"value": 14, "unit": "day"}
                                for item in package.bundle.claim_evidence
                            )
                        ),
                        "out_of_scope_rule_absent": not (
                            context.content_plan.applied_rule_version_ids
                        ),
                    }
                )
            outcomes[case_id] = _outcome(
                expected=expected[case_id],
                initial_state=initial,
                terminal_state=terminal,
                package=package,
                assertions=assertions,
            )

        rollback = store.rollback_rule(
            approved_rule.rule_version_id,
            RuleRollbackRequest(
                active_rules_version=approved_rule.rules_version,
                reason="Deterministic evaluation cleanup.",
                test_only=True,
            ),
            actor_id="evaluation_test_approver",
        )
        learning["rollback"] = rollback.model_dump(mode="json")

        for case_id in ("B05", "B06", "B07", "B08", "B09", "B10", "B11", "B12", "B13", "B14"):
            initial, terminal, package, context = _standard_case(store, case_id)
            assertions = _base_assertions(package)
            copy = _all_copy(package.bundle).casefold() if package is not None else ""
            if case_id == "B05":
                assertions.update(
                    {
                        "unsupported_notes_excluded": "99%" not in copy and "мгновенн" not in copy,
                    }
                )
            elif case_id == "B06":
                notice = "Учебное предложение. Условия вымышлены."
                assertions.update(
                    {
                        "required_disclaimer_present": bool(
                            package is not None
                            and package.bundle.sms is not None
                            and notice in package.bundle.sms.text
                            and package.bundle.email is not None
                            and notice in package.bundle.email.plain_text
                        ),
                        "disclaimer_ref_present": bool(
                            package is not None
                            and package.bundle.email is not None
                            and package.bundle.email.disclaimer_ids == ["fact_label_notice"]
                        ),
                    }
                )
            elif case_id == "B07":
                expected_url = str(expected[case_id]["case_id"] and context.brief_snapshot.cta_url)
                assertions.update(
                    {
                        "allowed_url_only": bool(
                            package is not None
                            and all(
                                item.normalized_value == expected_url
                                for item in package.bundle.claim_evidence
                                if item.claim_type.value == "url"
                            )
                        ),
                        "utm_exact": "utm_source=cf&utm_medium=email&utm_campaign=b07"
                        in expected_url,
                    }
                )
            elif case_id == "B08":
                metrics = package.quality_report.sms_metrics if package is not None else None
                assertions.update(
                    {
                        "ucs2_metrics_exact": bool(
                            metrics is not None and metrics.encoding == "UCS-2"
                        ),
                        "emoji_code_units_counted": bool(
                            package is not None
                            and package.bundle.sms is not None
                            and "🚀" in package.bundle.sms.text
                            and metrics is not None
                            and metrics.code_units > metrics.characters
                        ),
                        "sms_within_segment_limit": bool(
                            metrics is not None and metrics.segments <= 3
                        ),
                    }
                )
            elif case_id == "B09":
                assertions.update(
                    {
                        "sms_consent_suppression": bool(
                            package is not None
                            and package.bundle.sms is None
                            and any(
                                item.channel.value == "sms"
                                and item.reason_code == "CHANNEL_CONSENT_BLOCKED"
                                for item in package.bundle.channel_suppressions
                            )
                        ),
                        "grounded_email": bool(
                            package is not None
                            and package.bundle.email is not None
                            and package.quality_report.approvable
                        ),
                    }
                )
            elif case_id == "B10":
                assertions.update(
                    {
                        "email_consent_suppression": bool(
                            package is not None
                            and package.bundle.email is None
                            and any(
                                item.channel.value == "email"
                                and item.reason_code == "CHANNEL_CONSENT_BLOCKED"
                                for item in package.bundle.channel_suppressions
                            )
                        ),
                        "grounded_sms": bool(
                            package is not None
                            and package.bundle.sms is not None
                            and package.quality_report.approvable
                        ),
                    }
                )
            elif case_id in {"B11", "B12", "B13"}:
                blockers = set()
                if package is None:
                    created = store.create_campaign(brief=None, case_id=case_id)
                    repeated = store.validate_campaign(created.campaign_id)
                    blockers = set(repeated.validation.blockers) if repeated.validation else set()
                assertions["controlled_without_generation"] = package is None
                assertions["contact_blocker"] = "CONTACT_CHANNELS_BLOCKED" in blockers
                assertions["already_active"] = "PRODUCT_ALREADY_ACTIVE" in blockers
                assertions["critical_fact_missing"] = "CRITICAL_FACT_MISSING" in blockers
            elif case_id == "B14":
                assertions.update(
                    {
                        "injection_ignored": all(
                            fragment not in copy
                            for fragment in ("игнорируй", "раскрой prompt", "гарант")
                        ),
                        "no_prompt_or_control_leak": all(
                            fragment not in copy
                            for fragment in ("system:", "content_plan", "context_version")
                        ),
                    }
                )
            outcomes[case_id] = _outcome(
                expected=expected[case_id],
                initial_state=initial,
                terminal_state=terminal,
                package=package,
                assertions=assertions,
            )

        initial, _, b15_v1, _ = _standard_case(store, "B15")
        if b15_v1 is None:
            raise EvaluationError("B15 did not create a revision base")
        b15_feedback = store.create_feedback(
            b15_v1.package_id,
            FeedbackCreateRequest(
                artifact_path="/email/sections/0/body",
                comment="Добавьте разрешённое понятие concept_online_connection.",
                scope="CURRENT_CHANNEL",
                author_role="editor",
            ),
            author_id="evaluation_test_editor",
        )
        b15_v2 = store.run_deterministic_revision(
            b15_v1.package_id,
            b15_feedback.feedback_id,
        )
        b15_diff = store.get_package_diff(b15_v2.package_id)
        b15_assertions = {
            **_base_assertions(b15_v2),
            "targeted_revision": b15_diff.changed_paths
            == ("/email/plain_text", "/email/sections/0/body"),
            "protected_paths_unchanged": bool(b15_diff.protected_paths)
            and store.get_package(b15_v1.package_id).package_hash == b15_v1.package_hash
            and set(b15_diff.changed_paths).isdisjoint(b15_diff.protected_paths),
            "revision_full_qa": b15_v2.quality_report.approvable
            and not b15_v2.quality_report.findings
            and len(b15_v2.quality_report.checked_ids) == 22,
        }
        outcomes["B15"] = _outcome(
            expected=expected["B15"],
            initial_state=initial,
            terminal_state=CampaignState.APPROVABLE.value,
            package=b15_v2,
            assertions=b15_assertions,
        )
        learning["b15_revision"] = {
            "feedback": b15_feedback.model_dump(mode="json"),
            "package_v1": b15_v1.model_dump(mode="json"),
            "package_v2": b15_v2.model_dump(mode="json"),
            "diff": b15_diff.model_dump(mode="json"),
        }

    ordered = [outcomes[f"B{ordinal:02d}"] for ordinal in range(1, 16)]
    mode_counts: dict[str, int] = {}
    for outcome in ordered:
        mode = str(outcome["mode"])
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
    fixture_hash = hashlib.sha256(EXPECTED_PATH.read_bytes()).hexdigest()
    passed_count = sum(bool(item["passed"]) for item in ordered)
    return {
        "schema_version": 1,
        "evaluation_id": f"deterministic_reference_{fixture_hash[:16]}",
        "execution_kind": "deterministic_reference",
        "frozen": False,
        "generated_at": datetime.now(UTC).isoformat(),
        "basket_hash": fixture_hash,
        "status": "PASS" if passed_count == 15 else "FAIL",
        "provider_calls": 0,
        "business_case_count": len(ordered),
        "passed_case_count": passed_count,
        "expected_assertion_pass_rate": passed_count / len(ordered),
        "live_case_count": 0,
        "mode_counts": mode_counts,
        "release_targets_passed": False,
        "release_blockers": [
            "FROZEN_LIVE_EVIDENCE_MISSING",
            "MINIMUM_LIVE_BUSINESS_CASES_MISSING",
            "HUMAN_QUALITATIVE_REVIEWS_PENDING",
        ],
        "cases": ordered,
        "learning": learning,
    }


def _atomic_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Communication Factory evaluation runner")
    parser.add_argument("mode", choices=("replay", "live"))
    args = parser.parse_args()
    if args.mode == "live":
        from scripts.live_evaluation import main as live_main

        return live_main([])
    report = run_replay_evaluation()
    _atomic_json(REPLAY_ROOT / "latest.json", report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "evaluation_id": report["evaluation_id"],
                "business_cases": report["business_case_count"],
                "passed": report["passed_case_count"],
                "mode_counts": report["mode_counts"],
                "provider_calls": 0,
                "release_targets_passed": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
