from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any

from provider_profiles import ProviderProfileError, requested_provider_profile
from scripts.budget_control import (
    DEFAULT_USAGE_LEDGER,
    RUN_ID_PATTERN,
    BudgetPolicyError,
    NightBudget,
    RunRequest,
    night_marker_fields,
    normalize_model,
    read_usage_ledger,
    validate_paid_run_budget,
)
from scripts.preflight import run_preflight
from scripts.release_identity import frozen_git_identity

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME_ADMIN = "/opt/communication-factory/runtime_admin.py"
CONFIGURE_RUNTIME = "/opt/communication-factory/configure_runtime.py"
RUN_STATE_DIR = ROOT / "runtime" / "budget" / "runs"
REVIEW_ACCOUNTING_ROOT = ROOT / "runtime" / "skill-reviews"
PERSISTED_REVIEW_PATH = (
    "/home/ouroboros/Ouroboros/data/state/skills/communication_factory/review.json"
)


class BootstrapError(RuntimeError):
    pass


def _docker_exec(*args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        ["docker", "compose", "exec", "-T", "ouroboros", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return process


def _admin(
    command: str,
    *,
    require_skill: bool = False,
    timeout: int = 300,
    allow_failure_payload: bool = False,
) -> dict[str, Any]:
    args = ["python", RUNTIME_ADMIN, command]
    if require_skill:
        args.append("--require-skill")
    process = _docker_exec(*args, timeout=timeout)
    stream = process.stdout if process.stdout.strip() else process.stderr
    try:
        payload = json.loads(stream.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"runtime admin command {command} returned no safe JSON") from exc
    if not isinstance(payload, dict):
        raise BootstrapError(f"runtime admin command {command} returned invalid JSON")
    if process.returncode != 0 and not allow_failure_payload:
        raise BootstrapError(f"runtime admin command {command} failed")
    return payload


def _wait_for_runtime(timeout_sec: int = 90) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            return _admin("snapshot")
        except BootstrapError:
            time.sleep(1)
    raise BootstrapError("runtime supervisor did not become ready before bootstrap")


def _required_positive_int(name: str) -> int:
    try:
        value = int(os.environ.get(name, "0"))
    except ValueError as exc:
        raise BootstrapError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise BootstrapError(f"{name} must be a positive integer")
    return value


def _required_positive_float(name: str) -> float:
    try:
        value = float(os.environ.get(name, "0"))
    except ValueError as exc:
        raise BootstrapError(f"{name} must be positive") from exc
    if value <= 0:
        raise BootstrapError(f"{name} must be positive")
    return value


def _reserve_run(request: RunRequest, night: NightBudget | None = None) -> pathlib.Path:
    RUN_STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = RUN_STATE_DIR / f"{request.run_id}.json"
    payload = {
        "schema_version": 1,
        "run_id": request.run_id,
        "kind": "skill_review",
        "provider": request.provider,
        "model": normalize_model(request.model),
        "provider_profile": request.profile_name,
        "max_tokens": request.max_tokens,
        "max_cost_usd": request.max_cost_usd,
        "projected_tokens": request.projected_tokens,
        "projected_cost_usd": request.projected_cost_usd,
        "status": "running",
        "started_at": dt.datetime.now(dt.UTC).isoformat(),
        "account_remaining": "unknown",
    }
    if night is not None:
        payload.update(night_marker_fields(night))
    previous_run_id = str(os.environ.get("PREVIOUS_EVALUATION_ID") or "").strip()
    retry_reason = str(os.environ.get("EVALUATION_RETRY_REASON") or "").strip()
    if previous_run_id or retry_reason:
        if not previous_run_id or not retry_reason:
            raise BootstrapError("retry linkage requires previous run id and reason")
        if not RUN_ID_PATTERN.fullmatch(previous_run_id):
            raise BootstrapError("linked previous run id is invalid")
        if len(retry_reason) > 500 or any(ord(char) < 32 for char in retry_reason):
            raise BootstrapError("retry reason is invalid")
        previous_path = RUN_STATE_DIR / f"{previous_run_id}.json"
        if not previous_path.is_file():
            raise BootstrapError("linked previous review attempt does not exist")
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        if previous.get("status") != "failed":
            raise BootstrapError("linked previous review attempt is not failed")
        payload["retry_of"] = previous_run_id
        payload["retry_reason"] = retry_reason
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
    except FileExistsError as exc:
        raise BootstrapError("bootstrap review run id was already used") from exc
    return path


def _finish_run(
    path: pathlib.Path,
    *,
    status: str,
    usage_complete: bool,
    total_tokens: int = 0,
    total_cost_usd: float = 0.0,
    extra_fields: dict[str, Any] | None = None,
) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(
        {
            "status": status,
            "usage_complete": usage_complete,
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost_usd, 8),
            "finished_at": dt.datetime.now(dt.UTC).isoformat(),
        }
    )
    if extra_fields:
        payload.update(extra_fields)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _append_review_usage(
    run_id: str,
    review: dict[str, Any],
    *,
    observed_provider: str,
) -> tuple[int, float, tuple[str, ...], tuple[str, ...]]:
    rows = review.get("usage")
    if not isinstance(rows, list) or not rows:
        raise BootstrapError("skill review returned no provider usage")
    try:
        existing_rows = read_usage_ledger(DEFAULT_USAGE_LEDGER)
    except BudgetPolicyError as exc:
        raise BootstrapError(str(exc)) from exc
    if any(row.run_id == run_id for row in existing_rows):
        raise BootstrapError("skill review already has provider usage rows")
    DEFAULT_USAGE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    total_tokens = 0
    total_cost = 0.0
    serialized: list[str] = []
    observed_models: set[str] = set()
    if observed_provider not in {"openai", "openrouter"}:
        raise BootstrapError("skill review provider identity is invalid")
    for row in rows:
        if not isinstance(row, dict):
            raise BootstrapError("skill review usage is malformed")
        prompt_tokens = int(row.get("prompt_tokens") or 0)
        completion_tokens = int(row.get("completion_tokens") or 0)
        cost_usd = float(row.get("cost_usd") or 0.0)
        model = normalize_model(str(row.get("model") or ""))
        if prompt_tokens <= 0 or completion_tokens < 0 or cost_usd < 0 or not model:
            raise BootstrapError("skill review usage is incomplete")
        observed_models.add(model)
        total_tokens += prompt_tokens + completion_tokens
        total_cost += cost_usd
        serialized.append(
            json.dumps(
                {
                    "ts": dt.datetime.now(dt.UTC).isoformat(),
                    "run_id": run_id,
                    "provider": observed_provider,
                    "model": model,
                    "category": "skill_review",
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "cost_usd": cost_usd,
                },
                sort_keys=True,
            )
        )
    with DEFAULT_USAGE_LEDGER.open("a", encoding="utf-8") as handle:
        for row in serialized:
            handle.write(row + "\n")
    return total_tokens, total_cost, (observed_provider,), tuple(sorted(observed_models))


def _runtime_route_parts(value: Any) -> tuple[str, str]:
    route = str(value or "").strip()
    if "::" not in route:
        raise BootstrapError("runtime review route is malformed")
    provider, raw_model = route.split("::", 1)
    model = normalize_model(raw_model)
    if provider not in {"openai", "openrouter"} or not model:
        raise BootstrapError("runtime review route is unsupported")
    return provider, model


def _review_failure_fields(
    *,
    request: RunRequest,
    observed_providers: tuple[str, ...],
    observed_models: tuple[str, ...],
    total_tokens: int,
    total_cost_usd: float,
    accounting_hash: str = "",
) -> dict[str, Any]:
    provider_drifted = list(observed_providers) != [request.provider]
    model_drifted = list(observed_models) != [normalize_model(request.model)]
    classes: list[str] = []
    if provider_drifted:
        classes.append("provider.route_drift")
    if model_drifted:
        classes.append("provider.model_drift")
    fields: dict[str, Any] = {
        "known_tokens": total_tokens,
        "known_cost_usd": round(total_cost_usd, 8),
        "provider_usage_unknown": False,
        "evidence_eligible": False,
        "observed_providers": list(observed_providers),
        "observed_models": list(observed_models),
        "provider_drift_detected": provider_drifted,
        "model_drift_detected": model_drifted,
        "failure_classes": classes or ["skill_review.verdict"],
    }
    if accounting_hash:
        fields["accounting_artifact_sha256"] = accounting_hash
    return fields


def _write_review_accounting(
    *,
    run_id: str,
    request: RunRequest,
    review: dict[str, Any],
    observed_providers: tuple[str, ...],
    observed_models: tuple[str, ...],
    total_tokens: int,
    total_cost_usd: float,
) -> str:
    REVIEW_ACCOUNTING_ROOT.mkdir(parents=True, exist_ok=True)
    path = REVIEW_ACCOUNTING_ROOT / f"{run_id}.json"
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "requested_provider": request.provider,
        "requested_model": normalize_model(request.model),
        "observed_providers": list(observed_providers),
        "observed_models": list(observed_models),
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost_usd, 8),
        "review_status": str(review.get("status") or ""),
        "review_content_hash": str(review.get("content_hash") or ""),
        "reviewed_at": str(review.get("reviewed_at") or ""),
        "accounting_source": "ouroboros_skill_review_usage",
        "provider_ledger_mutated_by_estimate": False,
        "evidence_eligible": False,
        "recorded_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
    except FileExistsError as exc:
        raise BootstrapError("skill review accounting artifact already exists") from exc
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _persisted_review_snapshot() -> tuple[dict[str, Any], str]:
    process = _docker_exec("sh", "-c", f"cat {PERSISTED_REVIEW_PATH}", timeout=30)
    try:
        persisted = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise BootstrapError("persisted skill review is unavailable") from exc
    if process.returncode != 0 or not isinstance(persisted, dict):
        raise BootstrapError("persisted skill review is unavailable")
    snapshot = _admin("snapshot")
    raw_settings = snapshot.get("settings")
    settings = raw_settings if isinstance(raw_settings, dict) else {}
    observed_provider, _ = _runtime_route_parts(settings.get("model"))
    raw_skill = snapshot.get("skill")
    skill = raw_skill if isinstance(raw_skill, dict) else {}
    content_hash = str(persisted.get("content_hash") or "")
    if not content_hash or content_hash != str(skill.get("content_hash") or ""):
        raise BootstrapError("persisted skill review content identity drifted")
    usage: list[dict[str, Any]] = []
    for actor in persisted.get("raw_actor_records") or []:
        if isinstance(actor, dict):
            usage.append(
                {
                    "model": str(actor.get("model_id") or ""),
                    "status": str(actor.get("status") or ""),
                    "prompt_tokens": int(actor.get("tokens_in") or 0),
                    "completion_tokens": int(actor.get("tokens_out") or 0),
                    "cost_usd": float(actor.get("cost_usd") or 0.0),
                }
            )
    return (
        {
            "status": str(skill.get("review_status") or ""),
            "content_hash": content_hash,
            "reviewed_at": str(persisted.get("timestamp") or ""),
            "usage": usage,
        },
        observed_provider,
    )


def _runtime_review_route() -> tuple[str, str]:
    snapshot = _admin("snapshot")
    raw_settings = snapshot.get("settings")
    settings = raw_settings if isinstance(raw_settings, dict) else {}
    return _runtime_route_parts(settings.get("model"))


def _assert_runtime_review_route(request: RunRequest) -> tuple[str, str]:
    observed_provider, observed_model = _runtime_review_route()
    if observed_provider != request.provider or observed_model != normalize_model(request.model):
        raise BootstrapError("runtime review route differs from requested provider profile")
    return observed_provider, observed_model


def _parse_utc_timestamp(value: Any, *, field: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise BootstrapError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise BootstrapError(f"{field} must include timezone")
    return parsed.astimezone(dt.UTC)


def recover_failed_review_accounting(run_id: str) -> dict[str, Any]:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise BootstrapError("review recovery run id is invalid")
    marker_path = RUN_STATE_DIR / f"{run_id}.json"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError("failed review marker is unavailable") from exc
    if (
        not isinstance(marker, dict)
        or marker.get("run_id") != run_id
        or marker.get("kind") != "skill_review"
        or marker.get("status") != "failed"
        or marker.get("usage_complete") is not False
        or int(marker.get("total_tokens") or 0) != 0
        or float(marker.get("total_cost_usd") or 0.0) != 0.0
    ):
        raise BootstrapError("failed review marker is not eligible for accounting recovery")
    try:
        existing_rows = read_usage_ledger(DEFAULT_USAGE_LEDGER)
    except BudgetPolicyError as exc:
        raise BootstrapError(str(exc)) from exc
    if any(row.run_id == run_id for row in existing_rows):
        raise BootstrapError("failed review already has provider usage rows")
    if (REVIEW_ACCOUNTING_ROOT / f"{run_id}.json").exists():
        raise BootstrapError("failed review accounting artifact already exists")

    review, observed_provider = _persisted_review_snapshot()
    reviewed_at = _parse_utc_timestamp(review.get("reviewed_at"), field="review timestamp")
    started_at = _parse_utc_timestamp(marker.get("started_at"), field="run start timestamp")
    finished_at = _parse_utc_timestamp(marker.get("finished_at"), field="run finish timestamp")
    if not started_at <= reviewed_at <= finished_at:
        raise BootstrapError("persisted review does not belong to the failed run window")
    if review.get("status") != "clean" or not str(review.get("content_hash") or ""):
        raise BootstrapError("persisted review identity is incomplete")

    request = RunRequest(
        run_id=run_id,
        provider=str(marker.get("provider") or ""),
        model=str(marker.get("model") or ""),
        max_tokens=int(marker.get("max_tokens") or 0),
        max_cost_usd=float(marker.get("max_cost_usd") or 0.0),
        projected_tokens=int(marker.get("projected_tokens") or 0),
        projected_cost_usd=float(marker.get("projected_cost_usd") or 0.0),
        concurrency=1,
        openrouter_enabled=True,
        profile_name=str(marker.get("provider_profile") or ""),
    )
    raw_rows = review.get("usage")
    rows = raw_rows if isinstance(raw_rows, list) else []
    preview_models = tuple(
        sorted(
            {
                normalize_model(str(row.get("model") or ""))
                for row in rows
                if isinstance(row, dict) and str(row.get("model") or "").strip()
            }
        )
    )
    if observed_provider == request.provider and list(preview_models) == [
        normalize_model(request.model)
    ]:
        raise BootstrapError("persisted review does not prove the recorded route drift")
    used_tokens, used_cost, observed_providers, observed_models = _append_review_usage(
        run_id,
        review,
        observed_provider=observed_provider,
    )
    if used_tokens > request.max_tokens or used_cost > request.max_cost_usd:
        raise BootstrapError("recovered skill review usage exceeded its supplied run cap")
    failure_fields = _review_failure_fields(
        request=request,
        observed_providers=observed_providers,
        observed_models=observed_models,
        total_tokens=used_tokens,
        total_cost_usd=used_cost,
    )
    if not (failure_fields["provider_drift_detected"] or failure_fields["model_drift_detected"]):
        raise BootstrapError("persisted review does not prove the recorded route drift")
    accounting_hash = _write_review_accounting(
        run_id=run_id,
        request=request,
        review=review,
        observed_providers=observed_providers,
        observed_models=observed_models,
        total_tokens=used_tokens,
        total_cost_usd=used_cost,
    )
    failure_fields["accounting_artifact_sha256"] = accounting_hash
    marker.update(
        {
            "status": "failed",
            "usage_complete": True,
            "total_tokens": used_tokens,
            "total_cost_usd": round(used_cost, 8),
            "accounting_reconciled_at": dt.datetime.now(dt.UTC).isoformat(),
            "accounting_source": "persisted_ouroboros_skill_review",
            **failure_fields,
        }
    )
    temporary = marker_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, marker_path)
    return {
        "run_id": run_id,
        "tokens": used_tokens,
        "cost_usd": round(used_cost, 8),
        "observed_providers": list(observed_providers),
        "observed_models": list(observed_models),
        "accounting_artifact_sha256": accounting_hash,
    }


def _paid_review() -> dict[str, Any]:
    if os.environ.get("ALLOW_BOOTSTRAP_REVIEW", "").lower() != "true":
        raise BootstrapError(
            "fresh executable skill review is required; rerun with explicit review opt-in and caps"
        )
    run_id = str(os.environ.get("EVALUATION_ID") or "").strip()
    try:
        provider_profile = requested_provider_profile(dict(os.environ))
    except ProviderProfileError as exc:
        raise BootstrapError(str(exc)) from exc
    request = RunRequest(
        run_id=run_id,
        provider=provider_profile.ledger_provider,
        model=provider_profile.normalized_model,
        max_tokens=_required_positive_int("EVAL_MAX_TOKENS"),
        max_cost_usd=_required_positive_float("EVAL_MAX_COST_USD"),
        projected_tokens=int(
            os.environ.get("EVAL_PROJECTED_TOKENS") or _required_positive_int("EVAL_MAX_TOKENS")
        ),
        projected_cost_usd=float(
            os.environ.get("EVAL_PROJECTED_COST_USD")
            or _required_positive_float("EVAL_MAX_COST_USD")
        ),
        concurrency=int(os.environ.get("EVAL_CONCURRENCY", "0")),
        openrouter_enabled=provider_profile.runtime_provider == "openrouter",
        profile_name=provider_profile.name,
    )
    try:
        frozen_git_identity(
            root=ROOT,
            required_branch=provider_profile.required_branch,
        )
        observed_provider, _ = _assert_runtime_review_route(request)
        night = validate_paid_run_budget(
            request,
            run_state_dir=RUN_STATE_DIR,
            run_kind="skill_review",
        )
    except BudgetPolicyError as exc:
        raise BootstrapError(str(exc)) from exc
    marker = _reserve_run(request, night)
    usage_complete = False
    used_tokens = 0
    used_cost = 0.0
    failure_fields: dict[str, Any] = {}
    try:
        review_command = (
            "review-rebuttal"
            if os.environ.get("USE_SKILL_REVIEW_REBUTTAL", "").lower() == "true"
            else "review"
        )
        review = _admin(review_command, timeout=300, allow_failure_payload=True)
        (
            used_tokens,
            used_cost,
            observed_providers,
            observed_models,
        ) = _append_review_usage(
            run_id,
            review,
            observed_provider=observed_provider,
        )
        usage_complete = True
        failure_fields = _review_failure_fields(
            request=request,
            observed_providers=observed_providers,
            observed_models=observed_models,
            total_tokens=used_tokens,
            total_cost_usd=used_cost,
        )
        if used_tokens > request.max_tokens or used_cost > request.max_cost_usd:
            raise BootstrapError("skill review exceeded its supplied run cap")
        if failure_fields["provider_drift_detected"] or failure_fields["model_drift_detected"]:
            failure_fields["accounting_artifact_sha256"] = _write_review_accounting(
                run_id=run_id,
                request=request,
                review=review,
                observed_providers=observed_providers,
                observed_models=observed_models,
                total_tokens=used_tokens,
                total_cost_usd=used_cost,
            )
            raise BootstrapError("skill review provider/model switched unexpectedly")
        if review.get("status") != "clean" or review.get("error_present"):
            raise BootstrapError("skill review did not produce a clean verdict")
        _finish_run(
            marker,
            status="completed",
            usage_complete=True,
            total_tokens=used_tokens,
            total_cost_usd=used_cost,
        )
        return review
    except Exception:
        _finish_run(
            marker,
            status="failed",
            usage_complete=usage_complete,
            total_tokens=used_tokens,
            total_cost_usd=used_cost,
            extra_fields=failure_fields,
        )
        raise


def bootstrap() -> dict[str, Any]:
    run_preflight("bootstrap")
    _wait_for_runtime()
    configured = _docker_exec("python", CONFIGURE_RUNTIME, timeout=30)
    if configured.returncode != 0:
        raise BootstrapError("supported runtime settings write failed")
    snapshot = _admin("refresh")
    skill = snapshot.get("skill") or {}
    review_performed = False
    force_review = os.environ.get("FORCE_BOOTSTRAP_REVIEW", "").lower() == "true"
    if force_review or not skill.get("executable_review") or skill.get("review_stale"):
        _paid_review()
        review_performed = True
        snapshot = _admin("snapshot")
        skill = snapshot.get("skill") or {}
    if not skill.get("enabled"):
        _admin("enable")
    final = _admin("snapshot", require_skill=True)
    return {
        "runtime_ready": bool((final.get("runtime") or {}).get("supervisor_ready")),
        "workers_alive": int((final.get("runtime") or {}).get("workers_alive") or 0),
        "mcp_tools": list((final.get("mcp") or {}).get("prefixed_tools") or []),
        "mcp_timeout_sec": int((final.get("mcp") or {}).get("tool_timeout_sec") or 0),
        "safety_call_timeout_sec": int(
            (final.get("settings") or {}).get("safety_call_timeout_sec") or 0
        ),
        "tool_call_timeout_sec": int(
            (final.get("settings") or {}).get("tool_call_timeout_sec") or 0
        ),
        "skill_status": (final.get("skill") or {}).get("review_status"),
        "skill_enabled": bool((final.get("skill") or {}).get("enabled")),
        "review_performed": review_performed,
        "provider_secrets_persisted": not bool(
            (final.get("settings") or {}).get("provider_secrets_empty")
        ),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recover-review-run-id", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.recover_review_run_id:
            recovered = recover_failed_review_accounting(args.recover_review_run_id)
            print(
                "bootstrap-runtime: RECOVERED "
                f"run_id={recovered['run_id']} tokens={recovered['tokens']} "
                f"cost_usd={recovered['cost_usd']:.8f} evidence_eligible=false"
            )
            return 0
        result = bootstrap()
    except (OSError, ValueError, subprocess.SubprocessError, BootstrapError) as exc:
        print(f"bootstrap-runtime: FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        "bootstrap-runtime: PASS "
        f"workers={result['workers_alive']} mcp_tools={len(result['mcp_tools'])} "
        f"mcp_timeout={result['mcp_timeout_sec']} skill={result['skill_status']} "
        f"safety_call_timeout={result['safety_call_timeout_sec']} "
        f"tool_call_timeout={result['tool_call_timeout_sec']} "
        f"enabled={str(result['skill_enabled']).lower()} "
        f"review_performed={str(result['review_performed']).lower()} "
        "provider_secrets_persisted=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
