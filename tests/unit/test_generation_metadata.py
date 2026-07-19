from __future__ import annotations

from typing import Any

import pytest

from scripts.generation_metadata import (
    GenerationMetadataError,
    metadata_usage_rows,
    poll_generation_metadata,
)


def _request() -> dict[str, Any]:
    return {
        "generation_id": "gen-test-12345678",
        "category": "main_generation",
        "estimated_prompt_tokens": 500,
        "configured_max_output_tokens": 10_240,
    }


def test_metadata_poll_is_bounded_and_recovers_delayed_generation() -> None:
    now = [0.0]
    calls = [0]

    def monotonic() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    def probe(generation_id: str) -> dict[str, Any]:
        calls[0] += 1
        if calls[0] == 1:
            return {"generation_id": generation_id, "found": False, "status_code": 404}
        return {
            "generation_id": generation_id,
            "found": True,
            "status_code": 200,
            "data": {
                "id": generation_id,
                "model": "z-ai/glm-5.2-20260616",
                "native_tokens_prompt": 100,
                "native_tokens_completion": 20,
                "total_cost": 0.001,
            },
        }

    report = poll_generation_metadata(
        [_request()],
        max_seconds=10,
        interval_seconds=5,
        probe=probe,
        monotonic=monotonic,
        sleeper=sleep,
    )

    assert report["status"] == "complete"
    assert report["resolved_generation_ids"] == ["gen-test-12345678"]
    assert report["elapsed_seconds"] == 5.0
    rows = metadata_usage_rows(
        "run-1",
        [_request()],
        report,
        expected_model="z-ai/glm-5.2",
    )
    assert rows[0]["prompt_tokens"] == 100
    assert rows[0]["completion_tokens"] == 20
    assert rows[0]["cost_usd"] == 0.001
    assert rows[0]["model"] == "z-ai/glm-5.2"
    assert rows[0]["provider_reported_model"] == "z-ai/glm-5.2-20260616"
    assert rows[0]["usage_source"] == "openrouter_generation_metadata"


def test_metadata_usage_rejects_unapproved_resolved_model() -> None:
    report = {
        "results": [
            {
                "generation_id": "gen-test-12345678",
                "found": True,
                "data": {
                    "model": "z-ai/glm-5.2-20990101",
                    "native_tokens_prompt": 100,
                    "native_tokens_completion": 20,
                    "total_cost": 0.001,
                },
            }
        ]
    }

    with pytest.raises(GenerationMetadataError, match="incomplete or drifted"):
        metadata_usage_rows(
            "run-1",
            [_request()],
            report,
            expected_model="z-ai/glm-5.2",
        )


def test_metadata_poll_stops_at_deadline_and_keeps_orphan_unresolved() -> None:
    now = [0.0]

    def monotonic() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    report = poll_generation_metadata(
        [_request()],
        max_seconds=10,
        interval_seconds=5,
        probe=lambda generation_id: {
            "generation_id": generation_id,
            "found": False,
            "status_code": 404,
        },
        monotonic=monotonic,
        sleeper=sleep,
    )

    assert report["status"] == "incomplete"
    assert report["elapsed_seconds"] == 10
    assert report["unresolved_generation_ids"] == ["gen-test-12345678"]
    assert len(report["attempts"]) == 3
    assert (
        metadata_usage_rows(
            "run-1",
            [_request()],
            report,
            expected_model="z-ai/glm-5.2",
        )
        == []
    )
