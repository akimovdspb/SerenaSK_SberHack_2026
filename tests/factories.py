from __future__ import annotations

from typing import Any

from apps.api.app.workflow.store import WorkflowStore


def communication_bundle_envelope(
    context_version: str,
    *,
    summary: str = "Проверка",
    campaign_id: str = "cmp_contract_probe",
) -> dict[str, Any]:
    return {
        "kind": "communication_bundle",
        "schema_version": "1.0",
        "campaign_id": campaign_id,
        "operation": "initial",
        "iteration": 1,
        "context_version": context_version,
        "payload": {
            "summary": summary,
            "personalization_rationale": ["Использован только синтетический сценарий."],
            "sms": {
                "text": "Проверочный текст. Подробнее: https://offers.example.test/probe",
                "cta_url": "https://offers.example.test/probe",
                "fact_refs": [],
                "personalization_refs": [],
            },
            "email": {
                "subject": "Проверка фабрики",
                "preheader": "Синтетический сценарий без отправки",
                "headline": "Проверочный черновик",
                "sections": [
                    {
                        "section_id": "section_intro",
                        "kind": "intro",
                        "heading": "",
                        "body": "Это синтетический черновик для проверки контракта.",
                        "fact_refs": [],
                        "personalization_refs": [],
                    }
                ],
                "cta_label": "Открыть пример",
                "cta_url": "https://offers.example.test/probe",
                "disclaimer_ids": [],
                "plain_text": "Это синтетический черновик для проверки контракта.",
                "fact_refs": [],
                "personalization_refs": [],
            },
            "channel_suppressions": [],
            "claim_evidence": [],
            "warnings": [],
        },
    }


def deterministic_live_operation_adapter() -> Any:
    sequence = 0

    def execute(workflow: WorkflowStore, campaign_id: str) -> dict[str, Any]:
        nonlocal sequence
        sequence += 1
        context = workflow.get_current_context(campaign_id)
        workflow.run_current_deterministic_operation(campaign_id)
        workspace = workflow.workspace(campaign_id)
        package = workspace.package.model_dump(mode="json") if workspace.package else None
        if package is not None:
            package["mode"] = "live_ouroboros"
        proposal = (
            workspace.rule_proposals[-1].model_dump(mode="json")
            if workspace.rule_proposals
            else None
        )
        run_id = f"run_test_live_transport_{sequence:02d}"
        usage = {
            "main_generation": {
                "calls": 1,
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cached_tokens": 5,
                "cache_write_tokens": 0,
                "cost_usd": 0.001,
                "models": ["gpt-5.4-mini"],
                "providers": ["openai"],
            }
        }
        return {
            "schema_version": 1,
            "ok": True,
            "operation": context.operation.value,
            "context": context.model_dump(mode="json"),
            "run": {
                "run_id": run_id,
                "status": "COMPLETED",
                "mode": "live_ouroboros",
                "tool_receipts": [
                    "mcp_factory__cf_context_get",
                    "mcp_factory__cf_draft_save",
                ],
                "physical_attempt_count": 1,
            },
            "task": {"task_id": f"task_{sequence:02d}", "status": "completed"},
            "package": package,
            "rule_proposal": proposal,
            "provider_call_ledger": {},
            "usage_by_category": usage,
            "safe_events": [],
            "mcp_calls": [],
            "checks": {
                "usage_complete": True,
                "worker_released": True,
            },
            "runtime_budget": {
                "spent_usd": 0.001 * sequence,
                "limit_usd": 20.0,
                "remaining_usd": 20.0 - 0.001 * sequence,
            },
            "latency_ms": {
                "user_visible_terminal": 1_000,
                "full_worker_occupancy": 1_200,
            },
        }

    return execute
