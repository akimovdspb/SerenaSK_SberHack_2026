from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any

import pytest

from scripts import live_evaluation, live_readiness


def _write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _source(
    root: pathlib.Path,
    *,
    run_id: str,
    kind: str,
    case_id: str = "",
    commit: str,
    image_id: str,
    provider_profile: str = "openai-gpt-5.4-mini",
) -> None:
    evidence_root = "live-probes" if kind == "gate0_live_probe" else "live-campaigns"
    report_path = root / "runtime" / evidence_root / run_id / "report.json"
    glm = provider_profile == "openrouter-glm-5.2-functional"
    report: dict[str, Any] = {
        "ok": True,
        "checks": {"green": True},
        "functional_quality_passed": True if glm else None,
    }
    if glm:
        report.update(
            {
                "provider_profile": provider_profile,
                "run": {
                    "prompt_hash": "1" * 64,
                    "skill_content_hash": "2" * 64,
                    "tool_inventory_hash": "3" * 64,
                },
                "provider_call_ledger": {
                    "main_generation": {
                        "call_count": 1,
                        "providers": ["openrouter"],
                        "models": ["z-ai/glm-5.2"],
                        "prompt_tokens": 79_999,
                        "completion_tokens": 1,
                        "cost_usd": 0.04,
                    }
                },
            }
        )
    if kind == "gate0_live_probe":
        report["run_id"] = run_id
        if glm:
            report["case_id"] = "B01"
    else:
        report["checks"]["initial_fact_placement_exact"] = True
        report.update({"evaluation_id": run_id, "case_id": case_id})
    _write_json(report_path, report)
    marker: dict[str, Any] = {
        "run_id": run_id,
        "kind": kind,
        "status": "completed",
        "usage_complete": True,
        "app_commit": commit,
        "runtime_image_id": image_id,
        "provider_profile": provider_profile,
        "provider": "openrouter" if glm else "openai",
        "model": "z-ai/glm-5.2" if glm else "gpt-5.4-mini",
        "concurrency": 1,
        "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "total_tokens": 80_000,
        "total_cost_usd": 0.04,
        "max_tokens": 100_000,
        "max_cost_usd": 0.08,
    }
    if case_id:
        marker["case_id"] = case_id
    _write_json(root / "runtime" / "budget" / "runs" / f"{run_id}.json", marker)


def _environment() -> dict[str, str]:
    return {
        "READINESS_WARMUP_ID": "warmup-01",
        "READINESS_SMOKE_ID": "smoke-01",
        "READINESS_PILOT_IDS": "pilot-b04-01,pilot-b07-01",
        "READINESS_OUTPUTS_REVIEWED_BY_CODEX": "true",
        "READINESS_SAFETY_MULTIPLIER": "1.25",
    }


def test_readiness_builder_binds_current_distinct_reviewed_sources(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = "a" * 40
    image_id = "sha256:" + "b" * 64
    for run_id, kind, case_id in (
        ("warmup-01", "gate0_live_probe", ""),
        ("smoke-01", "gate2_live_campaign", "B04"),
        ("pilot-b04-01", "gate2_live_campaign", "B04"),
        ("pilot-b07-01", "gate2_live_campaign", "B07"),
    ):
        _source(
            tmp_path,
            run_id=run_id,
            kind=kind,
            case_id=case_id,
            commit=commit,
            image_id=image_id,
        )
    contract = tmp_path / "runtime" / "contracts" / "lock.json"
    basket = tmp_path / "data" / "basket.json"
    output = tmp_path / "runtime" / "evaluation" / "live-readiness.json"
    _write_json(contract, {"runtime": {"image_id": image_id}})
    _write_json(basket, {"cases": []})
    monkeypatch.setattr(
        live_readiness, "frozen_git_identity", lambda **_: (commit, "codex/p0-autonomous")
    )
    monkeypatch.setattr(live_readiness, "verify_running_profile", lambda: image_id)
    monkeypatch.setattr(live_evaluation, "ROOT", tmp_path)
    monkeypatch.setattr(
        live_evaluation,
        "RUN_STATE_DIR",
        tmp_path / "runtime" / "budget" / "runs",
    )

    manifest = live_readiness.build_readiness(
        _environment(),
        root=tmp_path,
        run_state_dir=tmp_path / "runtime" / "budget" / "runs",
        live_probe_root=tmp_path / "runtime" / "live-probes",
        live_campaign_root=tmp_path / "runtime" / "live-campaigns",
        contract_path=contract,
        basket_path=basket,
        output_path=output,
    )

    assert output.is_file()
    assert manifest["warmup"]["excluded_from_metrics"] is True
    assert [item["case_id"] for item in manifest["pilots"]] == ["B04", "B07"]
    assert manifest["projection"]["projected_tokens"] == 1_875_000
    assert manifest["projection"]["projected_cost_usd"] == 1.5


def test_readiness_builder_rejects_duplicate_or_relabelled_pilots(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment()
    environment["READINESS_PILOT_IDS"] = "pilot-b04-01,pilot-b04-01"
    with pytest.raises(live_readiness.LiveReadinessError, match="distinct"):
        live_readiness._requested_sources(environment)

    commit = "a" * 40
    image_id = "sha256:" + "b" * 64
    _source(
        tmp_path,
        run_id="pilot-b07-01",
        kind="gate2_live_campaign",
        case_id="B07",
        commit=commit,
        image_id=image_id,
    )
    marker_path = tmp_path / "runtime" / "budget" / "runs" / "pilot-b07-01.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["case_id"] = "B04"
    _write_json(marker_path, marker)
    with pytest.raises(live_readiness.LiveReadinessError, match="case identity"):
        live_readiness._source_entry(
            "pilot-b07-01",
            kind="gate2_live_campaign",
            app_commit=commit,
            runtime_image_id=image_id,
            root=tmp_path,
            run_state_dir=tmp_path / "runtime" / "budget" / "runs",
            live_probe_root=tmp_path / "runtime" / "live-probes",
            live_campaign_root=tmp_path / "runtime" / "live-campaigns",
        )


def test_readiness_rejects_campaign_without_current_placement_check(
    tmp_path: pathlib.Path,
) -> None:
    commit = "a" * 40
    image_id = "sha256:" + "b" * 64
    run_id = "smoke-legacy-01"
    _source(
        tmp_path,
        run_id=run_id,
        kind="gate2_live_campaign",
        case_id="B04",
        commit=commit,
        image_id=image_id,
    )
    report_path = tmp_path / "runtime" / "live-campaigns" / run_id / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    del report["checks"]["initial_fact_placement_exact"]
    _write_json(report_path, report)
    marker_path = tmp_path / "runtime" / "budget" / "runs" / f"{run_id}.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    _write_json(marker_path, marker)

    with pytest.raises(live_readiness.LiveReadinessError, match="output-integrity"):
        live_readiness._source_entry(
            run_id,
            kind="gate2_live_campaign",
            app_commit=commit,
            runtime_image_id=image_id,
            root=tmp_path,
            run_state_dir=tmp_path / "runtime" / "budget" / "runs",
            live_probe_root=tmp_path / "runtime" / "live-probes",
            live_campaign_root=tmp_path / "runtime" / "live-campaigns",
        )


def test_glm_readiness_uses_b01_smoke_and_three_distinct_functional_pilots(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_name = "openrouter-glm-5.2-functional"
    commit = "a" * 40
    image_id = "sha256:" + "b" * 64
    sources = (
        ("glm-smoke-b01", "gate0_live_probe", ""),
        ("glm-pilot-b04", "gate2_live_campaign", "B04"),
        ("glm-pilot-b14", "gate2_live_campaign", "B14"),
        ("glm-pilot-b15", "gate2_live_campaign", "B15"),
    )
    for run_id, kind, case_id in sources:
        _source(
            tmp_path,
            run_id=run_id,
            kind=kind,
            case_id=case_id,
            commit=commit,
            image_id=image_id,
            provider_profile=profile_name,
        )
    contract = tmp_path / "runtime" / "contracts" / "lock.json"
    basket = tmp_path / "data" / "basket.json"
    output = tmp_path / "runtime" / "evaluation" / "live-readiness.json"
    _write_json(contract, {"runtime": {"image_id": image_id}})
    _write_json(basket, {"cases": []})
    monkeypatch.setattr(
        live_readiness, "frozen_git_identity", lambda **_: (commit, "codex/p0-glm-basket")
    )
    monkeypatch.setattr(live_readiness, "verify_running_profile", lambda: image_id)
    monkeypatch.setattr(live_evaluation, "ROOT", tmp_path)
    monkeypatch.setattr(
        live_evaluation,
        "RUN_STATE_DIR",
        tmp_path / "runtime" / "budget" / "runs",
    )
    environment = {
        "EVAL_PROVIDER_PROFILE": profile_name,
        "READINESS_SMOKE_ID": "glm-smoke-b01",
        "READINESS_PILOT_IDS": "glm-pilot-b04,glm-pilot-b14,glm-pilot-b15",
        "READINESS_OUTPUTS_REVIEWED_BY_CODEX": "true",
        "READINESS_SAFETY_MULTIPLIER": "1.2",
    }

    manifest = live_readiness.build_readiness(
        environment,
        root=tmp_path,
        run_state_dir=tmp_path / "runtime" / "budget" / "runs",
        live_probe_root=tmp_path / "runtime" / "live-probes",
        live_campaign_root=tmp_path / "runtime" / "live-campaigns",
        contract_path=contract,
        basket_path=basket,
        output_path=output,
    )

    assert "warmup" not in manifest
    assert manifest["provider_profile"] == profile_name
    assert manifest["smoke"]["run_id"] == "glm-smoke-b01"
    assert [item["case_id"] for item in manifest["pilots"]] == ["B04", "B14", "B15"]


def test_glm_readiness_rollover_requires_current_b01_and_hash_equal_historical_pilots(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_name = "openrouter-glm-5.2-functional"
    current_commit = "a" * 40
    smoke_commit = "b" * 40
    pilot_commit = "c" * 40
    current_image = "sha256:" + "d" * 64
    historical_image = "sha256:" + "e" * 64
    sources = (
        ("current-b01", "gate0_live_probe", "", smoke_commit, current_image),
        ("old-b04", "gate2_live_campaign", "B04", pilot_commit, historical_image),
        ("old-b14", "gate2_live_campaign", "B14", pilot_commit, historical_image),
        ("old-b15", "gate2_live_campaign", "B15", pilot_commit, historical_image),
    )
    for run_id, kind, case_id, commit, image_id in sources:
        _source(
            tmp_path,
            run_id=run_id,
            kind=kind,
            case_id=case_id,
            commit=commit,
            image_id=image_id,
            provider_profile=profile_name,
        )
    contract = tmp_path / "runtime" / "contracts" / "lock.json"
    basket = tmp_path / "data" / "basket.json"
    output = tmp_path / "runtime" / "evaluation" / "live-readiness.json"
    handoff = tmp_path / "HANDOFF_VPS_P0_GLM_BASKET.md"
    _write_json(
        contract,
        {
            "runtime": {"image_id": current_image},
            "skill": {"prompt_hash": "1" * 64, "skill_content_hash": "2" * 64},
            "tools": {"inventory_hash": "3" * 64},
        },
    )
    _write_json(basket, {"cases": []})
    handoff.write_text("owner rollover\n", encoding="utf-8")
    authority_sha256 = hashlib.sha256(handoff.read_bytes()).hexdigest()
    monkeypatch.setattr(
        live_readiness,
        "frozen_git_identity",
        lambda **_: (current_commit, "codex/p0-glm-basket"),
    )
    monkeypatch.setattr(live_readiness, "verify_running_profile", lambda: current_image)
    monkeypatch.setattr(live_evaluation, "ROOT", tmp_path)
    monkeypatch.setattr(live_evaluation, "HANDOFF_PATH", handoff)
    monkeypatch.setattr(
        live_evaluation,
        "RUN_STATE_DIR",
        tmp_path / "runtime" / "budget" / "runs",
    )
    environment = {
        "EVAL_PROVIDER_PROFILE": profile_name,
        "READINESS_SMOKE_ID": "current-b01",
        "READINESS_PILOT_IDS": "old-b04,old-b14,old-b15",
        "READINESS_OUTPUTS_REVIEWED_BY_CODEX": "true",
        "READINESS_SAFETY_MULTIPLIER": "1.2",
        "READINESS_ALLOW_IDENTITY_ROLLOVER": "true",
        "READINESS_USE_EMPIRICAL_OPERATION_ENVELOPE": "true",
        "READINESS_RECOVERY_QUARANTINED_RUN_ID": "failed-basket-01",
        "GLM_NIGHT_AUTHORITY_SHA256": authority_sha256,
    }

    manifest = live_readiness.build_readiness(
        environment,
        root=tmp_path,
        run_state_dir=tmp_path / "runtime" / "budget" / "runs",
        live_probe_root=tmp_path / "runtime" / "live-probes",
        live_campaign_root=tmp_path / "runtime" / "live-campaigns",
        contract_path=contract,
        basket_path=basket,
        output_path=output,
    )

    assert manifest["identity_rollover"]["authority_sha256"] == authority_sha256
    assert manifest["smoke"]["source_runtime_image_id"] == current_image
    assert {item["source_runtime_image_id"] for item in manifest["pilots"]} == {historical_image}
    assert manifest["projection"]["basis"] == "largest_observed_hash_equal_operation"
    assert manifest["projection"]["projected_tokens"] == 1_440_000
    assert manifest["projection"]["projected_cost_usd"] == 0.72
    assert manifest["projection"]["includes_maximum_output"] is False

    pilot_report = tmp_path / "runtime" / "live-campaigns" / "old-b14" / "report.json"
    tampered = json.loads(pilot_report.read_text(encoding="utf-8"))
    tampered["run"]["prompt_hash"] = "f" * 64
    _write_json(pilot_report, tampered)
    marker_path = tmp_path / "runtime" / "budget" / "runs" / "old-b14.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["report_sha256"] = hashlib.sha256(pilot_report.read_bytes()).hexdigest()
    _write_json(marker_path, marker)
    environment["ALLOW_READINESS_REPLACE"] = "true"
    with pytest.raises(live_readiness.LiveReadinessError, match="generation contract differs"):
        live_readiness.build_readiness(
            environment,
            root=tmp_path,
            run_state_dir=tmp_path / "runtime" / "budget" / "runs",
            live_probe_root=tmp_path / "runtime" / "live-probes",
            live_campaign_root=tmp_path / "runtime" / "live-campaigns",
            contract_path=contract,
            basket_path=basket,
            output_path=output,
        )
