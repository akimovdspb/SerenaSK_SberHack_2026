from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

import request_ledger
from scripts import campaign_authoring_quality as quality


def _catalog_response(
    *,
    prompt: str = "0.00000091",
    completion: str = "0.00000286",
    canonical_slug: str = quality.EXPECTED_CANONICAL_SLUG,
) -> io.StringIO:
    return io.StringIO(
        json.dumps(
            {
                "data": [
                    {
                        "id": "z-ai/glm-5.2",
                        "canonical_slug": canonical_slug,
                        "pricing": {"prompt": prompt, "completion": completion},
                    }
                ]
            }
        )
    )


def test_price_contract_pins_current_catalog_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        quality.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _catalog_response(),
    )

    contract = quality._price_contract()

    assert contract["canonical_slug"] == quality.EXPECTED_CANONICAL_SLUG
    assert contract["input_price_per_token_usd"] == "9.1E-7"
    assert contract["output_price_per_token_usd"] == "0.00000286"


@pytest.mark.parametrize(
    ("prompt", "completion", "canonical_slug"),
    [
        ("0.00000101", "0.00000286", quality.EXPECTED_CANONICAL_SLUG),
        ("0.00000091", "0.00000301", quality.EXPECTED_CANONICAL_SLUG),
        ("0.00000091", "0.00000286", "z-ai/glm-5.2-drifted"),
    ],
)
def test_price_contract_rejects_material_price_or_canonical_drift(
    monkeypatch: pytest.MonkeyPatch,
    prompt: str,
    completion: str,
    canonical_slug: str,
) -> None:
    monkeypatch.setattr(
        quality.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _catalog_response(
            prompt=prompt,
            completion=completion,
            canonical_slug=canonical_slug,
        ),
    )

    with pytest.raises(quality.CampaignAuthoringQualityError):
        quality._price_contract()


def _initialized_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, dict]:
    root = tmp_path / "repo"
    root.mkdir()
    quality_root = root / "runtime" / "campaign-authoring-quality-v3"
    monkeypatch.setattr(quality, "ROOT", root)
    monkeypatch.setattr(quality, "QUALITY_ROOT", quality_root)
    monkeypatch.setattr(
        quality,
        "_assert_git_identity",
        lambda *, clean: ("f" * 40, quality.REQUIRED_BRANCH),
    )
    monkeypatch.setattr(quality, "_assert_secret_boundary", lambda: Path("/safe/key"))
    monkeypatch.setattr(
        quality,
        "_price_contract",
        lambda: {
            "model": "z-ai/glm-5.2",
            "canonical_slug": "z-ai/glm-5.2-20260616",
            "input_price_per_token_usd": "0.00000091",
            "output_price_per_token_usd": "0.00000286",
            "input_price_ceiling_per_token_usd": str(quality.PRICE_INPUT_CEILING_PER_TOKEN),
            "output_price_ceiling_per_token_usd": str(quality.PRICE_OUTPUT_CEILING_PER_TOKEN),
            "source": quality.OPENROUTER_MODELS_URL,
            "observed_at": "2026-07-17T00:00:00Z",
            "projection_sha256": "a" * 64,
        },
    )
    state = quality.initialize_run(evaluation_id="quality_test_001")
    return quality_root / "quality_test_001", state


def test_initialize_creates_fresh_goal_only_request_ledger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_dir, state = _initialized_run(monkeypatch, tmp_path)

    ledger = request_ledger.read_ledger(run_dir / "request-ledger.json")

    assert state["status"] == "initialized"
    assert state["branch"] == quality.REQUIRED_BRANCH
    assert ledger["goal_id"] == quality.GOAL_ID
    assert ledger["caps"] == {
        "request_cost_usd": "2.000000000000",
        "request_tokens": 500_000,
        "run_cost_usd": "150.000000000000",
        "run_tokens": 50_000_000,
    }
    assert ledger["requests"] == []
    assert ledger["route"]["input_price_per_token_usd"] == "0.000000910000"
    assert ledger["route"]["output_price_per_token_usd"] == "0.000002860000"


def test_compose_environment_pins_route_and_never_passes_key_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_dir, state = _initialized_run(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-survive")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-survive")
    monkeypatch.setattr(quality, "_secret_path", lambda: Path("/safe/openrouter-key.txt"))

    environment = quality._compose_environment(run_dir, state)

    assert "OPENROUTER_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert environment["OUROBOROS_MODEL"] == "openrouter::z-ai/glm-5.2"
    assert environment["EVAL_PROVIDER_PROFILE"] == ("openrouter-glm-5.2-campaign-authoring")
    assert environment["CF_REQUEST_LEDGER_CONTAINER_PATH"] == ("/accounting/request-ledger.json")
    assert environment["OUROBOROS_REVIEW_MAX_TOKENS"] == "16384"
    assert environment["TOTAL_BUDGET"] == "150"
    assert environment["LOCAL_UID"] == str(os.getuid())


def test_summary_counts_latest_case_attempt_and_retained_request_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_dir, state = _initialized_run(monkeypatch, tmp_path)
    state["status"] = "cases_running"
    state["cases"] = {
        "DQ01": [
            {
                "status": "failed",
                "report_path": "first.json",
                "qualification_phase": "basket",
            },
            {
                "status": "passed",
                "report_path": "second.json",
                "qualification_phase": "retry",
                "retry_of_phase": "basket",
            },
        ],
        "DQ03": [
            {
                "status": "failed",
                "report_path": "dq03.json",
                "qualification_phase": "basket",
            }
        ],
    }
    quality._atomic_json(run_dir / "run.json", state)
    request_ledger.bind_task(
        run_dir / "request-ledger.json",
        task_id="task_unknown",
        case_id="DQ03",
        attempt_id="attempt_unknown",
        request_digest="b" * 64,
    )
    reserved = request_ledger.reserve_request(
        run_dir / "request-ledger.json",
        task_id="task_unknown",
        category="main_generation",
        provider="openrouter",
        model="z-ai/glm-5.2",
        provider_call_id="provider_unknown",
        estimated_prompt_tokens=1_000,
        configured_max_output_tokens=1_000,
        request_digest="c" * 64,
    )
    request_ledger.retain_unknown(
        run_dir / "request-ledger.json",
        request_id=str(reserved["request_id"]),
        failure_type="ReadTimeout",
    )

    summary = quality.summarize(run_dir)

    assert summary["mechanically_valid_cases"] == 1
    assert summary["case_matrix"][0]["attempts"] == 2
    assert summary["case_matrix"][0]["status"] == "passed"
    assert summary["retained_unknown"][0]["case_id"] == "DQ03"


def test_transient_retry_remains_available_with_durable_unknown_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_dir, _state = _initialized_run(monkeypatch, tmp_path)
    report_path = quality.ROOT / "transient.json"
    quality._atomic_json(
        report_path,
        {
            "run": {
                "attempts": [{"retry_allowed": True}],
                "reason_code": "TRANSIENT_RUNTIME_PROVIDER_UNAVAILABLE",
            }
        },
    )
    request_ledger.bind_task(
        run_dir / "request-ledger.json",
        task_id="task_transient",
        case_id="DQ06",
        attempt_id="attempt_transient",
        request_digest="b" * 64,
    )
    reserved = request_ledger.reserve_request(
        run_dir / "request-ledger.json",
        task_id="task_transient",
        category="main_generation",
        provider="openrouter",
        model="z-ai/glm-5.2",
        provider_call_id="provider_transient",
        estimated_prompt_tokens=1_000,
        configured_max_output_tokens=1_000,
        request_digest="c" * 64,
    )
    request_ledger.retain_unknown(
        run_dir / "request-ledger.json",
        request_id=str(reserved["request_id"]),
        failure_type="ReadTimeout",
    )

    assert quality._transient_retry_allowed(
        run_dir,
        case_id="DQ06",
        previous={"report_path": "transient.json"},
    )


def test_durable_unknown_bound_does_not_make_permanent_failure_retryable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_dir, _state = _initialized_run(monkeypatch, tmp_path)
    report_path = quality.ROOT / "permanent.json"
    quality._atomic_json(
        report_path,
        {
            "run": {
                "attempts": [{"retry_allowed": False}],
                "reason_code": "TOOL_SEQUENCE_INVALID",
            }
        },
    )
    request_ledger.bind_task(
        run_dir / "request-ledger.json",
        task_id="task_permanent",
        case_id="DQ03",
        attempt_id="attempt_permanent",
        request_digest="d" * 64,
    )
    reserved = request_ledger.reserve_request(
        run_dir / "request-ledger.json",
        task_id="task_permanent",
        category="main_generation",
        provider="openrouter",
        model="z-ai/glm-5.2",
        provider_call_id="provider_permanent",
        estimated_prompt_tokens=1_000,
        configured_max_output_tokens=1_000,
        request_digest="e" * 64,
    )
    request_ledger.retain_unknown(
        run_dir / "request-ledger.json",
        request_id=str(reserved["request_id"]),
        failure_type="ReadTimeout",
    )

    assert not quality._transient_retry_allowed(
        run_dir,
        case_id="DQ03",
        previous={"report_path": "permanent.json"},
    )
