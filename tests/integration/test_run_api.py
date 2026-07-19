from __future__ import annotations

import pathlib
import time

from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.api.app.mcp.service import FactoryMcpService
from apps.api.app.services.catalog import DEFAULT_DATA_DIR
from apps.api.app.settings import Settings
from tests.integration.test_run_coordinator import FakeTaskAdapter


def _settings(tmp_path: pathlib.Path) -> Settings:
    return Settings(
        APP_ENV="test",
        DATABASE_URL=f"sqlite:///{tmp_path / 'factory.db'}",
        ARTIFACTS_DIR=tmp_path / "artifacts",
        SYNTHETIC_DATA_DIR=DEFAULT_DATA_DIR,
        MCP_SHARED_TOKEN="mcp-test-token-that-is-at-least-32-chars",
        LIVE_TASK_TIMEOUT_SECONDS=50,
        LIVE_RUN_TERMINAL_DEADLINE_SECONDS=55,
    )


def _key(ordinal: int) -> dict[str, str]:
    return {"Idempotency-Key": f"run-api-idempotency-{ordinal:04d}"}


def _ready_campaign(client: TestClient, *, ordinal: int) -> str:
    created = client.post(
        "/api/v1/campaigns",
        json={"case_id": "B04"},
        headers=_key(ordinal),
    )
    campaign_id = str(created.json()["campaign_id"])
    validated = client.post(
        f"/api/v1/campaigns/{campaign_id}/validate",
        headers=_key(ordinal + 1),
    )
    assert validated.json()["state"] == "READY"
    return campaign_id


def _wait_terminal(client: TestClient, run_id: str) -> dict[str, object]:
    for _ in range(100):
        response = client.get(f"/api/v1/runs/{run_id}")
        payload = response.json()
        if payload["terminal_at"] is not None and payload["worker_released_at"] is not None:
            return payload  # type: ignore[no-any-return]
        time.sleep(0.01)
    raise AssertionError("run API did not reach terminal state")


def test_live_run_api_exposes_terminal_record_sse_replay_and_package(
    tmp_path: pathlib.Path,
) -> None:
    adapters: list[FakeTaskAdapter] = []

    def adapter_factory(mcp: FactoryMcpService) -> FakeTaskAdapter:
        adapter = FakeTaskAdapter(mcp)
        adapters.append(adapter)
        return adapter

    app = create_app(_settings(tmp_path), task_adapter_factory=adapter_factory)
    with TestClient(app) as client:
        campaign_id = _ready_campaign(client, ordinal=1)
        started = client.post(
            f"/api/v1/campaigns/{campaign_id}/runs",
            json={"mode": "live_ouroboros"},
            headers=_key(3),
        )
        assert started.status_code == 201
        run_id = str(started.json()["run_id"])
        completed = _wait_terminal(client, run_id)

        assert completed["status"] == "COMPLETED"
        assert completed["mode"] == "live_ouroboros"
        assert completed["package_id"] is not None
        package = client.get(f"/api/v1/packages/{completed['package_id']}")
        assert package.status_code == 200
        assert package.json()["quality_report"]["approvable"] is True

        stream = client.get(f"/api/v1/runs/{run_id}/events")
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert "event: run.accepted" in stream.text
        assert "event: run.terminal" in stream.text
        first_id = int(
            next(
                line.removeprefix("id: ")
                for line in stream.text.splitlines()
                if line.startswith("id: ")
            )
        )
        replay = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": str(first_id)},
        )
        assert f"id: {first_id}\n" not in replay.text
        assert "event: run.terminal" in replay.text

    assert len(adapters) == 1
    assert adapters[0].payload["source"] == "communication_factory_ui"
    assert adapters[0].payload["timeout_sec"] == 50


def test_cancel_endpoint_reconciles_then_returns_marked_template_fallback(
    tmp_path: pathlib.Path,
) -> None:
    def adapter_factory(mcp: FactoryMcpService) -> FakeTaskAdapter:
        return FakeTaskAdapter(mcp, behavior="hold")

    app = create_app(_settings(tmp_path), task_adapter_factory=adapter_factory)
    with TestClient(app) as client:
        campaign_id = _ready_campaign(client, ordinal=10)
        started = client.post(
            f"/api/v1/campaigns/{campaign_id}/runs",
            json={"mode": "live_ouroboros"},
            headers=_key(12),
        )
        run_id = str(started.json()["run_id"])
        cancelled = client.post(
            f"/api/v1/runs/{run_id}/cancel",
            headers=_key(13),
        )
        assert cancelled.status_code == 200
        completed = _wait_terminal(client, run_id)

        assert completed["status"] == "COMPLETED_FALLBACK"
        assert completed["mode"] == "deterministic_template"
        assert completed["reason_code"] == "LIVE_TASK_CANCELLED"
