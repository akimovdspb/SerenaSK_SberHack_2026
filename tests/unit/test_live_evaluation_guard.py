from __future__ import annotations

import copy
import hashlib
import json
import pathlib
from typing import Any

import pytest

from scripts import live_evaluation
from scripts.live_evaluation import LiveEvaluationError


def _strict_contract() -> dict[str, Any]:
    return {
        "tools": {
            "strict_adapter": {
                "active": True,
                "decision_id": "CF-RP-001",
                "adapter_hash": "a" * 64,
            },
            "strict_provider_schemas": {
                name: {
                    "strict": True,
                    "supported_subset": True,
                    "schema_hash": character * 64,
                }
                for name, character in zip(
                    live_evaluation.ALLOWED_TOOLS,
                    ("b", "c"),
                    strict=True,
                )
            },
        }
    }


def test_live_request_requires_exact_opt_in_profile_caps_and_concurrency() -> None:
    environment = {
        "ALLOW_LIVE_EVAL": "true",
        "EVALUATION_ID": "full-live-01",
        "EVAL_PROVIDER_PROFILE": "openai-gpt-5.4-mini",
        "EVAL_MAX_TOKENS": "10000",
        "EVAL_MAX_COST_USD": "1.25",
        "EVAL_CONCURRENCY": "1",
    }

    request = live_evaluation.requested_run(environment)

    assert request.run_id == "full-live-01"
    assert request.provider == "openai"
    assert request.model == "gpt-5.4-mini"
    assert request.max_tokens == 10_000
    assert request.max_cost_usd == 1.25
    assert request.concurrency == 1

    for key in (
        "ALLOW_LIVE_EVAL",
        "EVALUATION_ID",
        "EVAL_PROVIDER_PROFILE",
        "EVAL_MAX_TOKENS",
        "EVAL_MAX_COST_USD",
        "EVAL_CONCURRENCY",
    ):
        invalid = dict(environment)
        invalid.pop(key)
        with pytest.raises(LiveEvaluationError):
            live_evaluation.requested_run(invalid)

    glm_environment = {
        **environment,
        "EVAL_PROVIDER_PROFILE": "openrouter-glm-5.2-functional",
    }
    glm_request = live_evaluation.requested_run(glm_environment)
    assert glm_request.provider == "openrouter"
    assert glm_request.model == "z-ai/glm-5.2"
    assert glm_request.openrouter_enabled is True
    assert "provider=openrouter model=z-ai/glm-5.2" in live_evaluation._preflight_summary(
        glm_request
    )


def test_live_evaluation_refuses_current_non_strict_runtime_contract() -> None:
    with pytest.raises(LiveEvaluationError, match="CF-RP-001"):
        live_evaluation.validate_strict_contract({"tools": {}})

    live_evaluation.validate_strict_contract(_strict_contract())

    invalid = _strict_contract()
    invalid["tools"]["strict_provider_schemas"]["mcp_factory__cf_draft_save"][
        "supported_subset"
    ] = False
    with pytest.raises(LiveEvaluationError, match="draft_save"):
        live_evaluation.validate_strict_contract(invalid)


def _write_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def test_readiness_requires_bound_warmup_smoke_and_distinct_pilots(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = tmp_path / "runs"
    reports = tmp_path / "reports"
    monkeypatch.setattr(live_evaluation, "ROOT", tmp_path)
    monkeypatch.setattr(live_evaluation, "RUN_STATE_DIR", runs)
    runtime_image_id = "sha256:" + "1" * 64

    def entry(run_id: str, case_id: str = "") -> dict[str, Any]:
        report = reports / f"{run_id}.json"
        source = {"ok": True, "checks": {"green": True}}
        if case_id:
            source.update({"evaluation_id": run_id, "case_id": case_id})
        else:
            source["run_id"] = run_id
        _write_json(report, source)
        report_hash = hashlib.sha256(report.read_bytes()).hexdigest()
        _write_json(
            runs / f"{run_id}.json",
            {
                "run_id": run_id,
                "kind": "gate2_live_campaign" if case_id else "gate0_live_probe",
                "case_id": case_id or None,
                "status": "completed",
                "usage_complete": True,
                "app_commit": "d" * 40,
                "runtime_image_id": runtime_image_id,
                "report_sha256": report_hash,
                "total_tokens": 80_000,
                "total_cost_usd": 0.04,
                "max_tokens": 100_000,
                "max_cost_usd": 0.08333333333333333,
            },
        )
        result = {
            "run_id": run_id,
            "report_path": report.relative_to(tmp_path).as_posix(),
            "report_sha256": report_hash,
            "status": "PASS",
            "usage_complete": True,
            "output_reviewed_by_codex": True,
            "total_tokens": 80_000,
            "total_cost_usd": 0.04,
            "max_tokens": 100_000,
            "max_cost_usd": 0.08333333333333333,
        }
        if case_id:
            result["case_id"] = case_id
        return result

    manifest = {
        "schema_version": 1,
        "status": "PASS",
        "app_commit": "d" * 40,
        "runtime_image_id": runtime_image_id,
        "runtime_contract_hash": "e" * 64,
        "basket_hash": "f" * 64,
        "warmup": {**entry("warmup-01"), "excluded_from_metrics": True},
        "smoke": entry("smoke-01", "B04"),
        "pilots": [entry("pilot-01", "B04"), entry("pilot-02", "B07")],
        "projection": {
            "paid_operation_count": 15,
            "projected_tokens": 1_800_000,
            "projected_cost_usd": 1.5,
            "safety_multiplier": 1.2,
            "per_operation_token_cap": 100_000,
            "per_operation_cost_cap_usd": 0.08333333333333333,
            "basis": "largest_selected_smoke_or_pilot_run_cap",
            "includes_maximum_output": True,
            "includes_configured_retries": True,
            "includes_safety_and_post_task": True,
        },
    }
    path = tmp_path / "readiness.json"
    _write_json(path, manifest)

    validated = live_evaluation.validate_readiness_manifest(
        path,
        commit="d" * 40,
        contract_hash="e" * 64,
        basket_hash="f" * 64,
        runtime_image_id=runtime_image_id,
    )

    assert validated["projection"]["paid_operation_count"] == 15

    malformed_rollover = copy.deepcopy(manifest)
    malformed_rollover["identity_rollover"] = "true"
    _write_json(path, malformed_rollover)
    with pytest.raises(LiveEvaluationError, match="rollover is malformed"):
        live_evaluation.validate_readiness_manifest(
            path,
            commit="d" * 40,
            contract_hash="e" * 64,
            basket_hash="f" * 64,
            runtime_image_id=runtime_image_id,
        )

    understated = copy.deepcopy(manifest)
    understated["projection"]["per_operation_token_cap"] = 90_000
    understated["projection"]["projected_tokens"] = 1_620_000
    _write_json(path, understated)
    with pytest.raises(LiveEvaluationError, match="projection"):
        live_evaluation.validate_readiness_manifest(
            path,
            commit="d" * 40,
            contract_hash="e" * 64,
            basket_hash="f" * 64,
            runtime_image_id=runtime_image_id,
        )

    manifest["pilots"][1]["case_id"] = "B04"
    _write_json(path, manifest)
    with pytest.raises(LiveEvaluationError, match="case identity"):
        live_evaluation.validate_readiness_manifest(
            path,
            commit="d" * 40,
            contract_hash="e" * 64,
            basket_hash="f" * 64,
            runtime_image_id=runtime_image_id,
        )


def test_retry_linkage_requires_new_id_reason_and_preserved_failed_attempt(
    tmp_path: pathlib.Path,
) -> None:
    runs = tmp_path / "runs"
    live = tmp_path / "live"
    previous = "full-live-failed-01"
    _write_json(
        runs / f"{previous}.json",
        {"kind": "full_live_evaluation", "status": "failed"},
    )
    (live / previous).mkdir(parents=True)
    environment = {
        "PREVIOUS_EVALUATION_ID": previous,
        "EVALUATION_RETRY_REASON": "Strict runtime was requalified under a new commit.",
    }

    assert live_evaluation.validate_retry_linkage(
        environment,
        run_state_dir=runs,
        live_root=live,
    ) == (previous, environment["EVALUATION_RETRY_REASON"])

    with pytest.raises(LiveEvaluationError, match="requires"):
        live_evaluation.validate_retry_linkage(
            {"PREVIOUS_EVALUATION_ID": previous},
            run_state_dir=runs,
            live_root=live,
        )
