from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

import yaml

from apps.api.app.domain.models import Channel, Operation, RuleType
from apps.api.app.live_probe_transport import (
    LEDGER_CATEGORIES,
    RUN_TERMINAL_DEADLINE_SECONDS,
    TASK_DEADLINE_SECONDS,
)
from apps.api.app.ouroboros_client import ALLOWED_PROVIDER_TOOLS, CONTRACT_MARKER
from scripts.compose_contract import load_rendered_compose, validate_compose

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def validate_spec_constants(
    constants: dict[str, Any],
    runtime_lock: dict[str, Any],
    compose: dict[str, Any],
) -> list[str]:
    errors = validate_compose(compose)
    ouroboros = _dict(constants.get("ouroboros"))
    lock_tag = str(runtime_lock.get("tag") or "")
    if lock_tag != ouroboros.get("baseline_tag"):
        errors.append("pinned Ouroboros tag drifted from spec_constants")
    if runtime_lock.get("commit") != ouroboros.get("baseline_sha"):
        errors.append("pinned Ouroboros commit drifted from spec_constants")
    activation = _dict(constants.get("skill_activation"))
    if runtime_lock.get("activation_mode") != activation.get("canonical_mode"):
        errors.append("instruction activation mode drifted from spec_constants")
    if activation.get("contract_marker") != CONTRACT_MARKER:
        errors.append("instruction contract marker drifted from spec_constants")

    tool_isolation = _dict(constants.get("tool_isolation"))
    if list(tool_isolation.get("allowed_effective_tool_names") or []) != ALLOWED_PROVIDER_TOOLS:
        errors.append("allowed provider tool set drifted from spec_constants")
    mcp = _dict(constants.get("mcp"))
    if [f"mcp_factory__{name}" for name in mcp.get("tools") or []] != ALLOWED_PROVIDER_TOOLS:
        errors.append("MCP tool projection drifted from spec_constants")

    post_task = _dict(constants.get("post_task"))
    if tuple(post_task.get("provider_call_categories") or []) != LEDGER_CATEGORIES:
        errors.append("provider call ledger categories drifted from spec_constants")
    execution = _dict(constants.get("execution"))
    if int(ouroboros.get("task_deadline_seconds_initial") or 0) != TASK_DEADLINE_SECONDS:
        errors.append("managed task deadline drifted from spec_constants")
    if int(execution.get("terminal_deadline_seconds") or 0) != RUN_TERMINAL_DEADLINE_SECONDS:
        errors.append("terminal deadline drifted from spec_constants")
    if execution.get("backend_llm_allowed") is not False:
        errors.append("backend LLM prohibition is absent from spec_constants")

    scope = _dict(constants.get("scope"))
    if list(scope.get("p0_channels") or []) != [item.value for item in Channel]:
        errors.append("P0 channel enum drifted from spec_constants")
    if list(scope.get("p0_rule_types") or []) != [item.value for item in RuleType]:
        errors.append("P0 rule-type enum drifted from spec_constants")
    if [item.value for item in Operation] != ["initial", "revision", "rule_proposal"]:
        errors.append("operation enum is not the canonical closed set")

    services = _dict(compose.get("services"))
    runtime_service = _dict(services.get("ouroboros"))
    environment = _dict(runtime_service.get("environment"))
    expected_environment = {
        "OUROBOROS_RUNTIME_MODE": ouroboros.get("runtime_mode"),
        "OUROBOROS_SAFETY_MODE": ouroboros.get("safety_mode"),
        "OUROBOROS_TASK_REVIEW_MODE": ouroboros.get("task_review_mode"),
        "OUROBOROS_ACCEPTANCE_MAX_IMPROVEMENT_PASSES": str(
            ouroboros.get("acceptance_max_improvement_passes")
        ),
        "OUROBOROS_POST_TASK_EVOLUTION": str(
            bool(ouroboros.get("post_task_evolution_enabled"))
        ).lower(),
        "OUROBOROS_TOOL_TIMEOUT_SEC": str(ouroboros.get("global_tool_timeout_seconds")),
        "OUROBOROS_FINALIZATION_GRACE_SEC": str(
            ouroboros.get("finalization_grace_seconds_initial")
        ),
    }
    for key, expected in expected_environment.items():
        if str(environment.get(key)) != str(expected):
            errors.append(f"runtime environment {key} drifted from spec_constants")
    if str(environment.get("OUROBOROS_MODEL_FALLBACKS") or ""):
        errors.append("automatic runtime model fallback is enabled")
    if int(mcp.get("tool_timeout_seconds") or 0) > TASK_DEADLINE_SECONDS:
        errors.append("MCP timeout exceeds the managed task deadline")
    return errors


def main() -> int:
    try:
        constants = yaml.safe_load((ROOT / "spec_constants.yaml").read_text(encoding="utf-8"))
        runtime_lock = json.loads((ROOT / "ouroboros" / "ouroboros.lock").read_text())
        compose = load_rendered_compose()
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"spec-drift: FAIL: {exc}", file=sys.stderr)
        return 1
    if not isinstance(constants, dict) or not isinstance(runtime_lock, dict):
        print("spec-drift: FAIL: canonical inputs are invalid", file=sys.stderr)
        return 1
    errors = validate_spec_constants(constants, runtime_lock, compose)
    if errors:
        for error in errors:
            print(f"spec-drift: FAIL: {error}", file=sys.stderr)
        return 1
    print("spec-drift: PASS schema=5 runtime=v6.61.4 task_deadline=25 terminal_deadline=29 tools=2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
