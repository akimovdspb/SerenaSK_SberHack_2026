from __future__ import annotations

import json
import pathlib
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from provider_profiles import normalize_provider_model, provider_metadata_model_allowed

ROOT = pathlib.Path(__file__).resolve().parents[1]

Probe = Callable[[str], dict[str, Any]]


class GenerationMetadataError(RuntimeError):
    pass


def _number(value: Any, label: str) -> int | float | str:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise GenerationMetadataError(f"generation metadata {label} is malformed")
    return cast(int | float | str, value)


def query_container(generation_id: str) -> dict[str, Any]:
    process = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "ouroboros",
            "python",
            "/opt/communication-factory/generation_metadata_probe.py",
            "--generation-id",
            generation_id,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    try:
        value = json.loads(process.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise GenerationMetadataError("generation metadata probe returned no safe report") from exc
    if not isinstance(value, dict) or value.get("generation_id") != generation_id:
        raise GenerationMetadataError("generation metadata probe identity differs")
    return {str(key): item for key, item in value.items()}


def poll_generation_metadata(
    requests: list[dict[str, Any]],
    *,
    max_seconds: int,
    interval_seconds: float = 30.0,
    probe: Probe = query_container,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if max_seconds <= 0 or max_seconds > 600:
        raise GenerationMetadataError("generation metadata poll limit must be 1..600 seconds")
    generation_ids = [str(row.get("generation_id") or "") for row in requests]
    if not generation_ids or len(set(generation_ids)) != len(generation_ids):
        raise GenerationMetadataError("orphan generation IDs are missing or duplicated")
    started = monotonic()
    deadline = started + max_seconds
    pending = set(generation_ids)
    latest: dict[str, dict[str, Any]] = {}
    attempts: list[dict[str, Any]] = []
    while pending:
        for generation_id in sorted(pending):
            try:
                result = probe(generation_id)
            except Exception as exc:
                result = {
                    "schema_version": 1,
                    "generation_id": generation_id,
                    "found": False,
                    "status_code": 0,
                    "error_type": type(exc).__name__,
                }
            latest[generation_id] = result
            attempts.append(
                {
                    "generation_id": generation_id,
                    "found": result.get("found") is True,
                    "status_code": int(result.get("status_code") or 0),
                    "error_type": str(result.get("error_type") or ""),
                    "elapsed_seconds": round(max(0.0, monotonic() - started), 3),
                }
            )
            if result.get("found") is True:
                pending.remove(generation_id)
        if not pending or monotonic() >= deadline:
            break
        sleeper(min(interval_seconds, max(0.0, deadline - monotonic())))
    elapsed = min(max_seconds, max(0.0, monotonic() - started))
    return {
        "schema_version": 1,
        "status": "complete" if not pending else "incomplete",
        "poll_max_seconds": max_seconds,
        "elapsed_seconds": round(elapsed, 3),
        "requested_generation_ids": generation_ids,
        "resolved_generation_ids": sorted(set(generation_ids) - pending),
        "unresolved_generation_ids": sorted(pending),
        "attempts": attempts,
        "results": [latest[generation_id] for generation_id in generation_ids],
    }


def metadata_usage_rows(
    run_id: str,
    orphan_requests: list[dict[str, Any]],
    poll_report: dict[str, Any],
    *,
    expected_model: str,
) -> list[dict[str, Any]]:
    request_by_id = {str(row.get("generation_id") or ""): row for row in orphan_requests}
    rows: list[dict[str, Any]] = []
    for raw in poll_report.get("results") or []:
        if not isinstance(raw, dict) or raw.get("found") is not True:
            continue
        generation_id = str(raw.get("generation_id") or "")
        request = request_by_id.get(generation_id)
        data = raw.get("data")
        if request is None or not isinstance(data, dict):
            raise GenerationMetadataError("resolved generation metadata is unbound")
        reported_model = normalize_provider_model(str(data.get("model") or ""))
        model = normalize_provider_model(expected_model)
        prompt_tokens = int(
            _number(
                data.get("native_tokens_prompt")
                if data.get("native_tokens_prompt") is not None
                else data.get("tokens_prompt") or 0,
                "prompt tokens",
            )
        )
        completion_tokens = int(
            _number(
                data.get("native_tokens_completion")
                if data.get("native_tokens_completion") is not None
                else data.get("tokens_completion") or 0,
                "completion tokens",
            )
        )
        cost_usd = float(
            _number(
                data.get("total_cost")
                if data.get("total_cost") is not None
                else data.get("usage") or 0.0,
                "cost",
            )
        )
        if (
            not provider_metadata_model_allowed(
                expected_model=model,
                reported_model=reported_model,
            )
            or prompt_tokens <= 0
            or completion_tokens < 0
            or cost_usd < 0
        ):
            raise GenerationMetadataError("resolved generation usage is incomplete or drifted")
        rows.append(
            {
                "ts": datetime.now(UTC).isoformat(),
                "run_id": run_id,
                "provider": "openrouter",
                "model": model,
                "category": str(request.get("category") or "unattributed"),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": cost_usd,
                "generation_id": generation_id,
                "usage_source": "openrouter_generation_metadata",
                "provider_reported_model": reported_model,
            }
        )
    return rows
