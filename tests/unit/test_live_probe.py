from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess

import pytest

from scripts import live_probe
from scripts.budget_control import RunRequest


def _request() -> RunRequest:
    return RunRequest(
        run_id="gate0-live-probe-01",
        provider="openai",
        model="gpt-5.4-mini",
        max_tokens=1000,
        max_cost_usd=1.0,
        projected_tokens=800,
        projected_cost_usd=0.5,
        concurrency=1,
    )


def test_glm_live_probe_request_is_explicit_and_exact() -> None:
    request = live_probe.requested_run(
        {
            "ALLOW_LIVE_PROBE": "true",
            "EVAL_PROVIDER_PROFILE": "openrouter-glm-5.2-functional",
            "EVALUATION_ID": "glm-smoke-b01-test",
            "EVAL_MAX_TOKENS": "100000",
            "EVAL_MAX_COST_USD": "1",
            "EVAL_PROJECTED_TOKENS": "50000",
            "EVAL_PROJECTED_COST_USD": "0.5",
            "EVAL_CONCURRENCY": "1",
        }
    )

    assert request.provider == "openrouter"
    assert request.model == "z-ai/glm-5.2"
    assert request.openrouter_enabled is True


def test_glm_live_probe_dispatches_the_full_b01_campaign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    def run_command(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        del timeout
        observed.extend(args)
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps({"run_id": "glm-smoke-b01-test", "case_id": "B01", "ok": True}),
            "",
        )

    monkeypatch.setattr(live_probe, "_run_command", run_command)
    _, report = live_probe.execute_transport(
        "glm-smoke-b01-test",
        provider_profile_name="openrouter-glm-5.2-functional",
    )

    assert observed == [
        "docker",
        "compose",
        "exec",
        "-T",
        "app",
        "python",
        "-m",
        "apps.api.app.live_campaign_transport",
        "--case-id",
        "B01",
        "--evaluation-id",
        "glm-smoke-b01-test",
    ]
    assert report["case_id"] == "B01"


def test_glm_running_profile_requires_image_bound_runtime_timeouts(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_id = f"sha256:{'a' * 64}"
    lock_path = tmp_path / "communication_factory.lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "runtime": {
                    "image_id": image_id,
                    "main_loop_max_tokens": 10_240,
                    "safety_call_timeout_seconds": 20,
                    "tool_call_timeout_seconds": 30,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EVAL_PROVIDER_PROFILE", "openrouter-glm-5.2-functional")
    monkeypatch.setattr(live_probe, "CONTRACT_LOCK", lock_path)
    monkeypatch.setattr(live_probe, "_compose_container_id", lambda service: service)

    def inspect(container_id: str, template: str) -> object:
        if template == "{{json .Image}}":
            return image_id
        if container_id == "app":
            return [
                "LIVE_PROVIDER_PROFILE=openrouter-glm-5.2-functional",
                "LIVE_TASK_TIMEOUT_SECONDS=180",
                "LIVE_RUN_TERMINAL_DEADLINE_SECONDS=195",
                "LIVE_USAGE_EXPECTED_PROVIDER=openrouter",
                "LIVE_USAGE_REQUIRE_POST_TASK_SUMMARY=false",
            ]
        return [
            "CF_PROVIDER_PROFILE=openrouter-glm-5.2-functional",
            "CF_RUNTIME_PROVIDER=openrouter",
            "OUROBOROS_MODEL=openrouter::z-ai/glm-5.2",
            "OUROBOROS_MODEL_FALLBACKS=",
            "OPENROUTER_API_KEY_FILE=/run/secrets/openrouter_api_key",
        ]

    monkeypatch.setattr(live_probe, "_inspect_json", inspect)

    assert live_probe.verify_running_profile() == image_id

    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    payload["runtime"]["tool_call_timeout_seconds"] = 5
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(live_probe.LiveProbeError, match="tool-call timeout"):
        live_probe.verify_running_profile()


def test_usage_rows_require_complete_single_provider_identity() -> None:
    report = {
        "provider_call_ledger": {
            "main_generation": {
                "call_count": 2,
                "prompt_tokens": 500,
                "completion_tokens": 50,
                "cost_usd": 0.02,
                "providers": ["openai"],
                "models": ["openai::gpt-5.4-mini"],
            },
            "post_task_summary": {
                "call_count": 1,
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cost_usd": 0.01,
                "providers": ["openai"],
                "models": ["gpt-5.4-mini"],
            },
            "provider_retry": {
                "call_count": 1,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": 0,
                "providers": [],
                "models": [],
            },
        }
    }

    rows = live_probe.usage_rows_from_report("gate0-live-probe-01", report)

    assert [row["category"] for row in rows] == [
        "main_generation",
        "post_task_summary",
    ]
    assert {row["model"] for row in rows} == {"gpt-5.4-mini"}

    report["provider_call_ledger"]["main_generation"]["providers"] = ["openrouter"]
    with pytest.raises(live_probe.LiveProbeError, match="incomplete"):
        live_probe.usage_rows_from_report("gate0-live-probe-01", report)


def test_failed_probe_recovery_accounts_mixed_models_and_preserves_failure(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "glm-smoke-b01-failed-test"
    runs = tmp_path / "runs"
    evidence = tmp_path / "evidence" / run_id
    ledger_path = tmp_path / "usage.jsonl"
    runs.mkdir()
    evidence.mkdir(parents=True)
    report = {
        "run_id": run_id,
        "checks": {"usage_complete": True},
        "provider_call_ledger": {
            "main_generation": {
                "call_count": 2,
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "cost_usd": 0.01,
                "providers": ["openrouter"],
                "models": ["z-ai/glm-5.2"],
            },
            "post_task_summary": {
                "call_count": 1,
                "prompt_tokens": 20,
                "completion_tokens": 5,
                "cost_usd": 0.002,
                "providers": ["openrouter"],
                "models": ["google/gemini-3.5-flash"],
            },
        },
    }
    report_path = evidence / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
    marker_path = runs / f"{run_id}.json"
    marker_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "kind": "gate0_live_probe",
                "status": "failed",
                "usage_complete": False,
                "provider": "openrouter",
                "model": "z-ai/glm-5.2",
                "report_sha256": report_hash,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_probe, "RUN_STATE_DIR", runs)
    monkeypatch.setattr(live_probe, "EVIDENCE_ROOT", tmp_path / "evidence")
    monkeypatch.setattr(live_probe, "DEFAULT_USAGE_LEDGER", ledger_path)

    recovered = live_probe.recover_failed_probe(run_id)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]

    assert recovered["total_tokens"] == 135
    assert recovered["total_cost_usd"] == 0.012
    assert recovered["model_drift_detected"] is True
    assert marker["status"] == "failed"
    assert marker["usage_complete"] is True
    assert marker["usage_recovered"] is True
    assert {row["model"] for row in rows} == {
        "z-ai/glm-5.2",
        "google/gemini-3.5-flash",
    }
    with pytest.raises(live_probe.LiveProbeError, match="unaccounted"):
        live_probe.recover_failed_probe(run_id)


def test_failed_probe_recovery_rejects_orphaned_provider_request(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "glm-smoke-b01-orphaned-test"
    runs = tmp_path / "runs"
    evidence = tmp_path / "evidence" / run_id
    runs.mkdir()
    evidence.mkdir(parents=True)
    report = {
        "run_id": run_id,
        "checks": {"usage_complete": True},
        "provider_call_ledger": {},
    }
    report_path = evidence / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    marker_path = runs / f"{run_id}.json"
    marker_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "kind": "gate0_live_probe",
                "status": "failed",
                "usage_complete": False,
                "provider_usage_unknown": True,
                "provider": "openrouter",
                "model": "z-ai/glm-5.2",
                "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_probe, "RUN_STATE_DIR", runs)
    monkeypatch.setattr(live_probe, "EVIDENCE_ROOT", tmp_path / "evidence")
    monkeypatch.setattr(live_probe, "DEFAULT_USAGE_LEDGER", tmp_path / "usage.jsonl")

    with pytest.raises(live_probe.LiveProbeError, match="orphaned physical provider request"):
        live_probe.recover_failed_probe(run_id)


def test_run_reservation_is_unique_and_records_terminal_usage(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(live_probe, "RUN_STATE_DIR", tmp_path / "runs")
    monkeypatch.setattr(live_probe, "EVIDENCE_ROOT", tmp_path / "evidence")
    monkeypatch.delenv("PREVIOUS_EVALUATION_ID", raising=False)
    monkeypatch.delenv("EVALUATION_RETRY_REASON", raising=False)
    request = _request()

    marker, evidence = live_probe.reserve_run(
        request,
        f"sha256:{'a' * 64}",
        "c" * 40,
    )
    live_probe.finish_run(
        marker,
        status="completed",
        usage_complete=True,
        total_tokens=670,
        total_cost_usd=0.03,
        report_sha256="b" * 64,
    )

    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert evidence.is_dir()
    assert payload["status"] == "completed"
    assert payload["usage_complete"] is True
    assert payload["total_tokens"] == 670
    assert payload["app_commit"] == "c" * 40
    with pytest.raises(live_probe.LiveProbeError, match="already used"):
        live_probe.reserve_run(request, f"sha256:{'a' * 64}", "c" * 40)


def test_retry_reservation_links_only_a_preserved_failed_attempt(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    previous_id = "gate0-live-probe-01"
    (runs / f"{previous_id}.json").write_text(
        json.dumps({"run_id": previous_id, "status": "failed"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_probe, "RUN_STATE_DIR", runs)
    monkeypatch.setattr(live_probe, "EVIDENCE_ROOT", tmp_path / "evidence")
    monkeypatch.setenv("PREVIOUS_EVALUATION_ID", previous_id)
    monkeypatch.setenv("EVALUATION_RETRY_REASON", "Input identifier contract was corrected.")
    request = RunRequest(
        **{**_request().__dict__, "run_id": "gate0-live-probe-02"},
    )

    marker, _ = live_probe.reserve_run(request, f"sha256:{'a' * 64}", "c" * 40)
    payload = json.loads(marker.read_text(encoding="utf-8"))

    assert payload["retry_of"] == previous_id
    assert payload["retry_reason"] == "Input identifier contract was corrected."


def test_retry_linkage_is_validated_before_a_live_probe_is_reserved(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(live_probe, "RUN_STATE_DIR", runs)
    monkeypatch.setenv("PREVIOUS_EVALUATION_ID", "missing-live-probe")
    monkeypatch.setenv("EVALUATION_RETRY_REASON", "Transport fixture dispatch was fixed.")

    with pytest.raises(
        live_probe.LiveProbeError,
        match="linked previous live probe is unavailable",
    ):
        live_probe.validate_retry_linkage()

    monkeypatch.delenv("EVALUATION_RETRY_REASON")
    with pytest.raises(live_probe.LiveProbeError, match="requires previous run id and reason"):
        live_probe.validate_retry_linkage()
