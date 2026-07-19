from __future__ import annotations

import json
import pathlib
import tempfile

from apps.api.app.domain.campaigns import CampaignBriefInput
from apps.api.app.domain.learning import (
    FeedbackCreateRequest,
    RuleApprovalRequest,
    RuleRollbackRequest,
)
from apps.api.app.domain.models import RuleScope
from apps.api.app.domain.workflow import CampaignState, PackageView
from apps.api.app.services.catalog import DEFAULT_DATA_DIR
from apps.api.app.workflow.store import WorkflowStore

CONCEPT_ID = "payouts_via_online_bank"
CONCEPT_FRAGMENT = "подготовка выплат в онлайн-банке"


def _all_copy(package: PackageView) -> str:
    bundle = package.bundle
    return "\n".join(
        (
            bundle.sms.text if bundle.sms else "",
            bundle.email.plain_text if bundle.email else "",
        )
    )


def run_gate() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="communication-factory-gate3-") as directory:
        root = pathlib.Path(directory)
        store = WorkflowStore(
            f"sqlite:///{root / 'gate3.db'}",
            data_dir=DEFAULT_DATA_DIR,
            artifacts_dir=root / "artifacts",
        )
        store.initialize()

        campaign = store.create_campaign(brief=None, case_id="B01")
        needs_input = store.validate_campaign(campaign.campaign_id)
        if needs_input.state is not CampaignState.NEEDS_INPUT or needs_input.validation is None:
            raise RuntimeError("Gate 3 B01 did not start with deterministic NEEDS_INPUT")
        if {item.question_id for item in needs_input.validation.questions} != {
            "missing_cta_label",
            "missing_cta_url",
        }:
            raise RuntimeError("Gate 3 B01 CTA questions drifted")
        answers = CampaignBriefInput(
            cta_label="Собрать первый реестр",
            cta_url="https://pulse-pay.example.test/start",
        )
        store.patch_brief(
            campaign.campaign_id,
            answers,
            fields_set=set(answers.model_fields_set),
        )
        ready = store.validate_campaign(campaign.campaign_id)
        if ready.state is not CampaignState.READY:
            raise RuntimeError("Gate 3 B01 prepared answers did not create a ready snapshot")
        initial_context = store.get_current_context(campaign.campaign_id)
        if CONCEPT_ID in initial_context.content_plan.selected_concept_ids:
            raise RuntimeError("Gate 3 B01 optional concept leaked into the initial plan")
        v1 = store.run_deterministic(campaign.campaign_id)
        initial_copy = _all_copy(v1)
        if (
            CONCEPT_FRAGMENT in initial_copy
            or "мгновенн" in initial_copy
            or "гарант" in initial_copy
        ):
            raise RuntimeError("Gate 3 B01 initial output used optional/injected content")

        feedback = store.create_feedback(
            v1.package_id,
            FeedbackCreateRequest(
                artifact_path="/email/sections/0/body",
                comment=f"Добавьте разрешённое понятие {CONCEPT_ID}.",
                scope="CURRENT_CHANNEL",
                author_role="editor",
            ),
            author_id="gate3_test_editor",
        )
        v2 = store.run_deterministic_revision(v1.package_id, feedback.feedback_id)
        diff = store.get_package_diff(v2.package_id)
        if diff.changed_paths != (
            "/email/plain_text",
            "/email/sections/0/body",
        ):
            raise RuntimeError("Gate 3 revision changed an undeclared or unexpected path")
        if not v2.quality_report.approvable or v2.quality_report.findings:
            raise RuntimeError("Gate 3 revised package did not pass full QA")
        if CONCEPT_FRAGMENT not in _all_copy(v2):
            raise RuntimeError("Gate 3 feedback concept is absent from v2")
        if store.get_package(v1.package_id).package_hash != v1.package_hash:
            raise RuntimeError("Gate 3 revision mutated immutable v1")

        proposal = store.run_deterministic_rule_proposal(
            feedback.feedback_id,
            RuleScope(
                product_ids=["synthetic_payroll"],
                channel="email",
                segment_ids=[],
            ),
        )
        if proposal.status.value != "READY_FOR_APPROVAL" or not proposal.tests:
            raise RuntimeError("Gate 3 rule proposal did not reach tested approval state")
        if any(not result.passed for result in proposal.tests):
            raise RuntimeError("Gate 3 rule target/regression/out-of-scope test failed")
        test_kinds = {result.test_kind for result in proposal.tests}
        if test_kinds != {"target", "regression", "out_of_scope"}:
            raise RuntimeError("Gate 3 rule test matrix is incomplete")
        approved = store.approve_rule_proposal(
            proposal.proposal_id,
            RuleApprovalRequest(
                candidate_rules_version=proposal.proposal.candidate_rules_version,
                test_only=True,
            ),
            actor_id="gate3_test_approver",
        )
        if not approved.active or not approved.test_only:
            raise RuntimeError("Gate 3 test-only human rule approval was not recorded")

        matching = store.create_campaign(brief=None, case_id="B03")
        matching = store.validate_campaign(matching.campaign_id)
        matching_context = store.get_current_context(matching.campaign_id)
        if matching_context.content_plan.selected_concept_ids != (CONCEPT_ID,):
            raise RuntimeError("Gate 3 B03 did not select the learned concept")
        if matching_context.content_plan.applied_rule_version_ids != (approved.rule_version_id,):
            raise RuntimeError("Gate 3 B03 does not evidence the applied rule version")
        matching_package = store.run_deterministic(matching.campaign_id)
        if (
            not matching_package.quality_report.approvable
            or matching_package.bundle.email is None
            or CONCEPT_FRAGMENT not in matching_package.bundle.email.plain_text
            or matching_package.bundle.sms is None
            or CONCEPT_FRAGMENT in matching_package.bundle.sms.text
        ):
            raise RuntimeError("Gate 3 B03 rule scope/application is invalid")

        negative = store.create_campaign(brief=None, case_id="B04")
        store.validate_campaign(negative.campaign_id)
        negative_context = store.get_current_context(negative.campaign_id)
        if negative_context.content_plan.applied_rule_version_ids:
            raise RuntimeError("Gate 3 rule leaked to an out-of-scope product")

        rollback = store.rollback_rule(
            approved.rule_version_id,
            RuleRollbackRequest(
                active_rules_version=approved.rules_version,
                reason="Детерминированная проверка rollback Gate 3.",
                test_only=True,
            ),
            actor_id="gate3_test_approver",
        )
        if rollback.status.value != "ROLLED_BACK" or rollback.active:
            raise RuntimeError("Gate 3 rollback did not create an immutable inactive event")
        after = store.create_campaign(brief=None, case_id="B03")
        store.validate_campaign(after.campaign_id)
        after_context = store.get_current_context(after.campaign_id)
        if after_context.content_plan.applied_rule_version_ids:
            raise RuntimeError("Gate 3 rolled-back rule still applies to a future case")

        return {
            "status": "PASS",
            "gate": 3,
            "provider_calls": 0,
            "b01_initial_optional_concept_absent": True,
            "revision_package_version": v2.package_version,
            "revision_changed_paths": list(diff.changed_paths),
            "rule_tests": len(proposal.tests),
            "b03_rule_version_evidenced": True,
            "out_of_scope_negative_passed": True,
            "rollback_passed": True,
            "test_only_approval": True,
        }


def main() -> int:
    print(json.dumps(run_gate(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
