from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import request_ledger
from provider_profiles import CAMPAIGN_AUTHORING_PROFILE_NAME, provider_profile
from scripts.generation_metadata import metadata_usage_rows, poll_generation_metadata

ROOT = pathlib.Path(__file__).resolve().parents[1]
QUALITY_ROOT = ROOT / "runtime" / "campaign-authoring-quality-v3"
GOAL_ID = "campaign-authoring-copy-quality-v3-20260717"
REQUIRED_BRANCH = "codex/campaign-authoring-quality-v3-20260717"
SOURCE_COMMIT = "fca0ae6e381680f48438023074f09f81ecc41a50"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
EXPECTED_CANONICAL_SLUG = "z-ai/glm-5.2-20260616"
PRICE_INPUT_CEILING_PER_TOKEN = Decimal("0.000001")
PRICE_OUTPUT_CEILING_PER_TOKEN = Decimal("0.000003")
GOAL_TOKEN_CAP = 50_000_000
GOAL_COST_CAP_USD = Decimal("150")
REQUEST_TOKEN_CAP = 500_000
REQUEST_COST_CAP_USD = Decimal("2")
CONTAINER_LEDGER_PATH = "/accounting/request-ledger.json"
RUNTIME_ADMIN = "/opt/communication-factory/runtime_admin.py"
CONFIGURE_RUNTIME = "/opt/communication-factory/configure_runtime.py"
REFERENCE_IDS = (
    "editorial_dq01",
    "editorial_dq03",
    "editorial_dq06",
    "editorial_dq07",
    "editorial_dq09",
    "editorial_dq11",
    "editorial_dq12",
)
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$")


class CampaignAuthoringQualityError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _atomic_json(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o660)
    os.replace(temporary, path)


def _load_json(path: pathlib.Path, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignAuthoringQualityError(f"{label} is unreadable") from exc
    if not isinstance(parsed, dict):
        raise CampaignAuthoringQualityError(f"{label} must be a JSON object")
    return {str(key): value for key, value in parsed.items()}


def _git(*arguments: str) -> str:
    process = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if process.returncode != 0:
        raise CampaignAuthoringQualityError("Git identity check failed")
    return process.stdout.strip()


def _assert_git_identity(*, clean: bool) -> tuple[str, str]:
    branch = _git("branch", "--show-current")
    commit = _git("rev-parse", "HEAD")
    if branch != REQUIRED_BRANCH:
        raise CampaignAuthoringQualityError("campaign quality branch identity differs")
    if (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", SOURCE_COMMIT, "HEAD"],
            cwd=ROOT,
            capture_output=True,
            timeout=30,
            check=False,
        ).returncode
        != 0
    ):
        raise CampaignAuthoringQualityError("campaign quality source ancestry differs")
    if clean and _git("status", "--porcelain=v1"):
        raise CampaignAuthoringQualityError("live qualification requires a clean commit")
    return commit, branch


def _secret_path() -> pathlib.Path:
    return pathlib.Path("/home/dmitry/secrets/communication-factory/OPENROUTER_API_KEY.txt")


def _assert_secret_boundary() -> pathlib.Path:
    path = _secret_path()
    try:
        metadata = path.lstat()
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CampaignAuthoringQualityError("OpenRouter secret source is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise CampaignAuthoringQualityError("OpenRouter secret source must be a regular file")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise CampaignAuthoringQualityError("OpenRouter secret source mode must be 0600")
    if len(lines) != 1 or not lines[0].strip():
        raise CampaignAuthoringQualityError("OpenRouter secret source must contain one line")
    if os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        raise CampaignAuthoringQualityError(
            "host qualification process must not inherit provider keys"
        )
    for candidate in ROOT.rglob("*API_KEY*.txt"):
        if ".git" not in candidate.parts and candidate.is_file():
            raise CampaignAuthoringQualityError("provider key copy exists inside the Git checkout")
    return path


def _price_contract() -> dict[str, str]:
    request = urllib.request.Request(
        OPENROUTER_MODELS_URL,
        headers={"User-Agent": "communication-factory-price-preflight/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except (OSError, ValueError) as exc:
        raise CampaignAuthoringQualityError("OpenRouter price metadata is unavailable") from exc
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise CampaignAuthoringQualityError("OpenRouter price metadata is malformed")
    model = next(
        (row for row in rows if isinstance(row, dict) and row.get("id") == "z-ai/glm-5.2"),
        None,
    )
    if not isinstance(model, dict):
        raise CampaignAuthoringQualityError("pinned GLM-5.2 metadata is absent")
    pricing = model.get("pricing")
    if not isinstance(pricing, dict):
        raise CampaignAuthoringQualityError("pinned GLM-5.2 pricing is absent")
    canonical_slug = str(model.get("canonical_slug") or "")
    if canonical_slug != EXPECTED_CANONICAL_SLUG:
        raise CampaignAuthoringQualityError("pinned GLM-5.2 canonical model drifted")
    try:
        input_price = Decimal(str(pricing.get("prompt") or ""))
        output_price = Decimal(str(pricing.get("completion") or ""))
    except InvalidOperation as exc:
        raise CampaignAuthoringQualityError("pinned GLM-5.2 pricing is malformed") from exc
    if (
        not input_price.is_finite()
        or not output_price.is_finite()
        or input_price <= 0
        or output_price <= 0
        or input_price > PRICE_INPUT_CEILING_PER_TOKEN
        or output_price > PRICE_OUTPUT_CEILING_PER_TOKEN
    ):
        raise CampaignAuthoringQualityError("pinned GLM-5.2 price contract drifted")
    projection = {
        "model": "z-ai/glm-5.2",
        "canonical_slug": canonical_slug,
        "input_price_per_token_usd": str(input_price),
        "output_price_per_token_usd": str(output_price),
        "input_price_ceiling_per_token_usd": str(PRICE_INPUT_CEILING_PER_TOKEN),
        "output_price_ceiling_per_token_usd": str(PRICE_OUTPUT_CEILING_PER_TOKEN),
        "source": OPENROUTER_MODELS_URL,
        "observed_at": _utc_now(),
    }
    projection["projection_sha256"] = hashlib.sha256(
        json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return projection


def _resolve_run_dir(value: str | os.PathLike[str]) -> pathlib.Path:
    candidate = pathlib.Path(value)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(QUALITY_ROOT.resolve())
    except ValueError as exc:
        raise CampaignAuthoringQualityError(
            "qualification run directory is outside runtime"
        ) from exc
    return resolved


def _state_path(run_dir: pathlib.Path) -> pathlib.Path:
    return run_dir / "run.json"


def _ledger_path(run_dir: pathlib.Path) -> pathlib.Path:
    return run_dir / "request-ledger.json"


def initialize_run(*, evaluation_id: str) -> dict[str, Any]:
    if not SAFE_ID.fullmatch(evaluation_id):
        raise CampaignAuthoringQualityError("evaluation id is invalid")
    commit, branch = _assert_git_identity(clean=True)
    _assert_secret_boundary()
    price = _price_contract()
    input_price = Decimal(price["input_price_per_token_usd"])
    output_price = Decimal(price["output_price_per_token_usd"])
    run_dir = QUALITY_ROOT / evaluation_id
    try:
        run_dir.mkdir(parents=True, mode=0o2770)
    except FileExistsError as exc:
        raise CampaignAuthoringQualityError("evaluation directory already exists") from exc
    os.chmod(run_dir, 0o2770)
    schema_attempt_id = f"schema_{uuid.uuid4().hex}"
    request_ledger.initialize_ledger(
        _ledger_path(run_dir),
        goal_id=GOAL_ID,
        evaluation_id=evaluation_id,
        provider="openrouter",
        model="z-ai/glm-5.2",
        input_price_per_token_usd=input_price,
        output_price_per_token_usd=output_price,
        price_source=OPENROUTER_MODELS_URL,
        price_observed_at=price["observed_at"],
        run_token_cap=GOAL_TOKEN_CAP,
        run_cost_cap_usd=GOAL_COST_CAP_USD,
        request_token_cap=REQUEST_TOKEN_CAP,
        request_cost_cap_usd=REQUEST_COST_CAP_USD,
    )
    state: dict[str, Any] = {
        "schema_version": 1,
        "goal_id": GOAL_ID,
        "evaluation_id": evaluation_id,
        "status": "initialized",
        "created_at": _utc_now(),
        "source_commit": SOURCE_COMMIT,
        "qualification_commit": commit,
        "branch": branch,
        "compose_project": f"cf-authoring-v3-{uuid.uuid4().hex[:10]}",
        "run_dir": str(run_dir.relative_to(ROOT)),
        "ledger_path": str(_ledger_path(run_dir).relative_to(ROOT)),
        "schema_attempt_id": schema_attempt_id,
        "price_contract": price,
        "cases": {},
    }
    _atomic_json(_state_path(run_dir), state)
    return state


def _load_state(run_dir_value: str | os.PathLike[str]) -> tuple[pathlib.Path, dict[str, Any]]:
    run_dir = _resolve_run_dir(run_dir_value)
    state = _load_json(_state_path(run_dir), "qualification state")
    if state.get("goal_id") != GOAL_ID or state.get("run_dir") != str(run_dir.relative_to(ROOT)):
        raise CampaignAuthoringQualityError("qualification state identity differs")
    return run_dir, state


def _compose_environment(run_dir: pathlib.Path, state: Mapping[str, Any]) -> dict[str, str]:
    profile = provider_profile(CAMPAIGN_AUTHORING_PROFILE_NAME)
    environment = dict(os.environ)
    environment.update(
        {
            "COMPOSE_PROJECT_NAME": str(state["compose_project"]),
            "EVAL_PROVIDER_PROFILE": profile.name,
            "CF_PROVIDER_PROFILE": profile.name,
            "CF_RUNTIME_PROVIDER": profile.runtime_provider,
            "OPENROUTER_ENABLED": "true",
            "OUROBOROS_MODEL": profile.runtime_route,
            "PROVIDER_API_KEY_HOST_PATH": str(_secret_path()),
            "PROVIDER_API_KEY_CONTAINER_PATH": profile.secret_container_path,
            "LIVE_TASK_TIMEOUT_SECONDS": str(profile.effective_task_timeout_seconds),
            "LIVE_RUN_TERMINAL_DEADLINE_SECONDS": str(profile.effective_terminal_deadline_seconds),
            "LIVE_USAGE_EXPECTED_PROVIDER": profile.ledger_provider,
            "LIVE_USAGE_REQUIRE_POST_TASK_SUMMARY": "false",
            "CONTROLLED_PROVIDER_RETRY_ENABLED": "false",
            "TOTAL_BUDGET": str(GOAL_COST_CAP_USD),
            "OUROBOROS_PER_TASK_COST_USD": str(REQUEST_COST_CAP_USD),
            "OUROBOROS_REVIEW_MAX_TOKENS": "16384",
            "CF_REQUEST_LEDGER_HOST_DIR": str(run_dir),
            "CF_REQUEST_LEDGER_CONTAINER_PATH": CONTAINER_LEDGER_PATH,
            "CF_REQUEST_LEDGER_GOAL_ID": GOAL_ID,
            "CF_REQUEST_LEDGER_EVALUATION_ID": str(state["evaluation_id"]),
            "CF_REQUEST_LEDGER_DEFAULT_CASE_ID": "SCHEMA_PROBE",
            "CF_REQUEST_LEDGER_DEFAULT_ATTEMPT_ID": str(state["schema_attempt_id"]),
            "LOCAL_UID": str(os.getuid()),
            "LOCAL_GID": str(os.getgid()),
        }
    )
    environment.pop("OPENROUTER_API_KEY", None)
    environment.pop("OPENAI_API_KEY", None)
    return environment


def _run(
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        cwd=ROOT,
        env=dict(environment),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _compose(
    environment: Mapping[str, str],
    *arguments: str,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    return _run(
        ["docker", "compose", *arguments],
        environment=environment,
        timeout=timeout,
    )


def _safe_json_result(process: subprocess.CompletedProcess[str], label: str) -> dict[str, Any]:
    lines = [line for line in process.stdout.splitlines() if line.strip()]
    try:
        parsed = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise CampaignAuthoringQualityError(f"{label} returned no safe JSON") from exc
    if not isinstance(parsed, dict):
        raise CampaignAuthoringQualityError(f"{label} returned malformed JSON")
    return {str(key): value for key, value in parsed.items()}


def _transient_retry_allowed(
    run_dir: pathlib.Path,
    *,
    case_id: str,
    previous: Mapping[str, Any],
) -> bool:
    report_path = previous.get("report_path")
    if not isinstance(report_path, str) or not report_path:
        return False
    report = _load_json(ROOT / report_path, "previous case report")
    transient_error_types = {
        "ConnectError",
        "ConnectTimeout",
        "ConnectionError",
        "ManagedTaskTransportError",
        "PoolTimeout",
        "ReadError",
        "ReadTimeout",
        "TimeoutError",
        "WriteError",
        "WriteTimeout",
    }
    explicit = str(report.get("error_type") or "") in transient_error_types
    raw_run = report.get("run")
    run = raw_run if isinstance(raw_run, dict) else {}
    attempts = run.get("attempts")
    run_attempts = attempts if isinstance(attempts, list) else []
    retry_assessed = any(
        isinstance(row, dict) and row.get("retry_allowed") is True for row in run_attempts
    )
    reason_code = str(run.get("reason_code") or "")
    transient_reason = reason_code.startswith("TRANSIENT_") or reason_code in {
        "TERMINAL_DEADLINE",
        "WORKER_RELEASE_UNCONFIRMED",
    }
    # The goal-level accounting contract keeps a request-specific unknown bound
    # charged against the aggregate caps. That durable bound makes a single
    # transient retry safe; it must not become an attempt-level owner gate.
    return bool(explicit or retry_assessed or transient_reason)


def _runtime_admin(
    environment: Mapping[str, str],
    command: str,
    *,
    timeout: int = 900,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    process = _compose(
        environment,
        "exec",
        "-T",
        "ouroboros",
        "python",
        RUNTIME_ADMIN,
        command,
        timeout=timeout,
    )
    return process, _safe_json_result(process, f"runtime admin {command}")


def bootstrap_schema_probe(run_dir_value: str | os.PathLike[str]) -> dict[str, Any]:
    run_dir, state = _load_state(run_dir_value)
    if state.get("status") != "initialized":
        raise CampaignAuthoringQualityError("schema probe requires initialized state")
    _assert_git_identity(clean=True)
    _assert_secret_boundary()
    environment = _compose_environment(run_dir, state)
    started = _utc_now()
    up = _compose(
        environment,
        "up",
        "-d",
        "--build",
        "--wait",
        "--wait-timeout",
        "180",
        "app",
        "ouroboros",
        timeout=1_200,
    )
    if up.returncode != 0:
        raise CampaignAuthoringQualityError("isolated qualification containers failed to start")
    configure = _compose(
        environment,
        "exec",
        "-T",
        "ouroboros",
        "python",
        CONFIGURE_RUNTIME,
        timeout=60,
    )
    if configure.returncode != 0:
        raise CampaignAuthoringQualityError("campaign runtime configuration failed")
    refresh_process, refresh = _runtime_admin(environment, "refresh")
    if refresh_process.returncode != 0:
        raise CampaignAuthoringQualityError("campaign skill refresh failed")
    review_process, review = _runtime_admin(environment, "review")
    schema_report = {
        "schema_version": 1,
        "kind": "schema_structured_output_probe",
        "evaluation_id": state["evaluation_id"],
        "attempt_id": state["schema_attempt_id"],
        "started_at": started,
        "finished_at": _utc_now(),
        "product_output_generated": False,
        "refresh": refresh,
        "review": review,
        "review_exit_code": review_process.returncode,
    }
    _atomic_json(run_dir / "schema-probe.json", schema_report)
    if (
        review_process.returncode != 0
        or review.get("status") != "clean"
        or review.get("error_present") is True
    ):
        state["status"] = "schema_probe_failed"
        state["updated_at"] = _utc_now()
        _atomic_json(_state_path(run_dir), state)
        raise CampaignAuthoringQualityError("schema structured-output probe failed")
    enable_process, enable = _runtime_admin(environment, "enable")
    if enable_process.returncode != 0 or enable.get("enabled") is not True:
        raise CampaignAuthoringQualityError("reviewed campaign skill could not be enabled")
    image = _run(
        [
            "docker",
            "image",
            "inspect",
            "communication-factory/ouroboros:v6.61.4",
            "--format",
            "{{.Id}}",
        ],
        environment=environment,
        timeout=30,
    )
    if image.returncode != 0 or not image.stdout.strip().startswith("sha256:"):
        raise CampaignAuthoringQualityError("qualification runtime image identity is unavailable")
    probe_environment = dict(environment)
    probe_environment["CONTRACT_IMAGE_ID"] = image.stdout.strip()
    contract = _compose(
        probe_environment,
        "--profile",
        "tools",
        "run",
        "--rm",
        "contract-probe",
        timeout=300,
    )
    if contract.returncode != 0:
        raise CampaignAuthoringQualityError("campaign runtime contract probe failed")
    ledger = request_ledger.read_ledger(_ledger_path(run_dir))
    schema_rows = [
        row
        for row in ledger.get("requests") or []
        if isinstance(row, dict) and row.get("case_id") == "SCHEMA_PROBE"
    ]
    if not schema_rows or any(row.get("status") == "RESERVED" for row in schema_rows):
        raise CampaignAuthoringQualityError("schema probe request accounting is incomplete")
    state.update(
        {
            "status": "schema_probe_passed",
            "updated_at": _utc_now(),
            "runtime_image_id": image.stdout.strip(),
            "schema_request_ids": [row.get("request_id") for row in schema_rows],
            "skill_content_hash": review.get("content_hash"),
        }
    )
    _atomic_json(_state_path(run_dir), state)
    return {
        "status": state["status"],
        "schema_requests": len(schema_rows),
        "ledger_totals": request_ledger.ledger_totals(ledger),
    }


def run_case(
    run_dir_value: str | os.PathLike[str],
    *,
    reference_id: str,
    phase: str,
) -> dict[str, Any]:
    if reference_id not in REFERENCE_IDS:
        raise CampaignAuthoringQualityError("unsupported editorial reference")
    run_dir, state = _load_state(run_dir_value)
    if state.get("status") not in {"schema_probe_passed", "cases_running", "cases_complete"}:
        raise CampaignAuthoringQualityError("live case requires a green schema probe")
    environment = _compose_environment(run_dir, state)
    case_id = reference_id.removeprefix("editorial_").upper()
    raw_cases = state.get("cases")
    cases = raw_cases if isinstance(raw_cases, dict) else {}
    attempts = cases.get(case_id)
    case_attempts = list(attempts) if isinstance(attempts, list) else []
    phases = [str(row.get("qualification_phase") or "") for row in case_attempts]
    if phase == "pilot":
        if case_id != "DQ01" or "pilot" in phases:
            raise CampaignAuthoringQualityError("DQ01 pilot phase is unavailable")
    elif phase == "basket":
        if "basket" in phases:
            raise CampaignAuthoringQualityError("basket case was already attempted")
    elif phase == "retry":
        if (
            "retry" in phases
            or not case_attempts
            or case_attempts[-1].get("status") != "failed"
            or not _transient_retry_allowed(
                run_dir,
                case_id=case_id,
                previous=case_attempts[-1],
            )
        ):
            raise CampaignAuthoringQualityError("transient retry is unavailable for this case")
    else:
        raise CampaignAuthoringQualityError("qualification phase is invalid")
    attempt_id = f"attempt_{case_id.lower()}_{phase}_{uuid.uuid4().hex}"
    output_path = run_dir / "cases" / f"{case_id.lower()}-{attempt_id}.json"
    case_attempts.append(
        {
            "attempt_id": attempt_id,
            "reference_id": reference_id,
            "qualification_phase": phase,
            "retry_of_phase": (
                case_attempts[-1].get("qualification_phase") if phase == "retry" else None
            ),
            "status": "running",
            "started_at": _utc_now(),
            "report_path": str(output_path.relative_to(ROOT)),
        }
    )
    cases[case_id] = case_attempts
    state["cases"] = cases
    state["status"] = "cases_running"
    state["updated_at"] = _utc_now()
    _atomic_json(_state_path(run_dir), state)
    process = _compose(
        environment,
        "exec",
        "-T",
        "app",
        "python",
        "-m",
        "apps.api.app.live_authoring_transport",
        "--reference-id",
        reference_id,
        "--evaluation-id",
        str(state["evaluation_id"]),
        "--attempt-id",
        attempt_id,
        "--ledger-path",
        CONTAINER_LEDGER_PATH,
        timeout=1_000,
    )
    report = _safe_json_result(process, f"authoring case {case_id}")
    report["transport_exit_code"] = process.returncode
    report["recorded_at"] = _utc_now()
    _atomic_json(output_path, report)
    case_attempts[-1].update(
        {
            "status": "passed" if report.get("mechanically_valid") is True else "failed",
            "finished_at": _utc_now(),
            "transport_exit_code": process.returncode,
            "error_type": report.get("error_type"),
        }
    )
    state["updated_at"] = _utc_now()
    _atomic_json(_state_path(run_dir), state)
    return report


def _query_generation(
    environment: Mapping[str, str],
    generation_id: str,
) -> dict[str, Any]:
    process = _compose(
        environment,
        "exec",
        "-T",
        "ouroboros",
        "python",
        "/opt/communication-factory/generation_metadata_probe.py",
        "--generation-id",
        generation_id,
        timeout=40,
    )
    return _safe_json_result(process, "generation metadata probe")


def reconcile_unknowns(
    run_dir_value: str | os.PathLike[str],
    *,
    max_seconds: int,
) -> dict[str, Any]:
    run_dir, state = _load_state(run_dir_value)
    environment = _compose_environment(run_dir, state)
    ledger = request_ledger.read_ledger(_ledger_path(run_dir))
    unknown = [
        dict(row)
        for row in ledger.get("requests") or []
        if isinstance(row, dict) and row.get("status") == "RETAINED_UNKNOWN"
    ]
    recoverable = [row for row in unknown if row.get("generation_id")]
    if not recoverable:
        report = {
            "schema_version": 1,
            "status": "nothing_recoverable",
            "retained_unknown": len(unknown),
            "recorded_at": _utc_now(),
        }
        _atomic_json(run_dir / "latest-reconciliation.json", report)
        return report
    poll = poll_generation_metadata(
        recoverable,
        max_seconds=max_seconds,
        interval_seconds=10,
        probe=lambda generation_id: _query_generation(environment, generation_id),
    )
    usage_rows = metadata_usage_rows(
        str(state["evaluation_id"]),
        recoverable,
        poll,
        expected_model="z-ai/glm-5.2",
    )
    request_by_generation = {
        str(row["generation_id"]): row for row in recoverable if row.get("generation_id")
    }
    reconciled: list[str] = []
    for usage in usage_rows:
        request = request_by_generation[str(usage["generation_id"])]
        request_ledger.reconcile_exact(
            _ledger_path(run_dir),
            request_id=str(request["request_id"]),
            prompt_tokens=int(usage["prompt_tokens"]),
            completion_tokens=int(usage["completion_tokens"]),
            cost_usd=str(usage["cost_usd"]),
            generation_id=str(usage["generation_id"]),
            usage_source="openrouter_generation_metadata",
        )
        reconciled.append(str(request["request_id"]))
    report = {
        "schema_version": 1,
        "status": poll["status"],
        "poll": poll,
        "reconciled_request_ids": reconciled,
        "recorded_at": _utc_now(),
    }
    _atomic_json(run_dir / f"reconciliation-{uuid.uuid4().hex}.json", report)
    _atomic_json(run_dir / "latest-reconciliation.json", report)
    return report


def summarize(run_dir_value: str | os.PathLike[str]) -> dict[str, Any]:
    run_dir, state = _load_state(run_dir_value)
    ledger = request_ledger.read_ledger(_ledger_path(run_dir))
    raw_cases = state.get("cases")
    cases = raw_cases if isinstance(raw_cases, dict) else {}
    matrix: list[dict[str, Any]] = []
    for case_id in (item.removeprefix("editorial_").upper() for item in REFERENCE_IDS):
        attempts = cases.get(case_id)
        rows = list(attempts) if isinstance(attempts, list) else []
        basket_rows = [
            row
            for row in rows
            if row.get("qualification_phase") == "basket"
            or (row.get("qualification_phase") == "retry" and row.get("retry_of_phase") == "basket")
        ]
        latest = basket_rows[-1] if basket_rows else {}
        matrix.append(
            {
                "case_id": case_id,
                "attempts": len(rows),
                "basket_attempts": len(basket_rows),
                "status": latest.get("status", "not_run"),
                "report_path": latest.get("report_path"),
                "error_type": latest.get("error_type"),
            }
        )
    request_rows = [row for row in ledger.get("requests") or [] if isinstance(row, dict)]
    report = {
        "schema_version": 1,
        "goal_id": GOAL_ID,
        "evaluation_id": state["evaluation_id"],
        "qualification_commit": state["qualification_commit"],
        "provider": "openrouter",
        "model": "z-ai/glm-5.2",
        "concurrency": 1,
        "case_matrix": matrix,
        "dq01_pilot": next(
            (
                row
                for row in reversed(list(cases.get("DQ01") or []))
                if row.get("qualification_phase") == "pilot"
                or (
                    row.get("qualification_phase") == "retry"
                    and row.get("retry_of_phase") == "pilot"
                )
            ),
            None,
        ),
        "mechanically_valid_cases": sum(row["status"] == "passed" for row in matrix),
        "ledger_totals": request_ledger.ledger_totals(ledger),
        "retained_unknown": [
            {
                "request_id": row.get("request_id"),
                "case_id": row.get("case_id"),
                "reserved_total_tokens": row.get("reserved_total_tokens"),
                "reserved_cost_usd": row.get("reserved_cost_usd"),
                "generation_id": row.get("generation_id"),
            }
            for row in request_rows
            if row.get("status") == "RETAINED_UNKNOWN"
        ],
        "recorded_at": _utc_now(),
    }
    _atomic_json(run_dir / "summary.json", report)
    if all(row["status"] != "not_run" for row in matrix):
        state["status"] = "cases_complete"
        state["updated_at"] = _utc_now()
        _atomic_json(_state_path(run_dir), state)
    return report


def stop_runtime(run_dir_value: str | os.PathLike[str]) -> dict[str, Any]:
    run_dir, state = _load_state(run_dir_value)
    environment = _compose_environment(run_dir, state)
    process = _compose(environment, "stop", "app", "ouroboros", timeout=120)
    if process.returncode != 0:
        raise CampaignAuthoringQualityError("isolated qualification containers did not stop")
    return {"status": "stopped", "compose_project": state["compose_project"]}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init")
    initialize.add_argument("--evaluation-id", required=True)
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("--run-dir", required=True)
    case = commands.add_parser("run-case")
    case.add_argument("--run-dir", required=True)
    case.add_argument("--reference-id", choices=REFERENCE_IDS, required=True)
    case.add_argument("--phase", choices=("pilot", "basket", "retry"), required=True)
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--run-dir", required=True)
    reconcile.add_argument("--max-seconds", type=int, default=600)
    summary = commands.add_parser("summary")
    summary.add_argument("--run-dir", required=True)
    stop = commands.add_parser("stop")
    stop.add_argument("--run-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            result = initialize_run(evaluation_id=args.evaluation_id)
        elif args.command == "bootstrap":
            result = bootstrap_schema_probe(args.run_dir)
        elif args.command == "run-case":
            result = run_case(
                args.run_dir,
                reference_id=args.reference_id,
                phase=args.phase,
            )
        elif args.command == "reconcile":
            result = reconcile_unknowns(args.run_dir, max_seconds=args.max_seconds)
        elif args.command == "summary":
            result = summarize(args.run_dir)
        else:
            result = stop_runtime(args.run_dir)
    except (
        CampaignAuthoringQualityError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
        request_ledger.RequestLedgerError,
    ) as exc:
        print(
            f"campaign-authoring-quality: FAIL error_type={type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
